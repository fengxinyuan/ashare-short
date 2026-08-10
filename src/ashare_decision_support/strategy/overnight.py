"""Configurable end-of-day screens for next-session observation.

This is deliberately a deterministic candidate generator, not a prediction
engine.  A profile tells the scanner what *shape* to look for; it does not
claim a hit rate or manufacture an expected return.  The output is useful to
the AI layer because every accepted symbol carries its raw measurements and
the exact profile that admitted it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from math import log10
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.signals.candles import close_position
from ashare_decision_support.signals.daily_technical import DailyTechnicalAnalyzer
from ashare_decision_support.signals.dipbuy import measure_dip
from ashare_decision_support.symbols import normalize_symbol


@dataclass(frozen=True)
class StrategyProfile:
    """A fixed set of shape constraints for an overnight observation style."""

    name: str
    label: str
    description: str
    min_atr_pct: float
    max_atr_pct: float
    min_pullback_pct: float
    max_pullback_pct: float
    min_range_position: float
    max_range_position: float
    min_close_vs_ma20_pct: float
    max_volume_contraction: float
    min_volume_ratio: float
    max_volume_ratio: float
    min_close_position: float
    max_close_position: float


PROFILES: dict[str, StrategyProfile] = {
    "balanced-pullback": StrategyProfile(
        name="balanced-pullback",
        label="均衡回踩",
        description="中等波动、缩量回撤后的次日观察，目标是保留流动性与结构。",
        min_atr_pct=0.01,
        max_atr_pct=0.04,
        min_pullback_pct=-0.16,
        max_pullback_pct=-0.025,
        min_range_position=0.10,
        max_range_position=0.65,
        min_close_vs_ma20_pct=-0.02,
        max_volume_contraction=1.05,
        min_volume_ratio=0.50,
        max_volume_ratio=1.80,
        min_close_position=0.00,
        max_close_position=0.86,
    ),
    "high-elasticity": StrategyProfile(
        name="high-elasticity",
        label="高弹性反转",
        description="大波动标的的深回撤观察，强调成交额和当日承接，风险也更高。",
        min_atr_pct=0.04,
        max_atr_pct=0.09,
        min_pullback_pct=-0.25,
        max_pullback_pct=-0.06,
        min_range_position=0.08,
        max_range_position=0.55,
        min_close_vs_ma20_pct=-0.04,
        max_volume_contraction=1.05,
        min_volume_ratio=0.50,
        max_volume_ratio=2.50,
        min_close_position=0.25,
        max_close_position=0.90,
    ),
    "breakout-continuation": StrategyProfile(
        name="breakout-continuation",
        label="突破延续",
        description="接近阶段高位且量能确认的趋势观察，不适用于低位筛选。",
        min_atr_pct=0.02,
        max_atr_pct=0.06,
        min_pullback_pct=-0.03,
        max_pullback_pct=0.0,
        min_range_position=0.82,
        max_range_position=1.00,
        min_close_vs_ma20_pct=0.0,
        max_volume_contraction=2.80,
        min_volume_ratio=1.20,
        max_volume_ratio=2.80,
        min_close_position=0.70,
        max_close_position=1.00,
    ),
}


@dataclass(frozen=True)
class StrategyScreenConfig:
    profile: str = "high-elasticity"
    price_min: float = 3.0
    price_max: float = 60.0
    min_amount_yi: float = 5.0
    # ST/退市风险标的默认纳入；风险由候选 warnings 明示，调用方可显式关闭。
    allow_st: bool = True
    main_board_only: bool = True
    limit: int = 30
    min_history_bars: int = 65

    # Optional overrides keep the CLI expressive without multiplying profiles.
    min_atr_pct: float | None = None
    max_atr_pct: float | None = None
    min_range_position: float | None = None
    max_range_position: float | None = None
    min_pullback_pct: float | None = None
    max_pullback_pct: float | None = None
    min_close_vs_ma20_pct: float | None = None
    max_volume_contraction: float | None = None
    min_volume_ratio: float | None = None
    max_volume_ratio: float | None = None
    min_close_position: float | None = None
    max_close_position: float | None = None

    def resolved_profile(self) -> StrategyProfile:
        if self.price_min < 0 or self.price_max <= 0 or self.price_min > self.price_max:
            raise ValueError("price range must satisfy 0 <= price_min <= price_max")
        if self.min_amount_yi < 0:
            raise ValueError("min_amount_yi must be non-negative")
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if self.min_history_bars < 20:
            raise ValueError("min_history_bars must be at least 20")
        base = PROFILES.get(self.profile)
        if base is None:
            raise ValueError(f"unknown strategy profile {self.profile!r}; choose one of {sorted(PROFILES)}")
        values = {
            key: value
            for key, value in {
                "min_atr_pct": self.min_atr_pct,
                "max_atr_pct": self.max_atr_pct,
                "min_range_position": self.min_range_position,
                "max_range_position": self.max_range_position,
                "min_pullback_pct": self.min_pullback_pct,
                "max_pullback_pct": self.max_pullback_pct,
                "min_close_vs_ma20_pct": self.min_close_vs_ma20_pct,
                "max_volume_contraction": self.max_volume_contraction,
                "min_volume_ratio": self.min_volume_ratio,
                "max_volume_ratio": self.max_volume_ratio,
                "min_close_position": self.min_close_position,
                "max_close_position": self.max_close_position,
            }.items()
            if value is not None
        }
        resolved = replace(base, **values)
        if resolved.min_atr_pct < 0 or resolved.max_atr_pct < resolved.min_atr_pct:
            raise ValueError("ATR bounds must satisfy 0 <= min_atr_pct <= max_atr_pct")
        if resolved.min_range_position < 0 or resolved.max_range_position > 1 or resolved.max_range_position < resolved.min_range_position:
            raise ValueError("range position bounds must be within [0, 1]")
        if resolved.min_volume_ratio < 0 or resolved.max_volume_ratio < resolved.min_volume_ratio:
            raise ValueError("volume ratio bounds are invalid")
        if resolved.min_pullback_pct > resolved.max_pullback_pct:
            raise ValueError("pullback bounds are invalid")
        if resolved.min_close_position < 0 or resolved.max_close_position > 1 or resolved.max_close_position < resolved.min_close_position:
            raise ValueError("close position bounds must be within [0, 1]")
        return resolved


@dataclass(frozen=True)
class StrategyCandidate:
    symbol: str
    name: str | None
    sector: str | None
    profile: str
    profile_label: str
    setup_rank: float
    rank_components: dict[str, float]
    signal_date: str | None
    last_close: float
    signal_low: float | None
    previous_low: float | None
    amount_yi: float | None
    atr_pct: float
    range_position_60d: float
    pullback_pct: float
    close_vs_ma20_pct: float
    close_position: float
    volume_contraction: float
    volume_ratio_20d: float
    pullback_atr: float | None
    down_days: int | None
    is_st: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyScreenResult:
    config: StrategyScreenConfig
    profile: StrategyProfile
    candidates: list[StrategyCandidate] = field(default_factory=list)
    scanned: int = 0
    with_bars: int = 0
    excluded_scope: int = 0
    excluded_st: int = 0
    rejected_shape: int = 0
    rejected_data: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ashare-strategy-screen-v1",
            "profile": asdict(self.profile),
            "config": asdict(self.config),
            "summary": {
                "scanned": self.scanned,
                "with_bars": self.with_bars,
                "excluded_scope": self.excluded_scope,
                "excluded_st": self.excluded_st,
                "rejected_shape": self.rejected_shape,
                "rejected_data": self.rejected_data,
                "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
                "candidates": len(self.candidates),
            },
            "candidates": [item.to_dict() for item in self.candidates],
            "notes": list(self.notes),
        }


def is_main_board(symbol: str) -> bool:
    """Return whether a normalized A-share symbol belongs to SH/SZ main board."""
    code, _, market = normalize_symbol(symbol).partition(".")
    if market == "SH":
        return code.startswith(("600", "601", "603", "605"))
    if market == "SZ":
        return code.startswith(("000", "001", "002", "003"))
    return False


def is_st_name(name: str | None) -> bool:
    label = (name or "").replace(" ", "").upper()
    return "ST" in label or "退" in label


def screen_overnight(  # noqa: PLR0912 - each branch is an auditable funnel gate
    bars_by_symbol: dict[str, list[dict]],
    *,
    names: dict[str, str | None] | None = None,
    sectors: dict[str, str | None] | None = None,
    config: StrategyScreenConfig | None = None,
) -> StrategyScreenResult:
    """Screen already fetched bars; no network access and no hidden defaults.

    ``amount`` is read from the signal-day daily bar so the liquidity rule and
    the OHLC structure always refer to the same trading session.
    """
    cfg = config or StrategyScreenConfig()
    profile = cfg.resolved_profile()
    normalized_names = {
        normalized: value
        for key, value in (names or {}).items()
        if (normalized := normalize_symbol(key))
    }
    normalized_sectors = {
        normalized: value
        for key, value in (sectors or {}).items()
        if (normalized := normalize_symbol(key))
    }
    normalized_bars: dict[str, list[dict]] = {}
    for raw_symbol, rows in bars_by_symbol.items():
        symbol = normalize_symbol(raw_symbol)
        if symbol and symbol not in normalized_bars:
            normalized_bars[symbol] = _ordered_bars(rows)
    result = StrategyScreenResult(config=cfg, profile=profile, scanned=len(normalized_bars))
    eligible_bars: dict[str, list[dict]] = {}
    for symbol, rows in normalized_bars.items():
        name = normalized_names.get(symbol)
        if cfg.main_board_only and not is_main_board(symbol):
            result.excluded_scope += 1
            continue
        if not cfg.allow_st and is_st_name(name):
            result.excluded_st += 1
            continue
        eligible_bars[symbol] = rows

    # Scope and name gates are deliberately before technical analysis: a full
    # universe scan should not spend CPU deriving indicators for excluded boards.
    snapshots = DailyTechnicalAnalyzer().analyze_many(eligible_bars)
    for symbol, rows in eligible_bars.items():
        name = normalized_names.get(symbol)
        if not rows or symbol not in snapshots:
            result.rejected_data += 1
            result.rejection_reasons["data_bars"] = result.rejection_reasons.get("data_bars", 0) + 1
            continue
        result.with_bars += 1
        candidate, rejection = _candidate_from(
            symbol,
            rows,
            snapshots[symbol],
            name=name,
            sector=normalized_sectors.get(symbol),
            config=cfg,
            profile=profile,
        )
        if candidate is None:
            key = rejection or "unknown"
            result.rejection_reasons[key] = result.rejection_reasons.get(key, 0) + 1
            if key.startswith("data_"):
                result.rejected_data += 1
            else:
                result.rejected_shape += 1
        else:
            result.candidates.append(candidate)

    result.candidates.sort(key=lambda item: (-item.setup_rank, item.range_position_60d, -(item.amount_yi or 0.0)))
    if cfg.limit:
        result.candidates = result.candidates[: cfg.limit]
    if not result.candidates:
        result.notes.append("没有标的同时满足该策略与当前参数；这不是行情方向结论。")
    result.notes.append("setup_rank 只用于同一策略内排序，不是胜率、概率或预期收益率。")
    if result.rejected_data:
        result.notes.append(f"{result.rejected_data} 只标的数据不足，未参与形态判断。")
    return result


def _ordered_bars(rows: list[dict] | None) -> list[dict]:
    """Return valid daily rows in chronological order for a stable signal day."""
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, dict)]
    return sorted(
        valid,
        key=lambda row: str(
            row.get("timestamp") or row.get("date") or row.get("日期") or row.get("时间") or ""
        ),
    )


def diversified_shortlist(
    results: dict[str, StrategyScreenResult], limit: int
) -> list[dict[str, Any]]:
    """Round-robin profiles without pretending their ranks are comparable.

    Profile scores have different feasible regions, so sorting all candidates
    by ``setup_rank`` would create a false common scale.  Round-robin keeps the
    top of every requested style and de-duplicates symbols deterministically.
    """
    if limit <= 0:
        return []
    memberships: dict[str, list[str]] = {}
    for profile, result in results.items():
        for candidate in result.candidates:
            memberships.setdefault(candidate.symbol, []).append(profile)
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    depth = 0
    ordered_profiles = sorted(results)
    while len(chosen) < limit:
        added = False
        for profile in ordered_profiles:
            candidates = results[profile].candidates
            if depth >= len(candidates):
                continue
            added = True
            candidate = candidates[depth]
            if candidate.symbol in seen:
                continue
            seen.add(candidate.symbol)
            item = candidate.to_dict()
            item["matched_profiles"] = sorted(memberships[candidate.symbol])
            item["source_profile_rank"] = depth + 1
            chosen.append(item)
            if len(chosen) >= limit:
                break
        if not added:
            break
        depth += 1
    return chosen


def _candidate_from(
    symbol: str,
    rows: list[dict],
    snapshot: Any,
    *,
    name: str | None,
    sector: str | None,
    config: StrategyScreenConfig,
    profile: StrategyProfile,
) -> tuple[StrategyCandidate | None, str | None]:
    last = rows[-1]
    close = finite(last.get("close"))
    amount = finite(last.get("amount"))
    daily_position = close_position(last)
    atr = getattr(snapshot, "atr14", None)
    ma20 = getattr(snapshot, "ma20", None)
    range_position = getattr(snapshot, "range_position_60d", None)
    if getattr(snapshot, "bar_count", 0) < config.min_history_bars:
        return None, "data_history"
    if close is None or close <= 0 or atr is None or atr <= 0 or ma20 is None or ma20 <= 0:
        return None, "data_technical"
    if daily_position is None or range_position is None:
        return None, "data_technical"

    dip = measure_dip(rows, snapshot)
    contraction = dip.volume_contraction
    pullback = dip.pullback_pct
    if contraction is None or pullback is None:
        return None, "data_dip_features"
    atr_pct = atr / close
    close_vs_ma20 = close / ma20 - 1.0
    amount_yi = amount / 1e8 if amount is not None and amount >= 0 else None
    volume_ratio = getattr(snapshot, "volume_ratio_20d", None)
    constraints = (
        ("price", config.price_min <= close <= config.price_max),
        ("amount", amount_yi is not None and amount_yi >= config.min_amount_yi),
        ("atr", profile.min_atr_pct <= atr_pct <= profile.max_atr_pct),
        ("pullback", profile.min_pullback_pct <= pullback <= profile.max_pullback_pct),
        ("range_position", profile.min_range_position <= range_position <= profile.max_range_position),
        ("ma20", close_vs_ma20 >= profile.min_close_vs_ma20_pct),
        ("volume_contraction", contraction <= profile.max_volume_contraction),
        ("volume_ratio", volume_ratio is not None and profile.min_volume_ratio <= volume_ratio <= profile.max_volume_ratio),
        ("close_position", profile.min_close_position <= daily_position <= profile.max_close_position),
    )
    failed = next((name for name, accepted in constraints if not accepted), None)
    if failed:
        return None, failed
    assert amount_yi is not None and volume_ratio is not None

    rank, rank_components = _setup_rank(
        amount_yi=amount_yi,
        min_amount_yi=config.min_amount_yi,
        range_position=range_position,
        pullback=pullback,
        min_pullback=profile.min_pullback_pct,
        close_position_value=daily_position,
        profile=profile,
    )
    reasons = [
        f"成交额 {amount_yi:.1f} 亿，达到 {config.min_amount_yi:.1f} 亿门槛",
        f"60 日位置 {range_position:.2f}，回撤 {pullback:+.1%}",
        f"ATR {atr_pct:.1%}，回调量能 {contraction:.2f}",
        f"收盘相对 MA20 {close_vs_ma20:+.1%}，日内收盘位置 {daily_position:.2f}",
    ]
    warnings = []
    if is_st_name(name):
        warnings.append("ST/退市风险标的已按参数纳入，涨跌幅限制与风险特征不同")
    if daily_position >= 0.80:
        warnings.append("收盘接近日内高位，下一交易日不宜按昨收直接追价")
    previous_low = finite(rows[-2].get("low")) if len(rows) >= 2 else None
    signal_low = finite(last.get("low"))
    stamp = last.get("timestamp") or last.get("date") or last.get("日期")
    signal_date = str(stamp)[:10] if stamp is not None else None
    return StrategyCandidate(
        symbol=symbol,
        name=name,
        sector=sector,
        profile=profile.name,
        profile_label=profile.label,
        setup_rank=rank,
        rank_components=rank_components,
        signal_date=signal_date,
        last_close=round(close, 4),
        signal_low=None if signal_low is None else round(signal_low, 4),
        previous_low=None if previous_low is None else round(previous_low, 4),
        amount_yi=round(amount_yi, 4),
        atr_pct=round(atr_pct, 6),
        range_position_60d=round(range_position, 6),
        pullback_pct=round(pullback, 6),
        close_vs_ma20_pct=round(close_vs_ma20, 6),
        close_position=round(daily_position, 6),
        volume_contraction=round(contraction, 6),
        volume_ratio_20d=round(volume_ratio, 6),
        pullback_atr=dip.pullback_atr,
        down_days=dip.down_days,
        is_st=is_st_name(name),
        reasons=reasons,
        warnings=warnings,
    ), None


def _setup_rank(
    *,
    amount_yi: float,
    min_amount_yi: float,
    range_position: float,
    pullback: float,
    min_pullback: float,
    close_position_value: float,
    profile: StrategyProfile,
) -> tuple[float, dict[str, float]]:
    """A transparent rank for list ordering, intentionally not a forecast."""
    liquidity = min(1.0, log10(max(amount_yi / max(min_amount_yi, 0.01), 1.0) + 1.0) / log10(4.0))
    if profile.name == "breakout-continuation":
        location = range_position
        pullback_quality = 1.0 - min(1.0, abs(pullback) / max(abs(min_pullback), 0.01))
        close_target = 0.90
    else:
        location = 1.0 - range_position
        pullback_quality = min(1.0, abs(pullback) / max(abs(min_pullback), 0.01))
        close_target = 0.55
    close_quality = 1.0 - min(1.0, abs(close_position_value - close_target) / max(close_target, 0.01))
    components = {
        "liquidity": round(liquidity, 6),
        "location": round(location, 6),
        "pullback_quality": round(pullback_quality, 6),
        "close_quality": round(close_quality, 6),
    }
    rank = 100.0 * (0.30 * liquidity + 0.30 * location + 0.25 * pullback_quality + 0.15 * close_quality)
    return round(rank, 2), components


BATCH_SCHEMA = "ashare-strategy-screen-batch-v2"

# 这两句跟着每一份筛选结果走。写成常量而不是在调用处各拼一遍：`_cmd_strategy_screen`
# 原来把它拼了**两遍**（单策略分支一遍，末尾一个重复的 `if profile == "all"` 又
# 一遍），两份字面量逐字相同——直到有人只改了其中一份。
def screen_notes(days: int, taxonomy_notes: list[str] | None = None) -> list[str]:
    return [
        f"日线回溯区间约 {days} 个自然日，信号日为最近一根已取得日线",
        "这是候选观察事实，不是订单、收益保证或模型预测",
        *(taxonomy_notes or []),
    ]


def screen_all_profiles(
    bars: dict[str, list[dict]],
    *,
    names: dict[str, str],
    sectors: dict[str, str],
    common: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    """把每个 profile 各跑一遍，拼成批量产物。

    从 `cli.py:_cmd_strategy_screen` 搬过来的。它原来是一个 98 行的命令处理器，
    而同一个文件里其余命令处理器的中位长度是 11 行——**跨档次的那一个，装的通常
    不是命令而是领域逻辑**：这里的跨策略重叠、每策略截断、综合短名单，没有一样
    和「怎么解析命令行」有关，却全都在 `argparse` 那一层。

    **先算全集再截断。** 每个 profile 用 `limit=0` 跑，重叠关系因此建立在完整的
    命中集合上；反过来先截断再求交，重叠就变成了排序切点的产物——同一批数据，
    把 `--limit` 从 30 改成 50 会「发现」新的重复命中，而那不是发现。
    """
    results = {
        profile: screen_overnight(
            bars,
            names=names,
            sectors=sectors,
            config=StrategyScreenConfig(profile=profile, **{**common, "limit": 0}),
        )
        for profile in sorted(PROFILES)
    }

    memberships: dict[str, list[str]] = {}
    for profile, result in results.items():
        for item in result.candidates:
            memberships.setdefault(item.symbol, []).append(profile)

    payloads: dict[str, Any] = {}
    for name, result in results.items():
        item = result.to_dict()
        # `matched_total` 是过闸的全部只数，`candidates` 是印出来的那几只。
        # 两个数必须分开报：只印后者会让「筛出 30 只」和「命中 300 只里的前 30」
        # 读起来一样。
        item["summary"]["matched_total"] = len(item["candidates"])
        item["config"]["limit"] = limit
        item["candidates"] = item["candidates"][:limit]
        item["summary"]["candidates"] = len(item["candidates"])
        payloads[name] = item

    return {
        "schema": BATCH_SCHEMA,
        "config": common | {"profile": "all"},
        "profiles": payloads,
        "diversified_shortlist": diversified_shortlist(results, limit),
        "matched_by_multiple_profiles": {
            symbol: sorted(profiles)
            for symbol, profiles in sorted(memberships.items())
            if len(profiles) > 1
        },
        "notes": [
            "综合短名单按策略轮询取各自前列，不把不同策略的 setup_rank 当成同一尺度",
            "策略位置区间可能互斥；多策略重复命中不代表更高胜率或收益率",
        ],
    }
