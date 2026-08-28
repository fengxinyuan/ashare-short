"""海外指数日线**历史**：A 股板块假设的外部驱动源。

**为什么要历史，而不是只要一个当日快照。** `adapters/global_market.py` 每天取一次
美股/韩股收盘，那份读数进盘前简报的一段文字，然后就没了。于是「费半跌了 2%」在
系统里是个孤立事实——它答不了下一个必然被问到的问题：**过去费半跌 2% 之后，A 股
电子板块第二天到底怎么走。**

那个问题只能查，不能推。查它要两段序列：A 股板块指数（`ths_sector_index.py` 已在
落盘，并可按年回填到 2007）和海外指数——后者此前一根都没存过。

## 两个源，两种格式

- **美股**：新浪美股日线。2026-08-20 实测 `.INX` 5696 根回到 2004-01-02、
  `.IXIC` 5693 根、`.SOX` 3165 根回到 2014-01-16。日期是**美东日期**。
- **亚太**：新浪环球指数日线。实测 KOSPI / KOSDAQ / 日经 / 台湾加权各 1000 根，
  回到 2022-07。日期是**当地日期**。亚太这一路**没有成交额**，个别品种连成交量
  都是 0——所以这一层只承诺 OHLC，量能字段取到就存、取不到就是 `None`。

两路都归一成 `YYYYMMDD`，与板块序列同一种日期写法。不归一的话，基准率层要在
`"2026-08-19"` 和 `"20260819"` 之间做一次隐式转换，而那种转换写错了不会报错，
只会让两边永远对不上、样本数恒为 0。

## 对齐规则：海外 T 日 → A 股的**下一个**交易日

这一条决定基准率是不是错的，所以写在取数层而不是使用层——`ALIGNMENT_NOTE` 会
跟着读数一起进事实包。统一按「A 股这根日线**开盘之前**，这条海外读数已经出来了
吗」判：

- 美股 T 日收盘 = 北京 T+1 凌晨，A 股 T+1 开盘前已知；
- 亚太 T 日收盘 = 北京 T 日 14:30（韩）/ 13:30（台），**晚于 A 股当日多数建仓
  时点**，所以对次日决策同样只能算到 T+1。

按「同日」对齐会拿到一个偷看未来的结果——韩股当天怎么走，在 A 股当天开盘时还
不知道。这类错不会报错，只会让基准率好看得离谱。
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.http import BROWSER_USER_AGENT, SINA_REFERER, fetch_text
from ashare_decision_support.fanout import gather
from ashare_decision_support.jsoncache import read_stamped, write_stamped
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

_CALLBACK = re.compile(r"\((.*)\)\s*;?\s*$", re.DOTALL)
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
    """一次取数的战果。产物是序列文件，这里只交代覆盖到了什么。"""

    keys: tuple[str, ...] = ()
    bars_after: dict[str, int] = field(default_factory=dict)
    earliest: dict[str, str] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)
    failures: int = 0
    notes: list[str] = field(default_factory=list)

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
            self._history_dir
            if self._history_dir is not None
            else state_home() / HISTORY_DIR_NAME
        )

    def refresh(self, keys: Iterable[str] | None = None) -> OverseasHistoryResult:
        """取回并**并进**已有序列。

        并而不是覆盖，同 `ths_sector_index._write_series` 的理由：亚太那一路只回
        最近 1000 根，跑够四年之后覆盖会把最早那段抹掉，而那段正是长窗口基准率
        要用的部分。
        """
        wanted = [key for key in (keys or INSTRUMENTS) if key in INSTRUMENTS]
        unknown = [key for key in (keys or ()) if key not in INSTRUMENTS]
        if not wanted:
            return OverseasHistoryResult(
                notes=[f"没有可取的海外品种（未知：{'、'.join(unknown)}）" if unknown else "没有可取的海外品种"]
            )

        done, errors = gather(
            [INSTRUMENTS[key] for key in wanted], self._fetch_one, max_workers=self.max_workers
        )
        root = self.history_dir()
        for instrument, bars in done:
            if bars:
                _write_series(root, instrument, bars)

        result = OverseasHistoryResult(keys=tuple(wanted), failures=len(errors))
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
        result.notes.append(f"海外指数历史：{covered}" if covered else "海外指数历史：一根都没取到")
        if unknown:
            result.notes.append(f"未知海外品种已跳过：{'、'.join(unknown)}")
        if errors:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            result.notes.append(
                f"{len(errors)}/{len(wanted)} 个品种取数失败（{kinds}），已落盘的序列不受影响"
            )
        return result

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


# ---- parsing -------------------------------------------------------------


def _parse_us(text: str) -> list[dict]:
    """``var _ashare_ds([{"d":"2026-08-19","o":...}]);`` -> 日线列表。"""
    match = _CALLBACK.search(text.strip())
    if not match:
        return []
    try:
        rows = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return _bars_from(rows)


def _parse_asia(text: str) -> list[dict]:
    """``{"code":0,"result":{"data":[{"d":"2026-08-20",...}]}}`` -> 日线列表。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    rows = ((payload.get("result") or {}).get("data")) if isinstance(payload.get("result"), dict) else None
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


# ---- 序列的读写 -----------------------------------------------------------


def _series_path(root: Path, key: str) -> Path:
    return root / f"{key}.json"


def _write_series(root: Path, instrument: OverseasInstrument, bars: list[dict]) -> bool:
    if not bars:
        return False
    merged = {str(bar["date"]): bar for bar in load_series(instrument.key, root)}
    merged.update({str(bar["date"]): bar for bar in bars})
    return write_stamped(
        _series_path(root, instrument.key),
        {
            "key": instrument.key,
            "name": instrument.name,
            "symbol": instrument.symbol,
            "source": instrument.source,
            "bars": [merged[stamp] for stamp in sorted(merged)],
        },
        HISTORY_SCHEMA,
    )


def load_series(key: str, root: str | Path | None = None) -> list[dict]:
    """读回一个海外品种的日线，按日期升序。没有就返回空列表。"""
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    payload = read_stamped(_series_path(directory, str(key)), HISTORY_SCHEMA)
    bars = (payload or {}).get("bars")
    if not isinstance(bars, list):
        return []
    return [bar for bar in bars if isinstance(bar, dict) and bar.get("date")]


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
