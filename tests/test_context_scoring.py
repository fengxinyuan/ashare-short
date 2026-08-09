from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreInput,
    SectorFactorContext,
    StockFactorContext,
    build_factor_contexts,
    read_catalysts,
)


def bars(symbol: str = "600000.SH", count: int = 80, step: float = 0.02) -> list[dict]:
    price = 20.0
    rows = []
    for index in range(count):
        price += step
        rows.append(
            {
                "symbol": symbol,
                "timestamp": datetime(2026, 5, 1 + index % 28),
                "open": price - 0.1,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 100_000 + index * 500,
                "amount": (100_000 + index * 500) * price * 100,
            }
        )
    return rows


def snapshot(**overrides) -> DailyTechnicalSnapshot:
    values = {
        "symbol": "600000.SH",
        "bar_count": 120,
        "last_close": 22.4,
        "last_open": 22.0,
        "last_high": 22.6,
        "last_low": 21.9,
        "ma5": 22.0,
        "ma10": 21.7,
        "ma20": 21.0,
        "ma60": 19.5,
        "atr14": 0.4,
        "volume_ratio_20d": 1.3,
        "range_position_60d": 0.65,
        "trend_state": TrendState.UPTREND,
        "stop_reference": 21.2,
        "target_reference": 24.0,
        "explanation": "test",
        "source_ts": datetime(2026, 8, 4, 15, 0),
    }
    values.update(overrides)
    return DailyTechnicalSnapshot(**values)


def test_context_increases_score_only_when_multiple_layers_agree() -> None:
    provider = RuleBasedScoreProvider()
    plain = provider.score(
        ScoreInput(symbol="600000.SH", bars=bars(), snapshot=snapshot())
    )
    context = StockFactorContext(
        sector=SectorFactorContext(
            name="电网设备",
            score=78.0,
            ret_5d=0.04,
            breadth=0.72,
            flow_norm=0.8,
            headlines=("电网设备获政策支持，订单增长",),
        ),
        net_inflow=8e7,
        flow_norm=0.9,
        flow_ratio=0.04,
        amount=2e9,
        earnings=SimpleNamespace(
            is_strong_catalyst=True,
            catalysts=["预增"],
            warnings=[],
            blocking=[],
        ),
    )
    volume_price = SimpleNamespace(
        quadrant="放量上涨",
        expanding=True,
        up_down_vol_ratio=1.45,
        climax=False,
        climax_stall=False,
        breakdown_on_volume=False,
    )
    enriched = provider.score(
        ScoreInput(
            symbol="600000.SH",
            bars=bars(),
            snapshot=snapshot(),
            volume_price=volume_price,
            context=context,
        )
    )

    assert enriched.score > plain.score
    assert enriched.components["sector_relation"] > 0
    assert enriched.components["capital_flow"] > 0
    assert enriched.components["volume_price"] > 0
    # 业绩**不产生分量**：它在理由里可见，但不进 components、不进 total。
    # 见 `rule_based.py:_score_earnings` 与 `DECISIONS.md` §六b。
    assert "earnings" not in enriched.components
    assert any("业绩催化" in reason for reason in enriched.reasons)


def test_earnings_never_moves_the_score(tmp_path=None) -> None:
    """把业绩读数从「最强催化」翻到「触发否决」，分数必须一动不动。

    这是那条纪律唯一守得住的检验方式。只断言 `components` 里没有 earnings 挡不住
    有人把分加在别的键上——只有分数本身不变才说明它真的没参与。
    """
    provider = RuleBasedScoreProvider()

    def scored(earnings):
        return provider.score(
            ScoreInput(
                symbol="600000.SH",
                bars=bars(),
                snapshot=snapshot(),
                context=StockFactorContext(earnings=earnings),
            )
        )

    best = scored(SimpleNamespace(
        is_strong_catalyst=True, catalysts=["预增 +1197%"], warnings=[], blocking=[]
    ))
    worst = scored(SimpleNamespace(
        is_strong_catalyst=False, catalysts=[], warnings=["预减"], blocking=["首亏"]
    ))
    absent = scored(None)

    assert best.score == worst.score == absent.score
    # 但两者的措辞必须不同——不计分不等于不出现。
    assert any("业绩催化" in reason for reason in best.reasons)
    assert any("业绩风险否决" in warning for warning in worst.warnings)


def test_negative_flow_and_breakdown_override_positive_headline() -> None:
    provider = RuleBasedScoreProvider()
    plain = provider.score(
        ScoreInput(symbol="600000.SH", bars=bars(), snapshot=snapshot())
    )
    context = StockFactorContext(
        sector=SectorFactorContext(
            name="热门题材",
            score=72.0,
            breadth=0.65,
            headlines=("政策支持，订单增长",),
        ),
        net_inflow=-8e7,
        flow_norm=-1.0,
        flow_ratio=-0.04,
        earnings=SimpleNamespace(
            is_strong_catalyst=False,
            catalysts=[],
            warnings=["业绩预减"],
            blocking=[],
        ),
    )
    volume_price = SimpleNamespace(
        quadrant="放量下跌",
        expanding=True,
        up_down_vol_ratio=0.65,
        climax=False,
        climax_stall=False,
        breakdown_on_volume=True,
    )
    enriched = provider.score(
        ScoreInput(
            symbol="600000.SH",
            bars=bars(),
            snapshot=snapshot(),
            volume_price=volume_price,
            context=context,
        )
    )

    assert enriched.score < plain.score
    assert enriched.components["capital_flow"] < 0
    assert enriched.components["volume_price"] < 0
    assert any("放量跌破" in warning for warning in enriched.warnings)


def test_build_factor_contexts_normalises_flow_and_keeps_sector_relation() -> None:
    metrics = SimpleNamespace(
        ret_1d=0.01,
        ret_5d=0.05,
        ret_20d=0.08,
        breadth=0.7,
        volume_ratio=1.25,
        above_ma20=0.65,
    )
    outlook = SimpleNamespace(
        name="电子元件",
        score=75.0,
        trusted=True,
        metrics=metrics,
        net_inflow=2e8,
        sentiment_polarity=0.8,
        sentiment_intensity=0.7,
        headlines=("电子元件涨价，政策支持",),
    )
    row = SimpleNamespace(amount=1e9, turnover_ratio=8.0)
    scan = SimpleNamespace(get=lambda symbol: row)
    contexts = build_factor_contexts(
        [outlook],
        {"600000.SH": "电子元件", "000001.SZ": "电子元件"},
        net_inflow_by_symbol={"600000.SH": 100e6, "000001.SZ": -50e6},
        market_scan=scan,
    )

    first = contexts["600000.SH"]
    second = contexts["000001.SZ"]
    assert first.sector is not None
    assert first.sector.catalyst.price_score > 0
    assert first.flow_norm == 1.0
    assert second.flow_norm == -0.5
    assert first.flow_ratio == 0.1
    assert first.turnover_rate == 0.08


def test_non_finite_upstream_readings_never_reach_the_score() -> None:
    """NaN / inf / bool 到评分层必须变成「没有这一项」，而不是变成一个分数。

    `numeric.clamp` 的前提是入参已经过 `finite`——它自己明说不挡 NaN。所以
    `nan` 一旦进到 `clamp((score - 50) / 50 * 2.5, -2.5, 2.5)`，比较全为 False，
    结果会静默落在区间的某一端：上游「这格没取到」被读成一个**满格分项**。
    这条断言守的是取数那一层，不是夹持那一步。
    """
    nan = float("nan")
    poisoned = SimpleNamespace(
        name="电子元件",
        score=nan,
        trusted=True,
        metrics=SimpleNamespace(
            ret_1d=nan,
            ret_5d=float("inf"),
            ret_20d=None,
            breadth=nan,
            volume_ratio=None,
            above_ma20=None,
        ),
        net_inflow=nan,
        # 本该是数字的位置上出现 True，含义是上游给错了，不是「等于 1」。
        sentiment_polarity=True,
        sentiment_intensity=nan,
        headlines=(),
    )
    contexts = build_factor_contexts([poisoned], {"600000.SH": "电子元件"})
    sector = contexts["600000.SH"].sector
    assert sector is not None
    assert (sector.score, sector.breadth, sector.ret_5d) == (None, None, None)
    assert (sector.sentiment_polarity, sector.sentiment_intensity) == (None, None)
    assert sector.net_inflow is None and sector.flow_norm is None

    provider = RuleBasedScoreProvider()
    scored = provider.score(
        ScoreInput(
            symbol="600000.SH",
            bars=bars(),
            snapshot=snapshot(),
            context=contexts["600000.SH"],
        )
    )
    assert scored.components["sector_relation"] == 0.0
    assert scored.score == provider.score(
        ScoreInput(symbol="600000.SH", bars=bars(), snapshot=snapshot())
    ).score


def test_catalyst_reader_separates_policy_price_and_risk() -> None:
    reading = read_catalysts(("政策支持，产品涨价并获批", "控股股东减持"))

    assert reading.policy_score > 0
    assert reading.price_score > 0
    assert reading.event_score > 0
    assert reading.risk_score < 0
    assert "减持" in reading.risk_hits


def test_sector_catalyst_treats_supply_shutdown_as_industry_support() -> None:
    reading = read_catalysts(("纸厂停机检修，库存下降，纸价上涨",), sector=True)

    assert reading.supply_score > 0
    assert reading.risk_score == 0
    assert "停机" in reading.hits
