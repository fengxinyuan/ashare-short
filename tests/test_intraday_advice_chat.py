from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ashare_decision_support.adapters.freshness import check_intraday_freshness, in_session
from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter
from ashare_decision_support.advice import AdviceEngine
from ashare_decision_support.chat.session import (
    ChatResponder,
    ChatSession,
    handle_command,
    render_facts_summary,
)
from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, PriceLevel
from ashare_decision_support.schedule.clock import minutes_since_open
from ashare_decision_support.signals.intraday import IntradayAnalyzer, _minutes_since_open
from ashare_decision_support.symbols import prefixed_code

# ---- symbol normalization ------------------------------------------------


def test_prefixed_code_covers_every_input_shape() -> None:
    assert prefixed_code("600519.SH") == "sh600519"
    assert prefixed_code("000001.SZ") == "sz000001"
    assert prefixed_code("300750") == "sz300750"
    assert prefixed_code("688981") == "sh688981"
    assert prefixed_code("sh600519") == "sh600519"
    assert prefixed_code("830799.BJ") == "bj830799"
    # 分歧点：`sina_intraday` 那份自抄的前缀表少了 730/731/732/733，把它判成
    # 北交所。三路取数认的是同一张表之后，这里和 `SH_PREFIXES` 只能一起改。
    assert prefixed_code("730001") == "sh730001"


# ---- intraday fetch ------------------------------------------------------


def minute_rows(day: str, count: int, start_price: float = 10.0, step: float = 0.01) -> list[dict]:
    rows = []
    moment = datetime.strptime(f"{day} 09:30:00", "%Y-%m-%d %H:%M:%S")
    price = start_price
    for index in range(count):
        price += step
        rows.append(
            {
                "day": (moment + timedelta(minutes=5 * index)).strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(price - step, 3),
                "high": round(price + 0.02, 3),
                "low": round(price - 0.02, 3),
                "close": round(price, 3),
                "volume": 1000.0,
                "amount": 1000.0 * price,
            }
        )
    return rows


class FakeSinaAk:
    def __init__(self, broken: set[str] | None = None, rows: list[dict] | None = None) -> None:
        self.broken = broken or set()
        self.rows = rows if rows is not None else minute_rows("2026-07-31", 48)
        self.calls: list[tuple[str, str]] = []

    def stock_zh_a_minute(self, symbol, period, adjust):
        self.calls.append((symbol, period))
        if symbol in self.broken:
            raise ConnectionError("feed down")
        return list(self.rows)


def test_intraday_adapter_requests_raw_prices() -> None:
    """Levels are order prices; an adjusted series would make them wrong."""
    ak = FakeSinaAk()
    captured = {}
    original = ak.stock_zh_a_minute

    def spy(symbol, period, adjust):
        captured["adjust"] = adjust
        return original(symbol, period, adjust)

    ak.stock_zh_a_minute = spy
    SinaIntradayAdapter(ak_module=ak).get_minute_bars(["600519.SH"], period="5")

    assert captured["adjust"] == ""


def test_intraday_adapter_degrades_one_symbol_at_a_time() -> None:
    adapter = SinaIntradayAdapter(ak_module=FakeSinaAk(broken={"sz000002"}))
    bars, notes = adapter.get_minute_bars(["600519.SH", "000002.SZ"], period="5")

    assert list(bars) == ["600519.SH"]
    assert any("000002.SZ" in note for note in notes)


def test_intraday_adapter_rejects_an_unsupported_period() -> None:
    adapter = SinaIntradayAdapter(ak_module=FakeSinaAk())
    try:
        adapter.get_minute_bars(["600519.SH"], period="7")
    except ValueError as exc:
        assert "7" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# ---- intraday structure --------------------------------------------------


def bars_from(rows: list[dict], symbol: str = "600519.SH") -> list[dict]:
    return SinaIntradayAdapter(ak_module=FakeSinaAk(rows=rows)).get_minute_bars([symbol], "5")[0][symbol]


def test_minutes_since_open_skips_the_lunch_break() -> None:
    assert _minutes_since_open(datetime(2026, 7, 31, 9, 30)) == 0
    assert _minutes_since_open(datetime(2026, 7, 31, 10, 30)) == 60
    assert _minutes_since_open(datetime(2026, 7, 31, 11, 30)) == 120
    # 13:00 is the afternoon open, so no minutes accrue over the break.
    assert _minutes_since_open(datetime(2026, 7, 31, 13, 0)) == 120
    assert _minutes_since_open(datetime(2026, 7, 31, 14, 0)) == 180
    assert _minutes_since_open(datetime(2026, 7, 31, 15, 0)) == 240


def test_the_bar_clock_caps_at_a_full_session_and_the_wall_clock_does_not() -> None:
    """两份实现收成一份之后，唯一保留的差别是**钳顶**，而它是刻意的。

    `schedule/clock.minutes_since_open` 答的是「现在几点了、数据还该不该更新」，
    收盘后必须继续走——15:30 和 15:00 要能区分。`signals/intraday` 这份答的是
    「这根 bar 落在交易日的第几分钟」，那个量按定义不超过 240；有些源把收盘那根
    戳成 15:00:30，不钳就会算出一个 241 分钟的交易日。
    """
    from ashare_decision_support.signals.intraday import FULL_SESSION_MINUTES

    late = datetime(2026, 7, 31, 15, 30)

    assert minutes_since_open(late) == 270
    assert _minutes_since_open(late) == FULL_SESSION_MINUTES == 240
    # 收盘那根被戳晚半分钟也不该多算出一分钟。
    assert _minutes_since_open(datetime(2026, 7, 31, 15, 0, 30)) == 240


def test_snapshot_flags_a_session_that_is_not_today() -> None:
    """Treating the previous close as live is the dangerous failure here."""
    snapshot = IntradayAnalyzer(today=date(2026, 8, 1)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.session_date == date(2026, 7, 31)
    assert not snapshot.is_current_session
    assert any("非当日" in note for note in snapshot.notes)


def test_snapshot_marks_the_current_session_clean() -> None:
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.is_current_session
    assert snapshot.notes == []


def test_snapshot_computes_vwap_and_position() -> None:
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.vwap is not None
    assert snapshot.session_low < snapshot.vwap < snapshot.session_high
    # Steady uptrend closes at the high.
    assert snapshot.range_position is not None and snapshot.range_position > 0.9
    assert snapshot.price_vs_open_pct is not None and snapshot.price_vs_open_pct > 0


def test_day_change_is_given_directly_not_left_for_the_reader_to_compose() -> None:
    """行情软件上那个「涨幅」必须自己是一个字段。

    它此前**声明了但从未被赋值**，注释却写着「尾盘打法的第一道筛直接读它」——
    于是永远是 None。代价不只是少一个数：事实包里只有跳空与开盘后涨幅两截，
    模型要说「今天涨了 3.3%」就得自己乘出来，而乘出来的数不在事实里，
    `unverified_numbers` 会把**整段论证**丢掉。
    """
    from ashare_decision_support.factcheck import unverified_numbers

    rows = minute_rows("2026-07-31", 48, start_price=10.0)
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(rows), prev_close=9.5
    )

    assert snapshot is not None
    assert snapshot.day_change_pct is not None
    # 是「相对昨收」，不是「相对今开」——两者只有在平开时才相等。
    assert snapshot.day_change_pct == round(snapshot.last_price / 9.5 - 1.0, 6)
    assert snapshot.day_change_pct != snapshot.price_vs_open_pct

    # 闸门那一头：两截数在手也拼不出合计，所以合计必须自己进事实包。
    pieces = json.dumps(
        {
            "open_gap_pct": snapshot.open_gap_pct,
            "price_vs_open_pct": snapshot.price_vs_open_pct,
        },
        ensure_ascii=False,
    )
    composed = f"{snapshot.day_change_pct:.6f}"
    assert unverified_numbers(f"今日涨幅 {composed}", pieces) == [composed]
    whole = json.dumps({"day_change_pct": snapshot.day_change_pct}, ensure_ascii=False)
    assert unverified_numbers(f"今日涨幅 {composed}", whole) == []


def test_snapshot_uses_only_the_latest_session() -> None:
    rows = minute_rows("2026-07-30", 48, start_price=5.0) + minute_rows("2026-07-31", 48, start_price=10.0)
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

    assert snapshot is not None
    assert snapshot.session_date == date(2026, 7, 31)
    assert snapshot.session_low > 9.0


def test_volume_ratio_scales_the_baseline_for_a_partial_session() -> None:
    """A half day against full days would read ~0.5 and look like drying up."""
    rows = []
    for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
        rows.extend(minute_rows(day, 48))
    rows.extend(minute_rows("2026-07-31", 24))  # half session
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

    assert snapshot is not None
    assert snapshot.volume_ratio is not None
    assert 0.9 <= snapshot.volume_ratio <= 1.1


def front_loaded_rows(day: str, count: int, opening_share: float = 0.30) -> list[dict]:
    """成交前置的一天：第一根吃掉 `opening_share`，其余均分。

    这是 A 股分时最常见的形状，也是 2026-08-04 牧原股份当天的实际形状——
    09:35 那一根 12.35 万手，占全天 64.8 万手的 19%。
    """
    rows = minute_rows(day, count)
    total = 1000.0 * count
    head = total * opening_share
    rest = (total - head) / max(count - 1, 1)
    for index, row in enumerate(rows):
        row["volume"] = head if index == 0 else rest
        row["amount"] = row["volume"] * row["close"]
    return rows


def test_volume_ratio_uses_the_baseline_at_the_same_point_of_day() -> None:
    """节奏和基线日一模一样的半天，量比就该读 1.0——不管在哪个时点读。

    原实现拿基线日的**全天**量按 `elapsed / 240` 线性折算，等于假设成交在一天里
    均匀发生。A 股不是：开盘那一根经常单根占掉两成。于是同样的节奏在早盘会被读成
    「放量」。2026-08-04 牧原实测 10:50 读 2.36，收盘实际 1.09。

    这里用同一个前置形状造基线日和当日，**不变量是 1.0**：只要今天走的是常态节奏，
    无论读几点，量比都不该偏离 1。线性折算在第一根上会读到 0.30/(5/240) = 14.4。
    """
    baseline = []
    for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
        baseline.extend(front_loaded_rows(day, 48))

    for bars_today, label in ((1, "开盘第一根"), (6, "半小时"), (24, "半天")):
        rows = baseline + front_loaded_rows("2026-07-31", 48)[:bars_today]
        snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

        assert snapshot is not None, label
        assert snapshot.volume_ratio is not None, label
        assert 0.95 <= snapshot.volume_ratio <= 1.05, f"{label} 读到 {snapshot.volume_ratio}"


def test_volume_ratio_still_flags_a_genuinely_heavy_open() -> None:
    """修掉的是口径偏差，不是灵敏度：真的比常态放量一倍，就该读到约 2。"""
    baseline = []
    for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
        baseline.extend(front_loaded_rows(day, 48))
    today = front_loaded_rows("2026-07-31", 48)[:6]
    for row in today:
        row["volume"] *= 2.0

    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(baseline + today))

    assert snapshot is not None
    assert snapshot.volume_ratio is not None
    assert 1.9 <= snapshot.volume_ratio <= 2.1


def test_volume_ratio_drops_a_baseline_day_with_no_volume_yet() -> None:
    """基线日在该时点前没有成交（数据缺口/停牌半天）就整条丢掉。

    留着它会让均值被 0 拖低，把量比放大成一个看起来很吓人的数——那正是这一层
    最不该制造的东西。
    """
    baseline = front_loaded_rows("2026-07-29", 48) + front_loaded_rows("2026-07-30", 48)
    gapped = front_loaded_rows("2026-07-28", 48)[12:]  # 当天上午的数据整段缺失
    rows = gapped + baseline + front_loaded_rows("2026-07-31", 48)[:6]
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

    assert snapshot is not None
    assert snapshot.volume_ratio is not None
    assert 0.95 <= snapshot.volume_ratio <= 1.05


# ---- 尾盘量能 -------------------------------------------------------------


def session_rows(
    day: str,
    bars: int = 48,
    start_price: float = 10.0,
    step: float = 0.01,
    tail_volume: float = 1.0,
    midday_volume: float = 1.0,
) -> list[dict]:
    """真实作息的 5 分钟线：上午 24 根 + 下午 24 根，**跳过午休**。

    `minute_rows` 从 09:30 一路加 5 分钟，48 根只走到 13:25——尾盘窗口测不到。这里
    把下午那半天挪回 13:00 起，最后一根落在 14:55（elapsed=235），尾盘 30 分钟窗口
    正好是最后 6 根。

    `tail_volume` 缩放最后 6 根的量，`midday_volume` 缩放中间那段——两个旋钮用来
    分别构造「尾盘放量」和「日内 U 形」。
    """
    rows = []
    price = start_price
    for index in range(bars):
        if index < bars // 2:
            moment = datetime.strptime(f"{day} 09:30:00", "%Y-%m-%d %H:%M:%S") + timedelta(
                minutes=5 * index
            )
        else:
            moment = datetime.strptime(f"{day} 13:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(
                minutes=5 * (index - bars // 2)
            )
        price += step
        in_tail = index >= bars - 6
        in_midday = bars // 4 <= index < bars - 6
        volume = 1000.0
        if in_tail:
            volume *= tail_volume
        elif in_midday:
            volume *= midday_volume
        rows.append(
            {
                "day": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(price - step, 3),
                "high": round(price + 0.02, 3),
                "low": round(price - 0.02, 3),
                "close": round(price, 3),
                "volume": volume,
                "amount": volume * price,
            }
        )
    return rows


def with_baseline(today_rows: list[dict], **baseline: float) -> list[dict]:
    rows: list[dict] = []
    for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
        rows.extend(session_rows(day, **baseline))
    return rows + today_rows


def tail_snapshot(today_rows: list[dict], **baseline: float):
    return IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(with_baseline(today_rows, **baseline))
    )


def test_closing_volume_ratio_reads_one_for_an_ordinary_tail() -> None:
    """尾盘节奏和前几日一样，就该读 1.0。"""
    snapshot = tail_snapshot(session_rows("2026-07-31"))

    assert snapshot is not None
    assert snapshot.closing_volume_ratio is not None
    assert 0.95 <= snapshot.closing_volume_ratio <= 1.05


def test_closing_volume_separates_a_bought_rally_from_a_painted_one() -> None:
    """**这是本读数存在的理由。**

    两天的价格序列完全相同，所以 `closing_drive_pct` 一模一样——单看涨幅，「尾盘
    缓拉吸筹」和「尾盘急拉做 K 线」不可分。加上量能之后才分得开：一个尾盘放量到
    2.5 倍（真金白银在买），一个萎缩到 0.4 倍（拉给人看的）。
    """
    bought = tail_snapshot(session_rows("2026-07-31", tail_volume=2.5))
    painted = tail_snapshot(session_rows("2026-07-31", tail_volume=0.4))

    assert bought is not None and painted is not None
    # 涨幅相同——这正是问题所在。
    assert bought.closing_drive_pct == painted.closing_drive_pct
    # 量能把它们分开。
    assert bought.closing_volume_ratio is not None
    assert painted.closing_volume_ratio is not None
    assert 2.4 <= bought.closing_volume_ratio <= 2.6
    assert 0.35 <= painted.closing_volume_ratio <= 0.45


def test_closing_volume_ratio_is_not_measured_against_the_daily_mean() -> None:
    """日内 U 形不该被读成尾盘放量。

    这一天（和基线日一样）中段缩量到 0.2、尾盘回到 1.0，是 A 股最常见的日内形状。
    下面把「当日分钟均量」口径会读到的数当场算出来做反证——它是常态形状的产物，
    不是资金行为。同一个错误的另一面见 `_volume_ratio` 弃用 `elapsed/240` 线性
    折算那段。
    """
    shape = {"midday_volume": 0.2}
    today = session_rows("2026-07-31", **shape)
    snapshot = tail_snapshot(today, **shape)

    # 反证：当日分钟均量做分母会读到 2.0，看着像「尾盘资金进场」。
    tail_per_bar = sum(row["volume"] for row in today[-6:]) / 6
    session_per_bar = sum(row["volume"] for row in today) / len(today)
    assert tail_per_bar / session_per_bar == 2.0

    # 同窗口口径读 1.0，因为这个形状本来就是常态。
    assert snapshot is not None
    assert snapshot.closing_volume_ratio is not None
    assert 0.95 <= snapshot.closing_volume_ratio <= 1.05


def test_closing_volume_ratio_declines_before_a_full_window_exists() -> None:
    """开盘 25 分钟时「最近 30 分钟」并不存在，给数就是让它冒充尾盘读数。"""
    snapshot = tail_snapshot(session_rows("2026-07-31")[:5])

    assert snapshot is not None
    assert snapshot.closing_volume_ratio is None


def test_closing_volume_ratio_needs_a_baseline_session() -> None:
    """只有当天、没有前几日，这个比值无从谈起。"""
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(session_rows("2026-07-31"))
    )

    assert snapshot is not None
    assert snapshot.closing_volume_ratio is None


def test_snapshot_returns_none_without_usable_bars() -> None:
    assert IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", []) is None


# ---- advice --------------------------------------------------------------


def price_level(side: str, price: float) -> PriceLevel:
    return PriceLevel(
        side=side, price=price, touches=2, strength=0.6, distance_pct=0.05, label=f"{side}-{price}"
    )


def daily_snapshot(last_close: float, supports: list[float], resistances: list[float]) -> DailyTechnicalSnapshot:
    return DailyTechnicalSnapshot(
        symbol="600519.SH",
        bar_count=120,
        last_close=last_close,
        last_open=last_close * 0.99,
        last_high=last_close * 1.01,
        last_low=last_close * 0.98,
        trend_state=TrendState.UPTREND,
        supports=[price_level("support", level) for level in supports],
        resistances=[price_level("resistance", level) for level in resistances],
        explanation="测试用快照",
        source_ts=datetime(2026, 7, 31, 15, 0),
    )


def advice_for(daily=None, rows=None, today=date(2026, 7, 31)):
    intraday = IntradayAnalyzer(today=today).analyze(
        "600519.SH", bars_from(rows if rows is not None else minute_rows("2026-07-31", 48))
    )
    return AdviceEngine().build("600519.SH", daily, intraday)


def test_entry_band_has_width() -> None:
    """A zero-width band is unfillable; the earlier version collapsed to one price."""
    advice = advice_for()

    assert advice.entry_low is not None and advice.entry_high is not None
    assert advice.entry_high > advice.entry_low


def test_stop_sits_below_the_session_low() -> None:
    advice = advice_for()
    assert advice.stop is not None and advice.entry_low is not None
    assert advice.stop < advice.entry_low


def test_levels_are_rescaled_by_whatever_the_alignment_factor_says() -> None:
    """比例乘法本身保留着，即便实盘上这个比值恒等于 1.0。

    2026-08-02 实测 400 只，日线（比例前复权）最新一根与分钟线现价的比值偏离
    1.0 超过 0.1% 的有 0 只——因为比例前复权锚定最新一根，两条序列本来就同尺度。
    乘法留在这里是为了将来万一换成后复权口径时，改一个函数就够了。
    """
    rows = minute_rows("2026-07-31", 48, start_price=100.0, step=0.1)
    intraday = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))
    assert intraday is not None

    # 日线序列被压到实际价格的一半，压力位 60 应当被还原成 120。
    daily = daily_snapshot(intraday.last_price / 2, supports=[45.0], resistances=[60.0])
    advice = AdviceEngine().build("600519.SH", daily, intraday)

    assert advice.targets
    assert any(abs(target - 120.0) < 1.0 for target in advice.targets)


def test_a_real_forward_adjusted_series_needs_no_conversion_at_all() -> None:
    """实盘口径下比值就是 1.0，价位原样发出，不该被悄悄改动。"""
    rows = minute_rows("2026-07-31", 48, start_price=100.0, step=0.1)
    intraday = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))
    assert intraday is not None

    # 比例前复权：最新一根 == 现价，历史位已在今天的尺度上。
    daily = daily_snapshot(intraday.last_price, supports=[95.0], resistances=[108.0])
    advice = AdviceEngine().build("600519.SH", daily, intraday)

    assert any(abs(target - 108.0) < 1e-6 for target in advice.targets)


def test_absurd_price_scale_disables_daily_levels_instead_of_lying() -> None:
    """比值离谱 = 两份数据根本不是同一只票的同一天，此时一个价位都不该发。"""
    rows = minute_rows("2026-07-31", 48, start_price=100.0, step=0.1)
    intraday = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))
    assert intraday is not None

    daily = daily_snapshot(0.5, supports=[0.4], resistances=[0.6])  # factor ~200x
    advice = AdviceEngine().build("600519.SH", daily, intraday)

    assert any("对不上" in warning for warning in advice.warnings)


def test_advice_explains_why_there_is_no_target() -> None:
    """At the high with no overhead resistance, say so rather than print a dash."""
    rows = minute_rows("2026-07-31", 48)
    # Close the session on a new high, so nothing intraday is left to aim at.
    rows[-1]["close"] = 10.6
    rows[-1]["high"] = 10.6
    advice = advice_for(daily=daily_snapshot(10.5, supports=[9.0], resistances=[]), rows=rows)

    assert advice.targets == []
    assert any("无法给出目标价" in warning for warning in advice.warnings)


def test_advice_without_intraday_refuses_to_quote_order_prices() -> None:
    advice = AdviceEngine().build(
        "600519.SH", daily_snapshot(10.0, supports=[9.0], resistances=[11.0]), intraday=None
    )

    assert advice.entry_low is None
    assert any("不可直接用于挂单" in warning for warning in advice.warnings)


def test_advice_always_publishes_three_scenarios() -> None:
    advice = advice_for()
    names = [scenario.name for scenario in advice.scenarios]

    assert names == ["强势延续", "均价震荡", "转弱回落"]
    assert all(scenario.level is not None for scenario in advice.scenarios)


def test_advice_with_no_data_at_all_says_so() -> None:
    advice = AdviceEngine().build("600519.SH", daily=None, intraday=None)
    assert any("无法给出任何判断" in warning for warning in advice.warnings)


# ---- chat ----------------------------------------------------------------


ADVICE_FACTS = {
    "trade_date": "2026-08-01",
    "symbols": [
        {"symbol": "600519.SH", "name": "贵州茅台", "last_price": 1350.6,
         "entry_low": 1333.93, "entry_high": 1337.53, "stop": 1285.46,
         "targets": [1355.72], "rrr": 4.08,
         "trade_context": {"turnover_ratio": 2.36, "session_amount": 12686185800.0},
         "shortline": {
             "action": "wait_trigger",
             "score": 3,
             "summary": "结构尚可，优先看回踩承接，突破再加速",
             "reasons": ["现价位于分时均价上方 0.32%", "换手率 2.36%"],
         }},
    ],
}


class FakeClient:
    def __init__(self, reply: str = "结构完好。", configured: bool = True, boom: bool = False) -> None:
        self.reply = reply
        self._configured = configured
        self.boom = boom
        self.provider = type("P", (), {"api_key_env": "DASHSCOPE_API_KEY"})()
        self.seen: list[list[dict]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def chat(self, messages):
        self.seen.append(messages)
        if self.boom:
            raise TimeoutError("upstream")
        return self.reply


def session_with_facts() -> ChatSession:
    session = ChatSession()
    session.add_symbols(["600519.SH"])
    session.set_facts("advice", ADVICE_FACTS)
    return session


def test_chat_answers_from_the_fact_bundle() -> None:
    client = FakeClient("现价 1350.6，止损 1285.46。")
    answer, notes = ChatResponder(client).answer(session_with_facts(), "现在什么情况")

    assert "1350.6" in answer
    assert notes == []
    # The facts are re-sent every turn rather than relied on from history.
    assert "1350.6" in client.seen[0][-1]["content"]


def test_chat_discards_an_answer_that_invents_a_number() -> None:
    client = FakeClient("目标价 1800.0，上涨概率 72%。")
    answer, notes = ChatResponder(client).answer(session_with_facts(), "能到多少")

    assert "已丢弃" in answer
    assert notes and "数据中不存在的数字" in notes[0]


def test_chat_records_history_only_for_accepted_answers() -> None:
    session = session_with_facts()
    responder = ChatResponder(FakeClient("目标价 1800.0。"))
    responder.answer(session, "能到多少")

    assert session.history == []


def test_chat_reports_a_failed_call_without_raising() -> None:
    answer, notes = ChatResponder(FakeClient(boom=True)).answer(session_with_facts(), "怎么样")
    assert "调用失败" in answer and notes


def test_chat_without_a_key_points_at_the_offline_path() -> None:
    answer, _ = ChatResponder(FakeClient(configured=False)).answer(session_with_facts(), "怎么样")
    assert "/show" in answer


def test_chat_defaults_to_facts_only_without_a_client() -> None:
    answer, notes = ChatResponder().answer(session_with_facts(), "怎么样")

    assert "## 买卖点" in answer
    assert notes and "--llm" in notes[0]


def test_chat_with_empty_context_asks_for_symbols() -> None:
    answer, _ = ChatResponder(FakeClient()).answer(ChatSession(), "怎么样")
    assert "/add" in answer


def test_add_command_computes_and_reports() -> None:
    session = ChatSession()
    calls: list[list[str]] = []

    def compute(symbols):
        calls.append(list(symbols))
        return ADVICE_FACTS

    message = handle_command(session, "/add 600519.SH", compute, dict)

    assert calls == [["600519.SH"]]
    assert "600519.SH" in message
    assert session.facts["advice"] == ADVICE_FACTS


def test_drop_command_removes_the_symbol_from_facts_too() -> None:
    session = session_with_facts()
    handle_command(session, "/drop 600519.SH", lambda s: ADVICE_FACTS, dict)

    assert session.symbols == []
    assert session.facts["advice"]["symbols"] == []


def test_refresh_command_needs_a_non_empty_context() -> None:
    message = handle_command(ChatSession(), "/refresh", lambda s: ADVICE_FACTS, dict)
    assert "上下文为空" in message


def test_clear_keeps_facts_and_drops_history() -> None:
    session = session_with_facts()
    session.record("user", "hi")
    handle_command(session, "/clear", lambda s: ADVICE_FACTS, dict)

    assert session.history == []
    assert session.has_facts


def test_show_renders_without_touching_the_llm() -> None:
    summary = render_facts_summary(session_with_facts())
    assert "600519.SH" in summary and "1285.46" in summary


def test_show_surfaces_shortline_judgement_and_trade_context() -> None:
    summary = render_facts_summary(session_with_facts())

    assert "结构尚可" in summary
    assert "换手率 2.36%" in summary
    assert "成交额 126.86 亿" in summary


def test_show_renders_a_sector_bundle_as_a_human_summary() -> None:
    session = ChatSession()
    session.set_facts(
        "sectors",
        {
            "market_direction": "偏多",
            "market_score": 79.8,
            "position_factor": 0.68,
            "sectors_selected": ["传媒", "计算机"],
            "candidate_symbols": ["300588.SZ", "688621.SH"],
            "limit_up": 99,
            "blowups": 107,
            "max_streak": 9,
        },
    )

    summary = render_facts_summary(session)

    assert "盘前主链路" in summary
    assert "先看板块" in summary
    assert "300588.SZ" in summary


def test_unknown_command_is_reported_not_executed() -> None:
    assert "未知命令" in handle_command(ChatSession(), "/nope", lambda s: {}, dict)


def test_debate_command_does_not_raise_on_an_empty_context() -> None:
    """`/debate` 此前**每一次调用都抛 TypeError**。

    `has_facts` 是 property，写成 `session.has_facts()` 等于去调用一个 bool。
    上下文空不空都一样炸——空的时候 `not False` 先求值，`False()` 照样抛。

    它活下来是因为这条命令一条测试都没有：辩论层本身测过（`test_automation.py`
    里四条），但**从对话入口进去的那条路**没测过。加这条不是为了这一个 bug，
    是为了那条路以后一直有人走。
    """
    assert "上下文为空" in handle_command(ChatSession(), "/debate", lambda s: {}, dict)


def test_debate_command_reports_a_missing_key_instead_of_failing() -> None:
    message = handle_command(session_with_facts(), "/debate", lambda s: {}, dict)
    assert "未配置 LLM key" in message


# ---- freshness probe -----------------------------------------------------


def test_in_session_matches_the_a_share_clock() -> None:
    assert in_session(datetime(2026, 7, 31, 10, 0))       # Friday morning
    assert in_session(datetime(2026, 7, 31, 14, 0))       # Friday afternoon
    assert not in_session(datetime(2026, 7, 31, 12, 0))   # lunch
    assert not in_session(datetime(2026, 8, 1, 10, 0))    # Saturday


class StubIntraday:
    def __init__(self, first_last: datetime, second_last: datetime | None = None) -> None:
        self.first_last = first_last
        self.second_last = second_last or first_last
        self.calls = 0

    def get_minute_bars(self, symbols, period="1", max_bars=None):
        self.calls += 1
        stamp = self.first_last if self.calls == 1 else self.second_last
        return ({symbol: [{"timestamp": stamp, "close": 10.0}] for symbol in symbols}, [])


def test_freshness_refuses_to_judge_outside_a_session() -> None:
    stub = StubIntraday(datetime(2026, 7, 31, 15, 0))
    report = check_intraday_freshness(
        stub, ["600519.SH"], now=datetime(2026, 8, 1, 10, 0), sleep=lambda _: None
    )

    assert not report["in_session"]
    assert "非交易时段" in report["verdict"]
    assert stub.calls == 1  # no second sample outside a session


def test_freshness_passes_a_live_feed() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    stub = StubIntraday(now - timedelta(minutes=1), now)
    report = check_intraday_freshness(stub, ["600519.SH"], now=now, sleep=lambda _: None)

    assert report["in_session"]
    assert "暂不需要付费" in report["verdict"]


def test_freshness_flags_a_feed_that_does_not_advance() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    frozen = now - timedelta(minutes=1)
    report = check_intraday_freshness(
        StubIntraday(frozen, frozen), ["600519.SH"], now=now, sleep=lambda _: None
    )

    assert "没有前进" in report["verdict"]
    assert "付费" in report["verdict"]


def test_freshness_flags_an_unusable_lag() -> None:
    now = datetime(2026, 7, 31, 14, 0)
    stub = StubIntraday(now - timedelta(minutes=20), now - timedelta(minutes=18))
    report = check_intraday_freshness(stub, ["600519.SH"], now=now, sleep=lambda _: None)

    assert "建议开通付费档" in report["verdict"]
