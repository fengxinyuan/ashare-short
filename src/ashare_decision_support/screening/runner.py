"""把适配性筛选跑到一个池子上，并把结果压成人能读的报告。

`suitability.py` 是纯函数、不碰网络；这一层负责拉数据、分批、汇总。分开的理由和
`signals/` 与 `jobs/` 的分工一样：算法要能拿构造数据单测，不能拖着适配器跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.screening.suitability import (
    ScreenConfig,
    SuitabilityCard,
    screen_many,
)

# 一次拉多少只。和 `TickFlowAdapter.get_historical_bars_batch` 的默认值保持一致，
# 那边有实测数据说明为什么不能调大（大批次静默丢标的）。
FETCH_CHUNK = 100

# 默认回看窗口。两年是「样本外切出 30% 后还剩半年」的最小值，再短样本外没意义。
DEFAULT_LOOKBACK_DAYS = 730


@dataclass
class ScreenReport:
    """一轮筛选的完整结果。

    `funnel` 是这份报告最该先看的东西：它告诉你票死在哪一层。如果九成标的死在
    同一层，那多半是**阈值定错了**而不是市场没机会——一个把全市场筛空的漏斗和
    一个放行一切的漏斗，信息量同样是零。
    """

    trade_date: date
    universe: int = 0
    fetched: int = 0
    funnel: dict[str, int] = field(default_factory=dict)
    passed: list[SuitabilityCard] = field(default_factory=list)
    unstable: list[SuitabilityCard] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self, top: int = 30) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "universe": self.universe,
            "fetched": self.fetched,
            "funnel": self.funnel,
            "passed": [card.to_dict() for card in self.passed[:top]],
            "unstable": [card.to_dict() for card in self.unstable[:top]],
            "notes": self.notes,
        }

    def describe(self, top: int = 15) -> list[str]:
        """给人看的摘要。数字全部来自卡片，这里不做任何新计算。"""
        lines = [
            f"筛选池 {self.universe} 只，取到日线 {self.fetched} 只",
            "漏斗： " + " → ".join(f"{layer} 淘汰 {count}" for layer, count in self.funnel.items()),
            f"通过 {len(self.passed)} 只，样本外不稳 {len(self.unstable)} 只",
        ]
        if not self.passed:
            lines.append(
                "本轮无标的通过。这不是异常——五层里任何一层都可以合理地筛空全市场，"
                "先看漏斗判断是阈值问题还是市场问题。"
            )
        for card in self.passed[:top]:
            lines.append(
                f"  {card.symbol}  "
                f"样本外中位 {_pct(card.rank_key)}  "
                f"胜率 {_pct(card.consistency.win_rate)}  "
                f"衰减 {_ratio(card.decay)}  "
                f"择时溢价 {_pct(card.capture.timing_premium)}  "
                f"可执行占比 {_ratio(card.capture.executable_ratio)}"
            )
        lines.extend(self.notes)
        return lines


def run_screen(
    adapter: Any,
    symbols: list[str],
    *,
    end: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    config: ScreenConfig | None = None,
) -> ScreenReport:
    """在 `symbols` 上跑一轮筛选。

    取不到日线的标的**不计入漏斗**，只记在 `fetched` 与 `universe` 的差里：
    「没数据」和「数据说它不行」是两件事，混在一起会让漏斗读数失真。
    """
    cfg = config or ScreenConfig()
    end_date = end or date.today()
    start_date = end_date - timedelta(days=lookback_days)

    bars_by_symbol = _fetch(adapter, symbols, start_date, end_date)
    usable = {symbol: bars for symbol, bars in bars_by_symbol.items() if bars}

    cards = screen_many(usable, cfg)

    report = ScreenReport(
        trade_date=end_date,
        universe=len(symbols),
        fetched=len(usable),
        funnel=_funnel(cards),
        passed=[card for card in cards if card.verdict == "pass"],
        unstable=[card for card in cards if card.verdict == "unstable"],
    )

    missing = len(symbols) - len(usable)
    if missing:
        report.notes.append(f"{missing} 只未取到日线，已排除在漏斗之外")
    report.notes.append(
        "口径提醒：`oracle` 列是事后视角（当日最低买、次日最高卖），不可执行，"
        "只作理论上限；决策看 `neutral`／`strict` 两列与样本外衰减。"
    )
    return report


def _fetch(
    adapter: Any, symbols: list[str], start: date, end: date
) -> dict[str, list[dict]]:
    """分批拉日线。单只失败不影响整批。"""
    batch = getattr(adapter, "get_historical_bars_batch", None)
    out: dict[str, list[dict]] = {}

    for index in range(0, len(symbols), FETCH_CHUNK):
        chunk = symbols[index : index + FETCH_CHUNK]
        try:
            if callable(batch):
                out.update(batch(chunk, start, end))
            else:
                out.update(adapter.get_historical_bars(chunk, start, end))
        except Exception:  # noqa: BLE001 - 一批失败不该让几千只的筛选整轮丢掉
            for symbol in chunk:
                out.setdefault(symbol, [])
    return out


def _funnel(cards: list[SuitabilityCard]) -> dict[str, int]:
    """每层淘汰了多少只。键的顺序就是漏斗顺序，靠 dict 插入序保证。"""
    layers = ("data", "tradability", "capture", "consistency", "behavior")
    counts = {layer: 0 for layer in layers}
    for card in cards:
        if card.rejected_at in counts:
            counts[card.rejected_at] += 1
    counts["holdout"] = sum(1 for card in cards if card.verdict == "unstable")
    counts["pass"] = sum(1 for card in cards if card.verdict == "pass")
    return counts


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"
