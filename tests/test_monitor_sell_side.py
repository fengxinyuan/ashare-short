"""卖点监控补的两个洞。

1. **你自己设的目标价从未被任何代码读过。** `position open --target` 一直能填，
   `Position.plan_target` 一直存着，`HoldingView` 一直带着它——而整条监控链路
   没有一个读取点。对称位置上的 `plan_stop` 从一开始就是生效的。

2. **浮盈回吐没有任何触发器。** 冲高又跌回来时，四类卖出触发**一条都不会响**：
   止损远在下方、目标没到、日线支撑还在、持有期没满。一笔曾经浮盈 10% 的持仓
   可以一路跌回 +4% 而全程静默——这正是「坐电梯」。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from ashare_decision_support.monitor.holdings import (
    GIVEBACK_MIN_ATR,
    HoldingView,
    assess_giveback,
    effective_targets,
)
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.positions import Positions

NOW = datetime(2026, 8, 3, 10, 30)


class Advice:
    """止损远在下方、目标远在上方——好让「四条都不响」是被验证的，不是被假设的。"""

    def __init__(self, stop=9.00, targets=(12.00,), entry=(None, None)):
        self.symbol = "601939.SH"
        self.name = "建设银行"
        self.stop = stop
        self.targets = list(targets)
        self.entry_low, self.entry_high = entry
        self.rrr = 2.4
        self.trend_state = "REBOUND"
        self.scenarios = []
        self.warnings = []


class Intraday:
    def __init__(self, price, vwap=None):
        self.last_price = price
        self.vwap = vwap if vwap is not None else price
        self.session_high = price * 1.01
        self.session_low = price * 0.99
        self.volume_ratio = 1.0
        self.last_timestamp = NOW
        self.session_date = date(2026, 8, 3)
        self.bar_count = 60


def held(**kw):
    d = {"symbol": "601939.SH", "cost_price": 10.00, "shares": 1000,
             "opened_on": "2026-08-01", "held_days": 2}
    d.update(kw)
    return HoldingView(**d)


def state():
    return AlertState(date(2026, 8, 3), Path("/dev/null"))


def fire(engine, st, advice, intraday, holding=None, **kw):
    return engine.evaluate(
        advice=advice, intraday=intraday, state=st, now=NOW, holding=holding, **kw,
    )


# ---- 你设的目标价 ------------------------------------------------------------


def test_your_own_target_replaces_the_computed_ladder() -> None:
    """填了目标价就是做了决定。把系统那串阶梯留着，等于在你已经决定的地方
    再塞两个提醒——这与 L3 裁决选中一个目标就只报那一个是同一条口径。"""
    assert effective_targets(held(plan_target=11.60), [11.20, 11.80]) == [11.60]
    assert effective_targets(held(), [11.20, 11.80]) == [11.20, 11.80]
    assert effective_targets(None, [11.20, 11.80]) == [11.20, 11.80]
    assert effective_targets(None, None) == []


def test_a_manual_target_actually_fires_an_alert() -> None:
    """这是这次改动的核心：改之前 `plan_target` 在监控链路里没有任何读取点，
    你设的目标价**永远不会触发告警**。"""
    engine, st = TriggerEngine(), state()
    holding = held(plan_target=11.60)

    fire(engine, st, Advice(), Intraday(11.00), holding)           # 基线：还没到
    alerts = fire(engine, st, Advice(), Intraday(11.65), holding)  # 摸到你的目标

    assert [a.trigger for a in alerts] == ["target_hit:0"]
    assert "11.60" in alerts[0].headline
    # 标出是你设的——否则你会以为这个数是系统算出来的。
    assert "你设定的" in alerts[0].headline


def test_the_computed_ladder_is_not_also_reported_once_you_set_your_own() -> None:
    """系统目标 12.00 在你设了 11.60 之后不该再单独响一次。"""
    engine, st = TriggerEngine(), state()
    holding = held(plan_target=11.60)

    fire(engine, st, Advice(targets=(12.00,)), Intraday(11.00), holding)
    alerts = fire(engine, st, Advice(targets=(12.00,)), Intraday(12.10), holding)

    assert [a.trigger for a in alerts] == ["target_hit:0"]
    assert "11.60" in alerts[0].headline


def test_a_manual_target_fires_before_the_session_has_taken_shape() -> None:
    """和 `plan_stop` 对称：你手填的是绝对价位，不依赖当日结构成型。开盘 15 分钟
    内系统目标被清空（`_apply_pinned_levels`），但你的目标该照报。"""
    engine, st = TriggerEngine(), state()
    holding = held(plan_target=11.60)

    fire(engine, st, Advice(targets=()), Intraday(11.00), holding, armed=False)
    alerts = fire(engine, st, Advice(targets=()), Intraday(11.65), holding, armed=False)

    assert [a.trigger for a in alerts] == ["target_hit:0"]


def test_a_computed_target_still_waits_for_the_session_to_arm() -> None:
    """上一条是给手工值开的口子，不是把闸门拆了：系统算的目标仍然要等 armed。"""
    engine, st = TriggerEngine(), state()

    fire(engine, st, Advice(targets=(12.00,)), Intraday(11.00), held(), armed=False)
    alerts = fire(engine, st, Advice(targets=(12.00,)), Intraday(12.10), held(), armed=False)

    assert alerts == []


def test_setting_only_a_stop_does_not_unlock_the_computed_targets() -> None:
    """两个口子各开各的。只填了止损却让系统算的目标也提前触发，等于把那道
    「等当日结构成型」的约束整个绕过去——而它正是为了拦这件事。"""
    engine, st = TriggerEngine(), state()
    holding = held(plan_stop=9.50)   # 只设止损，没设目标

    fire(engine, st, Advice(targets=(12.00,)), Intraday(11.00), holding, armed=False)
    alerts = fire(engine, st, Advice(targets=(12.00,)), Intraday(12.10), holding, armed=False)

    assert [a.trigger for a in alerts] == []


# ---- 浮盈回吐（移动止损）-----------------------------------------------------


def test_giving_back_half_the_gain_is_flagged() -> None:
    """成本 10.00 → 最高 11.00（+10%）→ 现价 10.40（+4%）：吐掉六成。"""
    reading = assess_giveback(held(), price=10.40, peak_price=11.00, atr=0.30)

    assert reading is not None and reading.due is True
    assert round(reading.given_back_ratio, 2) == 0.60
    assert round(reading.given_back_atr, 1) == 2.0


def test_a_retreat_inside_the_stocks_own_noise_is_not_a_giveback() -> None:
    """同样吐掉六成，但这只票的日振幅本来就有那么大——按比例判会让高波动的
    票天天报警。单位取标的自身 ATR，理由和低吸档位、止损距离完全一样。"""
    reading = assess_giveback(held(), price=10.40, peak_price=11.00, atr=0.80)

    assert reading is not None and reading.due is False
    assert reading.given_back_atr < GIVEBACK_MIN_ATR


def test_a_normal_pullback_that_keeps_most_of_the_gain_is_not_a_giveback() -> None:
    """回撤幅度够大，但浮盈还留着六成——那是正常回踩，不是坐电梯。"""
    reading = assess_giveback(held(), price=10.60, peak_price=11.00, atr=0.20)

    assert reading is not None and reading.due is False
    assert reading.given_back_ratio < 0.5


def test_unjudgeable_is_none_rather_than_no_giveback() -> None:
    """把「不知道」当成「没事」，正是这条触发器要防的那类静默。"""
    assert assess_giveback(held(cost_price=None), 10.40, 11.00, 0.30) is None   # 没成本价
    assert assess_giveback(held(), 10.40, 11.00, None) is None                  # 没有波幅单位
    assert assess_giveback(held(), 10.40, None, 0.30) is None                   # 峰值未知
    assert assess_giveback(None, 10.40, 11.00, 0.30) is None                    # 没持仓


def test_a_position_that_was_never_in_profit_is_not_a_giveback() -> None:
    """从来没有过浮盈的持仓归止损和结构破坏管，不归这里。"""
    assert assess_giveback(held(), price=9.40, peak_price=9.80, atr=0.20) is None


def test_the_four_old_triggers_stay_silent_while_the_lift_goes_down() -> None:
    """这条测试就是这个功能存在的理由。

    止损 9.00 没破、目标 12.00 没到、支撑 9.50 还在、持有 2 天没到期——改之前
    这一整段下跌不会产生任何一条告警。"""
    engine, st = TriggerEngine(), state()
    holding = held()

    fire(engine, st, Advice(), Intraday(11.00), holding,
         peak_price=11.00, atr=0.30, daily_support=9.50)
    alerts = fire(engine, st, Advice(), Intraday(10.40), holding,
                  peak_price=11.00, atr=0.30, daily_support=9.50)

    assert [a.trigger for a in alerts] == ["giveback"]
    assert alerts[0].level == "ACTION"
    body = " ".join(alerts[0].detail)
    assert "11.00" in body and "回吐" in body


def test_giveback_fires_on_the_first_pass_because_it_is_a_state() -> None:
    """峰值是从建仓以来的日线播种的，所以哪怕监控今天才第一次开起来，
    「已经吐掉一半」在第一轮就已经是事实，不是跃迁。"""
    engine, st = TriggerEngine(), state()
    alerts = fire(engine, st, Advice(), Intraday(10.40), held(),
                  peak_price=11.00, atr=0.30)

    assert [a.trigger for a in alerts] == ["giveback"]


def test_giveback_says_it_once_a_day() -> None:
    """「今天的高点已经吐掉一半」重复说第二遍不增加任何信息。"""
    engine, st = TriggerEngine(), state()
    holding = held()

    fire(engine, st, Advice(), Intraday(10.40), holding, peak_price=11.00, atr=0.30)
    again = fire(engine, st, Advice(), Intraday(10.30), holding, peak_price=11.00, atr=0.30)

    assert [a.trigger for a in again] == []


def test_giveback_never_fires_for_a_stock_you_do_not_hold() -> None:
    """没持仓就没有浮盈可以回吐。"""
    engine, st = TriggerEngine(), state()
    fire(engine, st, Advice(), Intraday(11.00), None, peak_price=11.00, atr=0.30)
    alerts = fire(engine, st, Advice(), Intraday(10.40), None, peak_price=11.00, atr=0.30)

    assert "giveback" not in [a.trigger for a in alerts]


# ---- 峰值：只涨不跌，且要活过重启 --------------------------------------------


def test_the_peak_only_ever_goes_up() -> None:
    st = state()
    assert st.observe_peak("601939.SH", 10.50) == 10.50
    assert st.observe_peak("601939.SH", 11.20) == 11.20
    assert st.observe_peak("601939.SH", 10.80) == 11.20   # 回落不改写峰值
    assert st.peak_of("601939.SH") == 11.20


def test_the_peak_survives_a_restart(tmp_path) -> None:
    """监控进程盘中重启是常事。峰值只存在内存里的话，重启会把上午冲出来的高点
    抹掉，于是「已回吐一半」重新读成「刚建仓没多久」——移动止损被静默关掉，
    恰好在最需要它的那天。"""
    path = tmp_path / "alerts.json"
    first = AlertState(date(2026, 8, 3), path)
    first.observe_peak("601939.SH", 11.20)
    first.save()

    assert AlertState.load(date(2026, 8, 3), path).peak_of("601939.SH") == 11.20


def test_old_alert_files_without_peaks_still_load(tmp_path) -> None:
    """读旧的 alerts/*.json 不该炸。"""
    path = tmp_path / "alerts.json"
    path.write_text('{"session": "2026-08-03", "fired": {}, "observed": {}}', encoding="utf-8")

    assert AlertState.load(date(2026, 8, 3), path).peak_of("601939.SH") is None


# ---- 峰值播种：建仓以来，不是今天以来 ----------------------------------------


def _session(tmp_path, bars):
    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(
        adapter=None, intraday_adapter=None, session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), tmp_path / "alerts.json"),
    )
    session.daily_bars = bars
    return session


BARS = [
    {"timestamp": datetime(2026, 7, 30), "high": 12.50, "close": 12.40},   # 建仓前
    {"timestamp": datetime(2026, 7, 31), "high": 10.10, "close": 10.00},
    {"timestamp": datetime(2026, 8, 1), "high": 10.30, "close": 10.20},    # 建仓日
    {"timestamp": datetime(2026, 8, 2), "high": 11.00, "close": 10.60},
]


def test_the_peak_is_seeded_from_the_holding_period_not_from_today(tmp_path) -> None:
    """只用当日峰值会漏掉最常见的坐电梯：昨天冲到 +10%，今天平开一路阴跌。
    今天的峰值约等于开盘价，日内回撤很小，移动止损整天不响。"""
    book = Positions(path=tmp_path / "positions.json")
    book.open("601939.SH", cost_price=10.00, opened_on=date(2026, 8, 1))

    session = _session(tmp_path, {"601939.SH": BARS})
    session.load_positions(book)

    # 建仓日之后的最高是 11.00；建仓**之前**那根 12.50 不算。
    assert session.state.peak_of("601939.SH") == 11.00


def test_no_open_date_means_no_seed_rather_than_a_year_of_history(tmp_path) -> None:
    """没有建仓日就没有「持仓期」。拿 240 天的历史最高当峰值会算出一个假的
    回撤幅度——少一个种子只是迟一点，播一个错的种子会直接报错误的告警。"""
    path = tmp_path / "positions.json"
    path.write_text('[{"symbol": "601939.SH", "cost_price": 10.0}]', encoding="utf-8")
    book = Positions(path=path)

    session = _session(tmp_path, {"601939.SH": BARS})
    session.load_positions(book)

    assert session.state.peak_of("601939.SH") is None


def test_seeding_survives_missing_daily_bars(tmp_path) -> None:
    """拿不到日线不该让装载持仓这一步炸掉——盘中的实时高点会自己累积。"""
    book = Positions(path=tmp_path / "positions.json")
    book.open("601939.SH", cost_price=10.00, opened_on=date(2026, 8, 1))

    session = _session(tmp_path, {})
    notes = session.load_positions(book)

    assert session.state.peak_of("601939.SH") is None
    assert any("浮盈回吐" in note for note in notes)


# ---- 端到端：峰值与 ATR 真的走完了一整轮 poll ---------------------------------


def _daily(count: int = 30):
    """平静的 30 根日线（振幅 0.2 元 → ATR14 约 0.2），最后一根冲到 11.00。"""
    origin = date(2026, 7, 3)
    rows = [
        {
            "symbol": "601939.SH",
            "timestamp": datetime.combine(origin + timedelta(days=index), datetime.min.time()),
            "open": 10.00, "high": 10.10, "low": 9.90, "close": 10.00,
            "volume": 100000, "amount": 1_000_000,
        }
        for index in range(count)
    ]
    rows[-2]["timestamp"] = datetime(2026, 8, 1)   # 建仓日
    rows[-1]["timestamp"] = datetime(2026, 8, 2)   # 冲到 11.00 的那天
    rows[-1].update(high=11.00, close=10.60)
    return rows


def _minutes(closes, day=date(2026, 8, 3)):
    origin = datetime(day.year, day.month, day.day, 9, 30)
    return [
        {
            "symbol": "601939.SH",
            "timestamp": origin + timedelta(minutes=index),
            "open": close, "high": close + 0.02, "low": close - 0.02, "close": close,
            "volume": 1000, "amount": close * 1000,
        }
        for index, close in enumerate(closes)
    ]


class _StubDaily:
    def get_historical_bars(self, symbols, start, end):
        return {symbol: _daily() for symbol in symbols}

    def get_security_master(self, symbols):
        return {symbol: {"name": "建设银行"} for symbol in symbols}


class _StubIntraday:
    def __init__(self, closes):
        self.bars = _minutes(closes)

    def get_minute_bars(self, symbols, period="1", **kwargs):
        return (dict.fromkeys(symbols, self.bars), [])


def test_a_real_poll_delivers_the_peak_and_the_atr_all_the_way_through(tmp_path) -> None:
    """单测直接调 `evaluate` 证明不了接线。这一条走完整条路：
    `prime` 拿日线 → `load_positions` 播种峰值 → `poll` 推进峰值并把 ATR 传下去。

    成本 10.00、持仓期最高 11.00、现价 10.40——回吐六成、约 2 个日波幅。"""
    from ashare_decision_support.monitor.session import MonitorSession

    book = Positions(path=tmp_path / "positions.json")
    book.open("601939.SH", cost_price=10.00, opened_on=date(2026, 8, 1))

    session = MonitorSession(
        adapter=_StubDaily(),
        intraday_adapter=_StubIntraday([10.40] * 40),
        session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), tmp_path / "alerts.json"),
    )
    session.prime(["601939.SH"])
    session.load_positions(book)

    assert session.state.peak_of("601939.SH") == 11.00
    # 时效闸门是监控的第一步：now 必须紧跟最后一根分钟线，否则这一轮会被判成
    # 数据停更而整轮不产出信号。
    result = session.poll(
        ["601939.SH"], session.intraday_adapter.bars[-1]["timestamp"] + timedelta(minutes=1)
    )

    assert [a.trigger for a in result.alerts] == ["giveback"]
    assert result.alerts[0].holding is True
    body = " ".join(result.alerts[0].detail)
    assert "11.00" in body and "成本 10.00" in body
