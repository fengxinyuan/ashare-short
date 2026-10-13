"""海外指数日线历史，用于板块情景的历史基准率研究。

新浪美股日线采用美东日期，亚太日线采用当地日期，统一存为 YYYYMMDD。
只保证可用的 OHLC，缺失成交量或成交额保留 None。
海外 T 日读数对齐 A 股下一交易日，保证决策时已知；ALIGNMENT_NOTE 随结果输出。
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.http import (
    BROWSER_USER_AGENT,
    SINA_REFERER,
    fetch_text,
    jsonp_payload,
)
from ashare_decision_support.fanout import gather
from ashare_decision_support.jsoncache import merge_series, read_series
from ashare_decision_support.numeric import finite
from ashare_decision_support.state.paths import state_home

SOURCE_US = "sina-usstock"
SOURCE_ASIA = "sina-global"

US_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/"
    "var%20_ashare_ds/US_MinKService.getDailyK?symbol={symbol}"
)
# `num` 实测被服务端封顶（传 5 与传 10000 都回 1000 根），照传是为了让上限来自
# 对端而不是来自这里——哪天它放开了，这一路自动多拿。
ASIA_URL = "https://gi.finance.sina.com.cn/hq/daily?symbol={symbol}&num=10000"

HISTORY_DIR_NAME = "overseas_index"
HISTORY_SCHEMA = "overseas-index-series-v1"

TIMEOUT_SECONDS = 20
MAX_WORKERS = 6

# 这句话跟着读数一起走，理由见模块 docstring。
ALIGNMENT_NOTE = (
    "海外读数按「T 日收盘 → A 股下一个交易日」对齐："
    "美股 T 日收在北京 T+1 凌晨，亚太 T 日收在北京 T 日下午——"
    "两者对 A 股当日开盘都属于已知，对当日收盘都不该当成同日信号"
)

_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


@dataclass(frozen=True)
class OverseasInstrument:
    """一个海外品种：取哪、叫什么、归哪一路解析、以及**它相对 A 股收盘的时点**。

    `after_a_share_close` 是三者里唯一影响数字的：它决定「这条读数出来之后，
    A 股第一个能反应的交易日是哪一天」。美股 T 日收在北京 T+1 凌晨，所以 A 股
    要到 T+1 才反应；亚太 T 日收在北京 T 日下午（韩 14:30、台 13:30），A 股当天
    尾盘就已经在反应了。差一天，基准率就完全是另一个数——而且不会报错。
    """

    key: str
    name: str
    symbol: str
    source: str
    after_a_share_close: bool


INSTRUMENTS: dict[str, OverseasInstrument] = {
    item.key: item
    for item in (
        OverseasInstrument("sp500", "标普500", ".INX", SOURCE_US, True),
        OverseasInstrument("nasdaq", "纳斯达克", ".IXIC", SOURCE_US, True),
        # 费半是这张表里对 A 股映射最直接的一个，也是唯一有独立产业含义的：
        # 标普/纳指答的是风险偏好，费半答的是半导体产业链本身。
        OverseasInstrument("sox", "费城半导体", ".SOX", SOURCE_US, True),
        OverseasInstrument("kospi", "KOSPI", "KOSPI", SOURCE_ASIA, False),
        OverseasInstrument("kosdaq", "KOSDAQ", "KOSDAQ", SOURCE_ASIA, False),
        OverseasInstrument("nikkei", "日经225", "NKY", SOURCE_ASIA, False),
        # 台湾加权里台积电权重极高，它与费半是两个**不同时段**的同一条产业链
        # 读数——台指收在北京 13:30，费半收在北京次日凌晨。
        OverseasInstrument("taiex", "台湾加权", "TWJQ", SOURCE_ASIA, False),
    )
}


@dataclass
class OverseasHistoryResult:
    """缓存覆盖与本次保存结果。ok 表示有历史可读，更新是否成功另列。"""

    keys: tuple[str, ...] = ()
    bars_after: dict[str, int] = field(default_factory=dict)
    earliest: dict[str, str] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)
    failures: int = 0
    notes: list[str] = field(default_factory=list)
    updated_keys: tuple[str, ...] = ()
    write_failures: int = 0

    @property
    def ok(self) -> bool:
        return any(self.bars_after.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "keys": list(self.keys),
            "bars_after": dict(self.bars_after),
            "earliest": dict(self.earliest),
            "latest": dict(self.latest),
            "failures": self.failures,
            "updated_keys": list(self.updated_keys),
            "write_failures": self.write_failures,
            "alignment": ALIGNMENT_NOTE,
            "notes": list(self.notes),
        }


class OverseasHistoryAdapter:
    """海外指数日线历史。两个新浪端点，都不限流，不占 TickFlow 配额。"""

    name = "overseas-history"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        max_workers: int = MAX_WORKERS,
        history_dir: str | Path | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_workers = max_workers
        self._history_dir = Path(history_dir) if history_dir else None

    def history_dir(self) -> Path:
        return (
            self._history_dir if self._history_dir is not None else state_home() / HISTORY_DIR_NAME
        )

    def refresh(self, keys: Iterable[str] | None = None) -> OverseasHistoryResult:
        """取回并**并进**已有序列。

        并而不是覆盖，同 `ths_sector_index._write_series` 的理由：亚太那一路只回
        最近 1000 根，跑够四年之后覆盖会把最早那段抹掉，而那段正是长窗口基准率
        要用的部分。
        """
        requested = list(dict.fromkeys(INSTRUMENTS if keys is None else keys))
        wanted = [key for key in requested if key in INSTRUMENTS]
        unknown = [key for key in requested if key not in INSTRUMENTS]
        if not wanted:
            return OverseasHistoryResult(
                notes=[
                    f"没有可取的海外品种（未知：{'、'.join(unknown)}）"
                    if unknown
                    else "没有可取的海外品种"
                ]
            )

        done, errors = gather(
            [INSTRUMENTS[key] for key in wanted], self._fetch_one, max_workers=self.max_workers
        )
        result = OverseasHistoryResult(keys=tuple(wanted), failures=len(errors))
        self._persist_history(done, result)
        root = self.history_dir()
        for key in wanted:
            stored = load_series(key, root)
            if not stored:
                continue
            result.bars_after[key] = len(stored)
            result.earliest[key] = str(stored[0]["date"])
            result.latest[key] = str(stored[-1]["date"])

        covered = "、".join(
            f"{INSTRUMENTS[key].name} {result.bars_after[key]} 根"
            f"（{result.earliest[key]}..{result.latest[key]}）"
            for key in wanted
            if key in result.bars_after
        )
        result.notes.append(
            f"海外指数历史缓存：{covered}" if covered else "海外指数历史：没有可用缓存"
        )
        if unknown:
            result.notes.append(f"未知海外品种已跳过：{'、'.join(unknown)}")
        if errors:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            result.notes.append(
                f"{len(errors)}/{len(wanted)} 个品种取数失败（{kinds}），已落盘的序列不受影响"
            )
        return result

    def _persist_history(
        self,
        fetched: list[tuple[OverseasInstrument, list[dict]]],
        result: OverseasHistoryResult,
    ) -> None:
        updated: list[str] = []
        failed: list[str] = []
        empty: list[str] = []
        root = self.history_dir()
        for instrument, bars in fetched:
            if not bars:
                empty.append(instrument.key)
            elif _write_series(root, instrument, bars):
                updated.append(instrument.key)
            else:
                failed.append(instrument.key)
        result.updated_keys = tuple(updated)
        result.write_failures = len(failed)
        if failed:
            result.notes.append(f"海外历史保存失败：{'、'.join(failed)}；原有历史保留，可重试")
        if empty:
            result.notes.append(f"海外品种未返回可用日线：{'、'.join(empty)}；原有历史保留")

    def _fetch_one(self, instrument: OverseasInstrument) -> list[dict]:
        if instrument.source == SOURCE_US:
            text = fetch_text(
                self._opener,
                US_URL.format(symbol=instrument.symbol),
                timeout=self.timeout,
                referer=SINA_REFERER,
                user_agent=BROWSER_USER_AGENT,
            )
            return _parse_us(text)
        text = fetch_text(
            self._opener,
            ASIA_URL.format(symbol=instrument.symbol),
            timeout=self.timeout,
            referer=SINA_REFERER,
            user_agent=BROWSER_USER_AGENT,
        )
        return _parse_asia(text)


def _parse_us(text: str) -> list[dict]:
    """``var _ashare_ds([{"d":"2026-08-19","o":...}]);`` -> 日线列表。"""
    return _bars_from(jsonp_payload(text))


def _parse_asia(text: str) -> list[dict]:
    """``{"code":0,"result":{"data":[{"d":"2026-08-20",...}]}}`` -> 日线列表。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    rows = (
        ((payload.get("result") or {}).get("data"))
        if isinstance(payload.get("result"), dict)
        else None
    )
    return _bars_from(rows)


def _bars_from(rows: Any) -> list[dict]:
    """两路共用的取值：按**键名**取，不按位置。

    亚太那一路的键序是 `d,v,c,o,l,h`，美股是 `d,o,h,l,c,v,a`——按位置取会把最高价
    读成收盘价，而两边都是合法数字，任何一层都不会报错。
    """
    if not isinstance(rows, list):
        return []
    bars: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = _normalize_date(row.get("d"))
        close = finite(row.get("c"))
        if stamp is None or close is None:
            # 没有日期就排不进序，没有收盘就算不出收益——这两样缺一根就整根丢掉。
            continue
        bars.append(
            {
                "date": stamp,
                "open": finite(row.get("o")),
                "high": finite(row.get("h")),
                "low": finite(row.get("l")),
                "close": close,
                # 亚太那一路个别品种的成交量恒为 0，`finite` 会原样保留它。
                # 这一层不判断 0 是「没有成交」还是「没有这个读数」——没有调用方
                # 读它，硬猜只会造出一个假读数。
                "volume": finite(row.get("v")),
            }
        )
    bars.sort(key=lambda bar: bar["date"])
    return bars


def _normalize_date(value: Any) -> str | None:
    """`2026-08-19` / `20260819` -> `20260819`。对不上格式就返回 None。"""
    match = _DATE.match(str(value or "").strip())
    return "".join(match.groups()) if match else None


# 序列的读写


def _series_path(root: Path, key: str) -> Path:
    return root / f"{key}.json"


def _write_series(root: Path, instrument: OverseasInstrument, bars: list[dict]) -> bool:
    """串行合并同一品种的历史；锁或保存失败不覆盖旧序列。"""
    header = {
        "key": instrument.key,
        "name": instrument.name,
        "symbol": instrument.symbol,
        "source": instrument.source,
    }
    return merge_series(
        _series_path(root, instrument.key), HISTORY_SCHEMA, bars, lambda _stored: header
    )


def load_series(key: str, root: str | Path | None = None) -> list[dict]:
    """读回一个海外品种的日线，按日期升序。没有就返回空列表。"""
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    return read_series(_series_path(directory, str(key)), HISTORY_SCHEMA)


def load_all_series(root: str | Path | None = None) -> dict[str, list[dict]]:
    """读回全部已落盘的海外序列，`{key: 日线}`。

    目录不存在就返回空字典：第一次 `overseas-refresh` 之前本来就没有历史，那是
    预期状态而不是错误——驱动层会把「没有历史」和「有历史但样本不足」分开报。
    """
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    result: dict[str, list[dict]] = {}
    for key in INSTRUMENTS:
        bars = load_series(key, directory)
        if bars:
            result[key] = bars
    return result


def daily_returns(bars: list[dict]) -> dict[str, float]:
    """`{交易日: 当日涨跌幅}`。第一根没有前收，不进表。

    收益按**收盘对收盘**算，与板块序列同口径。海外指数没有涨跌停，也不做除权，
    所以这里不需要复权因子那一层。
    """
    returns: dict[str, float] = {}
    previous: float | None = None
    for bar in bars:
        close = finite(bar.get("close"))
        if close is not None and previous is not None and previous > 0:
            returns[str(bar["date"])] = close / previous - 1.0
        if close is not None and close > 0:
            previous = close
    return returns
