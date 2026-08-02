"""同花顺行业指数：市场口径的板块行情，独立于申万成分股聚合。

**为什么要第二套板块读数。** 现有的 `sectors/momentum.py` 是从申万成分股日线
**聚合**出来的（等权中位数），这是免费档下唯一能拿到 breadth / 涨停家数 /
站上 MA20 占比的办法。但它拿不到两样东西：

1. **市值加权的板块指数本身**——所有人嘴里的「半导体今天跌了多少」说的是这个，
   不是成分股涨跌幅的中位数。两者经常不一样，而**不一样的时候恰恰是信息**：
   指数涨、宽度弱 = 权重股拉抬；指数平、宽度强 = 普涨但大票不动。
2. **板块级的日内位置**——收盘落在当日区间的哪个分位。2026-07-31 实测：半导体
   +4.23% 却收在当日**最低**（收位 0%），同日白酒 +0.63% 收在当日**最高**
   （收位 97%）。「涨了多少」两者都是正的，「谁在被卖」只有收位看得见。

**为什么用同花顺而不是东财。** 东财 `push2` 系板块接口在本机不可达（见
`DATA_SOURCE_AUDIT.md` §四）。同花顺 `d.10jqka.com.cn` 全系可用，**且没有限流**——
这一点很重要：申万聚合要吃掉 TickFlow 60 次/分钟配额里的 45 次，同花顺 90 个板块
一次都不占。

**口径。** 日频，收盘后定格。`last.js` 只回当年数据（2026 年 140 根），够算 20 日
窗口。不做任何跨源合并——这一层是**独立读数**，与申万排序并列呈现，不去猜两套
行业分类之间的映射（同花顺 90 个 ≈ 申万二级粒度，硬映到申万一级 31 个只会造出
一个看起来精确的错数）。
"""

from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Callable, Iterable

BASE_URL = "https://d.10jqka.com.cn/v6/line/bk_{code}/01/last.js"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://q.10jqka.com.cn/"

# 同花顺行业板块代码区间。2026-08-02 实测 881101..881419 里有 90 个有效板块，
# 其余返回空。扫一遍就把有效码缓存下来，之后只打这 90 个。
CODE_START = 881101
CODE_END = 881419

CACHE_NAME = "ths_sector_codes.json"
CACHE_SCHEMA = "ths-bk-v1"

TIMEOUT_SECONDS = 12
MAX_WORKERS = 16

# 窗口。20 根用于「距 20 日高点」与量比基线，5 根用于近周成交额。
HIGH_WINDOW = 20
VOLUME_WINDOW = 5
MIN_BARS = 25

_CALLBACK = re.compile(r"\((.*)\)\s*;?\s*$", re.S)


@dataclass(frozen=True)
class SectorIndexReading:
    """一个行业指数的当日读数。"""

    code: str
    name: str
    trade_date: str
    close: float
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_20d: float | None = None
    # 收盘落在当日区间的分位，0 = 收在最低，100 = 收在最高。
    # 「涨了多少」和「谁在被卖」是两个问题，这个字段答第二个。
    close_position: float | None = None
    off_20d_high: float | None = None
    # 近 5 日均额 / 近 20 日均额。>1 放量，<1 缩量。
    volume_ratio: float | None = None
    amount_5d: float | None = None

    @property
    def shape(self) -> str:
        """一句话形态。只用于措辞，不参与任何打分。"""
        if self.close_position is None or self.ret_1d is None:
            return "数据不足"
        if self.ret_1d > 0 and self.close_position <= 20:
            return "涨但收在日内低位（冲高回落，卖压在兑现）"
        if self.ret_1d > 0 and self.close_position >= 80:
            return "涨且收在日内高位（有承接）"
        if self.ret_1d < 0 and self.close_position >= 80:
            return "跌但收在日内高位（尾盘有买盘）"
        if self.ret_1d < 0 and self.close_position <= 20:
            return "跌且收在日内低位（无承接）"
        return "日内震荡"


@dataclass
class SectorIndexSnapshot:
    readings: list[SectorIndexReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def __len__(self) -> int:
        return len(self.readings)

    def top(self, key: str, n: int = 10, reverse: bool = True) -> list[SectorIndexReading]:
        scored = [r for r in self.readings if getattr(r, key) is not None]
        return sorted(scored, key=lambda r: getattr(r, key), reverse=reverse)[:n]

    def by_name(self) -> dict[str, SectorIndexReading]:
        return {r.name: r for r in self.readings}

    def describe(self, n: int = 5) -> list[str]:
        """给简报用的几行摘要。"""
        if not self.readings:
            return []
        lines: list[str] = []
        gainers = self.top("ret_1d", n)
        losers = self.top("ret_1d", n, reverse=False)
        if gainers:
            lines.append(
                "同花顺行业涨幅前列："
                + "、".join(f"{r.name} {r.ret_1d:+.2%}" for r in gainers)
            )
        if losers:
            lines.append(
                "同花顺行业跌幅前列："
                + "、".join(f"{r.name} {r.ret_1d:+.2%}" for r in losers)
            )
        # 「涨了但收在最低」是这一层独有的读数，单独点名。
        faded = [
            r
            for r in self.readings
            if r.ret_1d is not None
            and r.ret_1d > 0
            and r.close_position is not None
            and r.close_position <= 20
        ]
        if faded:
            faded.sort(key=lambda r: r.close_position or 0)
            lines.append(
                f"{len(faded)} 个板块当日上涨却收在日内低位（冲高回落）："
                + "、".join(
                    f"{r.name}（{r.ret_1d:+.2%}，收位 {r.close_position:.0f}%）"
                    for r in faded[:n]
                )
            )
        return lines


class ThsSectorIndexAdapter:
    """同花顺行业指数日线。无限流，不占 TickFlow 配额。"""

    name = "ths-sector-index"

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_workers = max_workers

    # ---- public ----------------------------------------------------------

    def fetch(self, codes: Iterable[str] | None = None) -> SectorIndexSnapshot:
        notes: list[str] = []
        wanted = list(codes) if codes is not None else self._codes(notes)
        if not wanted:
            return SectorIndexSnapshot(notes=notes + ["同花顺行业指数：没有可用板块代码"], ok=False)

        parsed: list[tuple[str, str, list[dict]]] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for code, result in zip(wanted, pool.map(self._safe_fetch_one, wanted)):
                if result is None:
                    failures += 1
                    continue
                name, bars = result
                if name and bars:
                    parsed.append((code, name, bars))

        readings = [r for r in (_measure(c, n, b) for c, n, b in parsed) if r is not None]
        if failures:
            notes.append(f"同花顺行业指数 {failures}/{len(wanted)} 个板块取数失败，其余照常")
        if not readings:
            return SectorIndexSnapshot(notes=notes + ["同花顺行业指数全部取数失败"], ok=False)

        # 各板块最新交易日必须一致，否则是在拿不同天的读数排名次。
        stamps = {r.trade_date for r in readings}
        if len(stamps) > 1:
            latest = max(stamps)
            dropped = [r for r in readings if r.trade_date != latest]
            readings = [r for r in readings if r.trade_date == latest]
            notes.append(
                f"同花顺行业指数有 {len(dropped)} 个板块最新交易日不是 {latest}，已排除"
                "（不同交易日的读数不可比）"
            )

        notes.append(
            f"同花顺行业指数 {len(readings)} 个板块，最新交易日 {readings[0].trade_date}"
        )
        return SectorIndexSnapshot(readings=readings, notes=notes, ok=True)

    # ---- internals -------------------------------------------------------

    def _codes(self, notes: list[str]) -> list[str]:
        cached = self._read_cache()
        if cached:
            return cached
        discovered = self._discover(notes)
        if discovered:
            self._write_cache(discovered)
        return discovered

    def _discover(self, notes: list[str]) -> list[str]:
        """扫一遍代码区间，留下有数据的。只在没有缓存时发生。"""
        candidates = [str(c) for c in range(CODE_START, CODE_END + 1)]
        alive: list[str] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for code, result in zip(candidates, pool.map(self._safe_fetch_one, candidates)):
                if result is not None and result[0] and result[1]:
                    alive.append(code)
        notes.append(
            f"同花顺行业代码首次探测：{CODE_START}..{CODE_END} 共 {len(candidates)} 个，"
            f"{len(alive)} 个有数据，已缓存"
        )
        return alive

    def _safe_fetch_one(self, code: str) -> tuple[str, list[dict]] | None:
        try:
            return self._fetch_one(code)
        except Exception:
            return None

    def _fetch_one(self, code: str) -> tuple[str, list[dict]] | None:
        request = urllib.request.Request(
            BASE_URL.format(code=code),
            headers={"User-Agent": USER_AGENT, "Referer": REFERER},
        )
        with self._opener(request, timeout=self.timeout) as response:
            text = response.read().decode("utf-8", "ignore")
        return _parse_line_js(text)

    def _cache_path(self) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / CACHE_NAME

    def _read_cache(self) -> list[str]:
        path = self._cache_path()
        if path is None:
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
            return []
        codes = payload.get("codes")
        return [str(c) for c in codes] if isinstance(codes, list) else []

    def _write_cache(self, codes: list[str]) -> bool:
        path = self._cache_path()
        if path is None or not codes:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"schema": CACHE_SCHEMA, "codes": codes}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return False
        return True


# ---- parsing -------------------------------------------------------------


def _parse_line_js(text: str) -> tuple[str, list[dict]] | None:
    """``quotebridge_v6_line_bk_881101_01_last({...})`` -> (板块名, 日线)。

    自己解析而不是用现成封装：上游改字段时这里是**解析失败**，不是静默错值。
    """
    match = _CALLBACK.search(text.strip())
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    name = str(payload.get("name") or "").strip()
    raw = payload.get("data")
    if not name or not isinstance(raw, str):
        return None

    bars: list[dict] = []
    for row in raw.split(";"):
        cells = row.split(",")
        if len(cells) < 7:
            continue
        try:
            bars.append(
                {
                    "date": cells[0],
                    "open": float(cells[1]),
                    "high": float(cells[2]),
                    "low": float(cells[3]),
                    "close": float(cells[4]),
                    "volume": float(cells[5]),
                    "amount": float(cells[6]),
                }
            )
        except ValueError:
            continue
    return name, bars


def _measure(code: str, name: str, bars: list[dict]) -> SectorIndexReading | None:
    if len(bars) < MIN_BARS:
        return None
    last = bars[-1]
    close = last["close"]
    if close <= 0:
        return None

    highs = [b["high"] for b in bars[-HIGH_WINDOW:]]
    amounts = [b["amount"] for b in bars if b["amount"] > 0]
    span = last["high"] - last["low"]

    return SectorIndexReading(
        code=code,
        name=name,
        trade_date=str(last["date"]),
        close=round(close, 3),
        ret_1d=_pct(bars[-2]["close"], close),
        ret_5d=_pct(bars[-6]["close"], close) if len(bars) >= 6 else None,
        ret_20d=_pct(bars[-21]["close"], close) if len(bars) >= 21 else None,
        close_position=(
            round((close - last["low"]) / span * 100, 1) if span > 0 else 50.0
        ),
        off_20d_high=_pct(max(highs), close) if highs and max(highs) > 0 else None,
        volume_ratio=(
            round(fmean(amounts[-VOLUME_WINDOW:]) / fmean(amounts[-HIGH_WINDOW:]), 4)
            if len(amounts) >= HIGH_WINDOW and fmean(amounts[-HIGH_WINDOW:]) > 0
            else None
        ),
        amount_5d=(
            round(fmean(amounts[-VOLUME_WINDOW:]), 2) if len(amounts) >= VOLUME_WINDOW else None
        ),
    )


def _pct(previous: float, current: float) -> float | None:
    if previous <= 0:
        return None
    return round(current / previous - 1.0, 6)
