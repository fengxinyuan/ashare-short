"""集合竞价（09:25）盘口撮合与异动初筛。

在早盘 09:25 集合竞价撮合完成后，通过开盘价、竞价成交额与昨日盘面比对，
快速识别主力资金意图：竞价抢筹爆量、弱转强超预期、一字核按钮与强转弱出逃。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ashare_decision_support.numeric import finite


@dataclass(frozen=True)
class AuctionItem:
    """个股集合竞价原始数据包。"""

    symbol: str
    name: str
    open_price: float
    pre_close: float
    auction_amount: float  # 竞价成交额（元）
    prev_amount: float | None = None  # 昨日全天成交额（元）
    prev_streak: int = 0  # 昨日连板数
    prev_limit_up: bool = False  # 昨日是否涨停
    prev_change_pct: float | None = None  # 昨日涨跌幅（如 0.05 表示 +5%）


@dataclass(frozen=True)
class AuctionStockSignal:
    """单只标的的竞价研判结论。"""

    symbol: str
    name: str
    change_pct: float
    amount_ratio: float | None  # 竞价额占昨日全天额比例
    tags: tuple[str, ...]
    intensity: float  # 异动强度评分 0 ~ 100
    reason: str


@dataclass(frozen=True)
class AuctionScanResult:
    """全市场集合竞价扫描汇总。"""

    signals: list[AuctionStockSignal] = field(default_factory=list)
    market_avg_change: float = 0.0
    rush_bidding_count: int = 0  # 抢筹爆量家数
    nuclear_drop_count: int = 0  # 核按钮家数
    weak_to_strong_count: int = 0  # 弱转强家数
    strong_to_weak_count: int = 0  # 强转弱家数
    notes: list[str] = field(default_factory=list)

    def top_rush_stocks(self, top: int = 5) -> list[AuctionStockSignal]:
        """按异动强度排序筛选抢筹最激烈的标的。"""
        rush = [s for s in self.signals if "抢筹爆量" in s.tags or "弱转强" in s.tags]
        rush.sort(key=lambda s: s.intensity, reverse=True)
        return rush[:top]


def evaluate_auction_stock(item: AuctionItem) -> AuctionStockSignal:
    """按确定性规则研判单个标的的竞价异动。"""
    if item.pre_close <= 0:
        return AuctionStockSignal(
            symbol=item.symbol,
            name=item.name,
            change_pct=0.0,
            amount_ratio=None,
            tags=(),
            intensity=0.0,
            reason="昨收盘价无效",
        )

    change_pct = (item.open_price - item.pre_close) / item.pre_close
    ratio: float | None = None
    if item.prev_amount and item.prev_amount > 0:
        ratio = item.auction_amount / item.prev_amount

    tags: list[str] = []
    intensity = 50.0
    reasons: list[str] = []

    # 1. 抢筹爆量高开
    if change_pct >= 0.02 and ratio is not None and ratio >= 0.035:
        tags.append("抢筹爆量")
        reasons.append(f"高开 {change_pct * 100:.1f}% 且竞价成交达昨日 {ratio * 100:.1f}%")
        intensity += 25.0

    # 2. 弱转强超预期 (昨跌或炸板，今放量高开)
    is_prev_weak = (item.prev_change_pct is not None and item.prev_change_pct <= -0.02) or (
        item.prev_streak == 0 and not item.prev_limit_up
    )
    if is_prev_weak and change_pct >= 0.015 and (ratio or 0) >= 0.02:
        tags.append("弱转强")
        reasons.append(f"昨日收弱后竞价逆势高开 {change_pct * 100:.1f}%")
        intensity += 30.0

    # 3. 一字核按钮 (深度低开或逼近跌停)
    if change_pct <= -0.08:
        tags.append("一字核按钮")
        reasons.append(f"开盘深水计提 {change_pct * 100:.1f}%")
        intensity = 5.0

    # 4. 强转弱低开出逃 (昨强势/涨停，今大幅低开)
    elif (item.prev_limit_up or item.prev_streak > 0) and change_pct <= -0.02:
        tags.append("强转弱")
        reasons.append(f"昨日涨停/连板后竞价低开 {change_pct * 100:.1f}% 被资金抢跑")
        intensity = max(10.0, intensity - 30.0)

    # 5. 平开异常巨量
    if abs(change_pct) <= 0.008 and ratio is not None and ratio >= 0.06:
        tags.append("平开异动放量")
        reasons.append(f"窄幅平开但竞价异常爆量达昨日 {ratio * 100:.1f}%")

    reason_text = "；".join(reasons) if reasons else f"开盘涨幅 {change_pct * 100:.1f}%"
    clamped_intensity = max(0.0, min(100.0, intensity))

    return AuctionStockSignal(
        symbol=item.symbol,
        name=item.name,
        change_pct=round(change_pct, 4),
        amount_ratio=round(ratio, 4) if ratio is not None else None,
        tags=tuple(tags),
        intensity=round(clamped_intensity, 1),
        reason=reason_text,
    )


def scan_call_auction(items: list[AuctionItem]) -> AuctionScanResult:
    """全市场集合竞价结果批量扫描与情绪汇总。"""
    if not items:
        return AuctionScanResult(notes=["无集合竞价输入标的"])

    signals: list[AuctionStockSignal] = []
    total_change = 0.0
    valid_changes = 0
    rush_count = 0
    nuclear_count = 0
    weak_to_strong = 0
    strong_to_weak = 0

    for item in items:
        sig = evaluate_auction_stock(item)
        signals.append(sig)

        chg = finite(sig.change_pct)
        if chg is not None:
            total_change += chg
            valid_changes += 1

        if "抢筹爆量" in sig.tags:
            rush_count += 1
        if "一字核按钮" in sig.tags:
            nuclear_count += 1
        if "弱转强" in sig.tags:
            weak_to_strong += 1
        if "强转弱" in sig.tags:
            strong_to_weak += 1

    avg_change = round(total_change / valid_changes, 4) if valid_changes else 0.0
    notes = [
        f"全市场竞价扫描 {len(signals)} 只标的，平均溢价 {avg_change * 100:.2f}%",
        f"抢筹爆量 {rush_count} 只，弱转强 {weak_to_strong} 只",
        f"核按钮 {nuclear_count} 只，强转弱抢跑 {strong_to_weak} 只",
    ]

    return AuctionScanResult(
        signals=signals,
        market_avg_change=avg_change,
        rush_bidding_count=rush_count,
        nuclear_drop_count=nuclear_count,
        weak_to_strong_count=weak_to_strong,
        strong_to_weak_count=strong_to_weak,
        notes=notes,
    )
