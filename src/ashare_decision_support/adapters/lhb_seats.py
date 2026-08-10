"""龙虎榜席位：谁在买、谁在卖。

## 为什么单独一层

`news/akshare_source.py:fetch_dragon_tiger` 已经在取龙虎榜，但只取到个股层面的
**净买额**一个数，然后按板块汇总进 `sectors/predict.py` 的打分。净买额答不了超短
真正关心的那句话——**这笔钱是谁的**。

同样是净买 3 亿：
- 一个游资营业部单吃 3.1 亿，是**一个人的方向**，明天可能就走；
- 三个「机构专用」各买 1 亿，是**几家机构的建仓**，节奏完全不同；
- 北向专用买 3 亿，那是外资的被动/主动配置，和游资打板不是一回事。

席位构成把这三种情况分开，而净买额把它们压成同一个数。

## 数据源与可达性

走 `datacenter-web.eastmoney.com`，**与 `adapters/earnings.py` 同一台主机**。
2026-08-04 本机实测 `http=200 / 0.28s`；同一时刻 `push2his.eastmoney.com`
返回 `http=000`（被拒），与 `docs/DATA_SOURCES.md` 记录一致。所以这一层可以建，
而按单笔金额分档的资金流（`stock_individual_fund_flow`，push2his 系）不可以——
后者在同一次实测里退避重试 6 次才通过一次。

## 这一层只出事实

席位名称与净额是**交易所披露的事实**。「跟着某个游资买能赚钱」是**判断**，而且是
一条未经本系统台账验证的先验。所以这里：

- 记录席位身份、净额、占比，出可统计的结构化读数；
- **不给任何「游资看多 → 买入」的信号**，不进打分、不改仓位系数。

理由和 `adapters/broad_index.py` 那条一样：台账里的待兑现记录都是按现行公式算的，
现在改公式会让新旧记录不可比。等 `journal/stats.py` 攒够样本，「某类席位的方向
是否真的领先」本身就是一个可以被验证的假设——那时候再谈给分。

## 上榜是有门槛的

龙虎榜只收录触发交易所公开标准的票（涨跌幅偏离、振幅、换手、量比等）。**大市值
蓝筹基本不会出现。** 2026-08-04 实测：牧原股份（流通市值 1248 亿）在 7/1–8/4
整个区间**一次都没上榜**，而全市场同期有 2446 条记录。所以这一层对超短的小票池
有用，对权重股恒为空——取不到不是故障，`covered` 字段说明的就是这件事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.tabular import records

# akshare 里这一层唯一的入口函数。写成常量是因为它出现两次——`getattr` 的键和
# 取不到时的说明文案，两处写死必然漂（改了键忘了文案，读的人就会去查一个根本不
# 存在的函数名）。
#
# 原先这里还有一个 `LHB_SOURCE = "akshare.stock_lhb_stock_detail_em"` 的来源标签，
# 已删：`SeatRead` 没有任何 provenance 字段，那个标签**无处可去**，从定义那天起
# 就没有调用方。取数来源已经由下面的说明文案带出来了。
LHB_FUNCTION = "stock_lhb_stock_detail_em"

# 席位类别。前三类由名称精确判定，是交易所的固定称谓，不是猜的。
SEAT_INSTITUTION = "机构专用"
SEAT_NORTHBOUND = "北向资金"
SEAT_BRANCH = "营业部"

NORTHBOUND_MARKERS = ("沪股通专用", "深股通专用")

# 知名游资席位。**这是一份先验名单，不是从台账标定出来的**，也没有回测支持。
# 它的作用只有一个：把「某个营业部」变成「一个可追踪的对象」，让 `journal/` 有
# 东西可以累积。名单不全、会过时，所以命中与否都不改变任何数值读数，只加一个标注。
KNOWN_HOT_MONEY: tuple[str, ...] = (
    "国信证券股份有限公司深圳泰然九路证券营业部",
    "华鑫证券有限责任公司上海分公司",
    "中国银河证券股份有限公司北京中关村大街证券营业部",
    "东方财富证券股份有限公司拉萨东环路第二证券营业部",
    "中信证券股份有限公司上海溧阳路证券营业部",
    "国泰君安证券股份有限公司上海江苏路证券营业部",
    "招商证券股份有限公司深圳蛇口工业七路证券营业部",
    "光大证券股份有限公司深圳金田路证券营业部",
)

# 单一席位净额占当日成交额多大比例才算「集中」。0.05 = 5%，超短里这已经是
# 一个人明显在主导盘面的量级。先验设定。
CONCENTRATION_THRESHOLD = 0.05


@dataclass(frozen=True)
class Seat:
    name: str
    category: str
    buy: float | None = None
    sell: float | None = None
    net: float | None = None
    buy_share: float | None = None
    sell_share: float | None = None
    known_hot_money: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "buy": self.buy,
            "sell": self.sell,
            "net": self.net,
            "buy_share": self.buy_share,
            "sell_share": self.sell_share,
            "known_hot_money": self.known_hot_money,
        }


@dataclass
class SeatRead:
    """一只票在某个上榜日的席位构成。`covered=False` 表示当日未上榜。"""

    symbol: str
    trade_date: str
    covered: bool = False
    reason: str | None = None
    seats: list[Seat] = field(default_factory=list)
    net_by_category: dict[str, float] = field(default_factory=dict)
    top_buyer: Seat | None = None
    top_seller: Seat | None = None
    concentration: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "covered": self.covered,
            "reason": self.reason,
            "seats": [seat.to_dict() for seat in self.seats],
            "net_by_category": dict(self.net_by_category),
            "top_buyer": self.top_buyer.to_dict() if self.top_buyer else None,
            "top_seller": self.top_seller.to_dict() if self.top_seller else None,
            "concentration": self.concentration,
            "notes": list(self.notes),
        }

    def describe(self) -> list[str]:
        if not self.covered:
            return [f"{self.trade_date} 未上龙虎榜" + (f"（{self.reason}）" if self.reason else "")]
        lines = []
        for category, net in sorted(self.net_by_category.items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"{category}净额 {net / 1e4:+.0f} 万")
        if self.top_buyer and self.top_buyer.net:
            tag = "（知名游资席位）" if self.top_buyer.known_hot_money else ""
            lines.append(f"买方居首 {self.top_buyer.name}{tag} 净 {self.top_buyer.net / 1e4:+.0f} 万")
        if self.top_seller and self.top_seller.net:
            tag = "（知名游资席位）" if self.top_seller.known_hot_money else ""
            lines.append(f"卖方居首 {self.top_seller.name}{tag} 净 {self.top_seller.net / 1e4:+.0f} 万")
        if self.concentration is not None and self.concentration >= CONCENTRATION_THRESHOLD:
            lines.append(f"单一席位占当日成交 {self.concentration:.1%}，盘面由少数席位主导")
        lines.append("席位与净额是交易所披露的事实；「跟着某类席位做」是未经本系统验证的判断")
        lines.extend(self.notes)
        return lines


def classify_seat(name: str) -> tuple[str, bool]:
    """席位名称 → (类别, 是否知名游资)。名称为空时归到营业部，不猜。"""
    text = (name or "").strip()
    if not text:
        return SEAT_BRANCH, False
    if SEAT_INSTITUTION in text:
        return SEAT_INSTITUTION, False
    if any(marker in text for marker in NORTHBOUND_MARKERS):
        return SEAT_NORTHBOUND, False
    return SEAT_BRANCH, text in KNOWN_HOT_MONEY


class LhbSeatAdapter:
    """取某只票某个上榜日的买卖席位。任何一步失败都整体弃权，不抛。"""

    name = "lhb-seats"

    def __init__(self, ak_module: Any | None = None) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module  # type: ignore
            except ImportError:
                ak_module = None
        self.ak = ak_module

    def fetch(self, symbol: str, trade_date: date | str, market_amount: float | None = None) -> SeatRead:
        day = trade_date if isinstance(trade_date, str) else trade_date.strftime("%Y%m%d")
        day_key = day.replace("-", "")
        code = "".join(ch for ch in str(symbol) if ch.isdigit())[:6]
        read = SeatRead(symbol=symbol, trade_date=day_key)

        fn = getattr(self.ak, LHB_FUNCTION, None) if self.ak else None
        if fn is None:
            read.reason = f"AkShare 未提供 {LHB_FUNCTION}"
            return read

        merged = self._merge_rows(fn, code, day_key, read)
        if not merged:
            read.reason = read.reason or "当日未上榜或无席位明细"
            return read

        read.covered = True
        read.seats = sorted(
            (_build_seat(name, row) for (name, *_), row in merged.items()),
            key=lambda s: -(s.net or 0.0),
        )

        totals: dict[str, float] = {}
        for seat in read.seats:
            if seat.net is None:
                continue
            totals[seat.category] = totals.get(seat.category, 0.0) + seat.net
        read.net_by_category = {k: round(v, 2) for k, v in totals.items()}

        self._fill_extremes(read, market_amount)
        return read

    def _merge_rows(self, fn, code: str, day_key: str, read: SeatRead) -> dict[tuple, dict[str, Any]]:
        """买入榜 + 卖出榜合并去重。

        同一席位会同时出现在两张榜上，两边**整行数值完全一致**，不去重的话它的
        净额会被算两遍。

        但去重键不能只用名称：**「机构专用」是交易所对所有机构席位的统一匿名称谓**，
        同一天出现四行「机构专用」是四家不同的机构。按名称去重会把它们静默合并成
        一家，净额直接算错。所以用整行做键。
        """
        merged: dict[tuple, dict[str, Any]] = {}
        for flag in ("买入", "卖出"):
            try:
                rows = records(fn(symbol=code, date=day_key, flag=flag))
            except Exception as exc:
                read.notes.append(f"{flag}席位取数失败：{type(exc).__name__}")
                continue
            for row in rows:
                seat_name = str(row.get("交易营业部名称") or "").strip()
                if not seat_name:
                    continue
                key = (
                    seat_name,
                    finite(row.get("买入金额")),
                    finite(row.get("卖出金额")),
                    finite(row.get("净额")),
                )
                merged.setdefault(key, row)
        return merged

    def _fill_extremes(self, read: SeatRead, market_amount: float | None) -> None:
        with_net = [s for s in read.seats if s.net is not None]
        if not with_net:
            return
        read.top_buyer = with_net[0] if (with_net[0].net or 0) > 0 else None
        read.top_seller = with_net[-1] if (with_net[-1].net or 0) < 0 else None
        biggest = max(with_net, key=lambda s: abs(s.net or 0.0))
        if market_amount and market_amount > 0:
            read.concentration = round(abs(biggest.net or 0.0) / market_amount, 4)
            return
        # 没有当日成交额时退回用服务端给的占比，口径不同，写进 notes。
        share = max(biggest.buy_share or 0.0, biggest.sell_share or 0.0)
        if share > 0:
            read.concentration = share
            read.notes.append("集中度用服务端「占总成交比例」，与按成交额现算口径略有差异")


def _build_seat(seat_name: str, row: dict[str, Any]) -> Seat:
    category, hot = classify_seat(seat_name)
    buy = finite(row.get("买入金额"))
    sell = finite(row.get("卖出金额"))
    net = finite(row.get("净额"))
    if net is None and buy is not None and sell is not None:
        net = buy - sell
    return Seat(
        name=seat_name,
        category=category,
        buy=buy,
        sell=sell,
        net=net,
        buy_share=finite(row.get("买入金额-占总成交比例")),
        sell_share=finite(row.get("卖出金额-占总成交比例")),
        known_hot_money=hot,
    )
