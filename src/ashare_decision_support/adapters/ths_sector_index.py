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
`docs/DATA_SOURCES.md` §四）。同花顺 `d.10jqka.com.cn` 全系可用，**且没有限流**——
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.numeric import finite
from ashare_decision_support.state.paths import state_home

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

# 日线序列的落盘位置与格式。
#
# **取数已经把整条序列拿回来了，这里只是别再扔掉它。** `last.js` 一次回当年全部
# 日线（2026 年 140 根），而 `_measure` 只留最新一根——板块轮动、跷跷板、相对强弱
# 排名要的历史因此每天被丢一次，尽管它一分钱额外成本都没有。
#
# 落在状态目录而不是 `--output-dir`：后者常按日期命名，那样序列会散成一天一份、
# 永远拼不起来，而这份数据的全部价值就在于跨日累积。同 `jobs/morning.py` 里
# 语料库那段的理由。
HISTORY_DIR_NAME = "sector_index"
HISTORY_SCHEMA = "ths-bk-series-v1"

TIMEOUT_SECONDS = 12
MAX_WORKERS = 16

# 窗口。20 根用于「距 20 日高点」与量比基线，5 根用于近周成交额。
HIGH_WINDOW = 20
VOLUME_WINDOW = 5
MIN_BARS = 25

# 收盘位置的高低分界（收盘落在当日区间的百分位）。收在上沿 = 尾盘有承接，
# 收在下沿 = 卖压在兑现。`shape` 与 `describe()` 两处都要用，写死两遍必然漂。
HIGH_CLOSE = 80.0
LOW_CLOSE = 20.0

_CALLBACK = re.compile(r"\((.*)\)\s*;?\s*$", re.DOTALL)


def _yi(amount: float | None) -> str:
    """成交额 → 「日均 N 亿」。取不到就明说，不印一个 0 亿冒充读数。"""
    return "日均额缺失" if amount is None else f"日均 {amount / 1e8:.0f} 亿"


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
        if self.ret_1d > 0 and self.close_position <= LOW_CLOSE:
            return "涨但收在日内低位（冲高回落，卖压在兑现）"
        if self.ret_1d > 0 and self.close_position >= HIGH_CLOSE:
            return "涨且收在日内高位（有承接）"
        if self.ret_1d < 0 and self.close_position >= HIGH_CLOSE:
            return "跌但收在日内高位（尾盘有买盘）"
        if self.ret_1d < 0 and self.close_position <= LOW_CLOSE:
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
        lines.extend(self._close_position_lines(n))
        lines.extend(self._volume_position_lines(n))
        return lines

    # ---- describe 的两条独有读数 ------------------------------------------

    def _close_position_lines(self, n: int) -> list[str]:
        """收盘位置的横截面分布。

        **原来只在出现冲高回落时才有一行**，于是最强的那种收盘反而是沉默的：
        2026-08-07 实测 90 个板块涨了 78 个、冲高回落 **0 个**，简报上一个字都没有。
        沉默在这里不是中性的——读的人会把「没写」读成「没测出什么」，而当天的事实
        是全场尾盘承接极强，这正是本层 docstring 说它独有、别处拿不到的那个读数。

        所以分布**总是**报：分母（有收位读数的板块数）跟着出，读的人才能自己判断
        「0 个冲高回落」是全场强，还是当天压根没几个板块在涨。
        """
        scored = [r for r in self.readings if r.close_position is not None]
        if not scored:
            return []
        risers = [r for r in scored if r.ret_1d is not None and r.ret_1d > 0]
        held = [r for r in scored if (r.close_position or 0.0) >= HIGH_CLOSE]
        faded = sorted(
            (r for r in risers if (r.close_position or 0.0) <= LOW_CLOSE),
            key=lambda r: r.close_position or 0.0,
        )
        line = (
            f"收盘位置：{len(held)}/{len(scored)} 个板块收在日内高位"
            f"（收位 ≥{HIGH_CLOSE:.0f}%）；当日上涨的 {len(risers)} 个里"
            f"有 {len(faded)} 个收在日内低位"
        )
        if faded:
            line += "（冲高回落，卖压在兑现）：" + "、".join(
                f"{r.name}（{r.ret_1d:+.2%}，收位 {r.close_position:.0f}%）"
                for r in faded[:n]
            )
        elif risers:
            line += "——**没有一个冲高回落**，尾盘承接强。"
        else:
            line += "。"
        return [line]

    def _volume_position_lines(self, n: int) -> list[str]:
        """上涨板块按量能位置分两组，每一组都带上量级。

        `volume_ratio`（近 5 日均额 / 近 20 日均额）此前**算了但没有任何人读**——
        它是这一层第二个独有读数，而且是分辨「反弹有没有钱」的那一个：
        2026-08-07 实测同为大涨，小金属 1.29、贵金属 1.19 是放量，半导体 0.85、
        军工电子 0.94 是缩量。只报涨幅，这两类在简报上长得一模一样。

        **比值必须配量级，否则会读出一个反过来的结论。** 同日教育量能 1.68 排在
        放量榜首，但它日均只有 43 亿；半导体量能 0.85 垫底，日均却有 3835 亿。
        只看比值会得出「教育是最大的流入方向」，而当天真正的资金事实是**半导体
        那一侧在失血，且失的血比教育的全部成交额还多一个数量级**。
        `sectors/turnover.py:by_amount_delta` 早就写下过同一条理由（占比答「钱挪去
        哪了」，绝对额答「哪里的钱最多地消失了」）——这一层此前漏了后半句，而
        `amount_5d` 一直就在手边。
        """
        risers = [
            r
            for r in self.readings
            if r.ret_1d is not None and r.ret_1d > 0 and r.volume_ratio is not None
        ]
        if not risers:
            return []
        expanding = sorted(risers, key=lambda r: -(r.volume_ratio or 0.0))
        shrinking = [r for r in risers if (r.volume_ratio or 0.0) < 1.0]
        lines = [
            f"上涨板块的量能位置（近 5 日均额 / 近 20 日均额，括号内为日均额）：放量 "
            f"{len(risers) - len(shrinking)}/{len(risers)} 个，"
            + "、".join(
                f"{r.name} {r.volume_ratio:.2f}（{_yi(r.amount_5d)}）"
                for r in expanding[:n]
                if (r.volume_ratio or 0.0) >= 1.0
            )
        ]
        if shrinking:
            shrinking.sort(key=lambda r: r.volume_ratio or 0.0)
            lines.append(
                "⚠️ 涨但缩量（涨幅在榜、量能没跟上，更像空头回补而不是新钱进场）："
                + "、".join(
                    f"{r.name}（{r.ret_1d:+.2%}，量能 {r.volume_ratio:.2f}，"
                    f"{_yi(r.amount_5d)}）"
                    for r in shrinking[:n]
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
        history_dir: str | Path | None = None,
        persist_history: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_workers = max_workers
        self._history_dir = Path(history_dir) if history_dir else None
        self.persist_history = persist_history
        self.history_written = 0

    # ---- public ----------------------------------------------------------

    def fetch(self, codes: Iterable[str] | None = None) -> SectorIndexSnapshot:
        notes: list[str] = []
        wanted = list(codes) if codes is not None else self._codes(notes)
        if not wanted:
            return SectorIndexSnapshot(notes=[*notes, "同花顺行业指数：没有可用板块代码"], ok=False)

        parsed: list[tuple[str, str, list[dict]]] = []
        done, errors = gather(wanted, self._fetch_one, max_workers=self.max_workers)
        failures = len(errors)
        for code, result in done:
            if result is None:
                continue
            name, bars = result
            if name and bars:
                parsed.append((code, name, bars))

        readings = [r for r in (_measure(c, n, b) for c, n, b in parsed) if r is not None]
        # 落盘在 `_measure` 之后、闸门之前：即使某个板块的最新交易日对不齐、
        # 当日读数被排除，它的历史序列仍然是好的，没有理由跟着丢。
        self._persist_history(parsed)
        if failures:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            notes.append(
                f"同花顺行业指数 {failures}/{len(wanted)} 个板块取数失败（{kinds}），其余照常"
            )
        if not readings:
            return SectorIndexSnapshot(notes=[*notes, "同花顺行业指数全部取数失败"], ok=False)

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
        done, _errors = gather(candidates, self._fetch_one, max_workers=self.max_workers)
        # 探测阶段没有「失败」这回事：这个区间里本来就大部分代码不存在，取不到
        # 就是不存在。所以只看取到了什么，不记 note。
        alive = [code for code, result in done if result is not None and result[0] and result[1]]
        notes.append(
            f"同花顺行业代码首次探测：{CODE_START}..{CODE_END} 共 {len(candidates)} 个，"
            f"{len(alive)} 个有数据，已缓存"
        )
        return alive

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

    # ---- 日线序列落盘 ------------------------------------------------------

    def history_dir(self) -> Path:
        return self._history_dir if self._history_dir is not None else state_home() / HISTORY_DIR_NAME

    def _persist_history(self, parsed: list[tuple[str, str, list[dict]]]) -> int:
        """把整条日线按日期并进已有序列。**失败只降速，不影响当日读数。**

        并而不是覆盖：`last.js` 只回当年的数据，跨年之后覆盖会把去年整段抹掉，
        而那正是 60 日以上窗口要用的部分。
        """
        if not self.persist_history:
            return 0
        written = 0
        for code, name, bars in parsed:
            if _write_series(self.history_dir(), code, name, bars):
                written += 1
        self.history_written = written
        return written

    def _read_cache(self) -> list[str]:
        payload = read_stamped(self._cache_path(), CACHE_SCHEMA)
        if payload is None:
            return []
        codes = payload.get("codes")
        return [str(c) for c in codes] if isinstance(codes, list) else []

    def _write_cache(self, codes: list[str]) -> bool:
        if not codes:
            return False
        return write_stamped(self._cache_path(), {"codes": codes}, CACHE_SCHEMA)


# ---- 日线序列的读写 -------------------------------------------------------


def _series_path(root: Path, code: str) -> Path:
    return root / f"{code}.json"


def _write_series(root: Path, code: str, name: str, bars: list[dict]) -> bool:
    if not bars:
        return False
    merged = {str(bar["date"]): bar for bar in load_series(code, root)}
    merged.update({str(bar["date"]): bar for bar in bars})
    # 原子写在这里格外要紧：这一步是**整份重写**（旧序列并上新的），写到一半
    # 断掉丢的不是今天这根，是这个板块攒下来的整条历史。
    return write_stamped(
        _series_path(root, code),
        {"code": code, "name": name, "bars": [merged[key] for key in sorted(merged)]},
        HISTORY_SCHEMA,
    )


def load_series(code: str, root: str | Path | None = None) -> list[dict]:
    """读回一个板块的日线序列，按日期升序。没有就返回空列表。

    这是板块轮动那一层（主线持续度、跷跷板相关性、相对强弱排名）唯一的输入口。
    """
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    payload = read_stamped(_series_path(directory, str(code)), HISTORY_SCHEMA)
    return _usable_bars(payload)


def _usable_bars(payload: dict[str, Any] | None) -> list[dict]:
    """序列文件里能用的那些根。没有 `date` 的行丢掉——它排不进序，也对不上前一天。"""
    bars = (payload or {}).get("bars")
    if not isinstance(bars, list):
        return []
    return [bar for bar in bars if isinstance(bar, dict) and bar.get("date")]


def load_all_series(root: str | Path | None = None) -> dict[str, tuple[str, list[dict]]]:
    """读回全部已落盘的板块序列，`{code: (板块名, 日线)}`。

    板块名只在文件里，`load_series` 不带它出来——而轮动层报的是「电力设备排名
    上升最快」，不是「881155 排名上升最快」，所以这里单独走一遍 payload。

    目录不存在就返回空字典：`morning` 第一次跑之前本来就没有历史，那是预期状态
    而不是错误。**第一次跑完就有 140 根**（`last.js` 一次回当年全部日线），
    所以轮动层不需要等着攒够天数才能用。
    """
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    result: dict[str, tuple[str, list[dict]]] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return result
    for path in paths:
        payload = read_stamped(path, HISTORY_SCHEMA)
        usable = _usable_bars(payload)
        if not payload or not usable:
            continue
        code = str(payload.get("code") or path.stem)
        result[code] = (str(payload.get("name") or code), usable)
    return result


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
        # 一格收不住就整根丢掉。半根日线（有收盘没成交额）比没有这根更糟——
        # 它会让 `_measure` 的窗口计数够了，而算出来的量比少一天的分母。
        values = [finite(cell) for cell in cells[1:7]]
        if any(value is None for value in values):
            continue
        open_, high, low, close, volume, amount = values
        bars.append(
            {
                "date": cells[0],
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
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
