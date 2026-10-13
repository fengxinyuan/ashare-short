"""兑现核对：建议发出后 N 个交易日，实际发生了什么。

价位百分比必须和参考时点对齐。收盘参考价对应参考日收盘；分时参考价通过保存的
同日开盘价换算到日线复权尺度，不能用之后的收盘价重设入场、止损和目标。

信号口径描述参考时点之后的价格路径；执行口径按日线模拟成交、T+1 和跳空。
日线无法核验盘中确认、排队及滑点，复权价格也不等同于含现金分红的账户收益。
模拟成交、已平仓毛收益和未平仓估值分别保存，不解释为真实成交记录。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.journal.records import KIND_RECOMMENDATION, KIND_VETOED, Outcome
from ashare_decision_support.journal.store import Journal
from ashare_decision_support.models.enums import Playbook
from ashare_decision_support.numeric import finite, positive
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.signals.series import bar_date, parse_bar_date
from ashare_decision_support.symbols import normalize_symbol

DEFAULT_HORIZON_SESSIONS = 5
_DAILY_PLAN_SOURCES = frozenset({"morning", "post_close"})

# 执行口径重放的成交依据。写成常量是因为统计层要按它分组（`journal/stats.py`），
# 两边各写一份字面量的话，改一处漏一处不会报错，只会让「未成交」悄悄变成一个新桶。
EXEC_FILL_OPEN = "开盘成交"
EXEC_FILL_LIMIT = "限价成交"
EXEC_NOT_FILLED = "限价未成交"
EXEC_SKIP_STOP = "开盘已在止损下方，不进场"
EXEC_SKIP_TARGET = "开盘已越过目标，不追"
EXEC_NO_OPEN = "缺开盘价，无法重放"
EXEC_ABSTAINED = "裁决弃权，不建仓"
EXEC_VETOED = "风控否决，只观察"
EXEC_NO_SETUP = "无有效打法，不建仓"
EXEC_NO_PLAN = "缺少有效入场、止损或目标，无法重放"
EXEC_INVALID_PLAN = "买入区间、止损与目标关系无效"
EXEC_BELOW_BAND = "开盘跌穿买入区间，计划失效"
EXEC_ABOVE_BREAKOUT = "开盘越过突破买入上限，不追"
EXEC_FILL_BREAKOUT = "突破触发成交"
EXEC_BREAKOUT_NOT_TRIGGERED = "未触及突破触发价"
# 一字涨停：全天只有一个价，买单排在封单后面，事实上买不到。日线里它长这样：
# 最高 == 最低，且收盘高于前收。跌停一字对称，卖不出（`_first_exit`）。
EXEC_LOCKED_UP = "一字涨停买不进"

# 执行口径的离场方式。`t1_locked` 只在核对窗口为 1 时出现：成交当日 T+1 锁定，
# 窗口结束时仓位还在手上，收益是按收盘价的浮动盈亏，不是已实现的。
EXIT_STOP = "stop"
EXIT_TARGET = "target"
EXIT_TIME = "time"
EXIT_T1_LOCKED = "t1_locked"
EXIT_LIMIT_LOCKED = "limit_locked"
EXIT_UNCERTAIN = "unresolved"
CLOSED_EXITS = (EXIT_STOP, EXIT_TARGET, EXIT_TIME)
EXECUTION_MODEL_VERSION = "daily-ohlc-v2"

# 老记录没有 `reference_date` 时，拿参考价去对日线：和哪一根收盘对得上就锚在哪一根。
# 容差取 0.1%：没经过复权调整的记录会精确相等，差的只是浮点舍入；复权因子变过之后
# 整段序列一起偏移，两根都对不上——那就维持旧口径，不猜。
LEGACY_ANCHOR_TOLERANCE = 0.001


def verify_pending(
    journal: Journal,
    adapter: Any,
    today: date | None = None,
    horizon: int = DEFAULT_HORIZON_SESSIONS,
    min_age_days: int = 1,
) -> dict[str, Any]:
    """核对完整窗口并写回台账；未完成的记录附原因，保留待下次重试。"""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("核对窗口必须是正整数（交易日）")
    now = today or beijing_today()
    pending = journal.pending(now, min_age_days=min_age_days)
    if not pending:
        return {
            "checked": 0,
            "resolved": 0,
            "outcomes": [],
            "deferred": [],
            "notes": ["没有待核对的建议"],
        }

    symbols = sorted(
        {
            normalize_symbol(row["symbol"])
            for row in pending
            if isinstance(row.get("symbol"), str) and row["symbol"].strip()
        }
    )
    earliest = min(parse_bar_date(row["trade_date"]) for row in pending)
    start = earliest - timedelta(days=10)
    notes: list[str] = []
    try:
        bars = fetch_daily_bars(adapter, symbols, start, now, notes) if symbols else {}
    except Exception as exc:
        return {
            "checked": len(pending),
            "resolved": 0,
            "outcomes": [],
            "deferred": [_deferred(row, "日线获取失败") for row in pending],
            "notes": [f"取日线失败，本次不核对：{type(exc).__name__}: {exc}"],
        }

    outcomes: list[Outcome] = []
    deferred: list[dict] = []
    for row in pending:
        symbol = row.get("symbol")
        rows = bars.get(normalize_symbol(symbol)) if isinstance(symbol, str) else []
        try:
            outcome = _resolve(row, rows or [], now, horizon)
        except ValueError as exc:
            deferred.append(_deferred(row, str(exc)))
            continue
        execution = outcome.execution
        if execution.get("basis") == EXEC_NO_OPEN or execution.get("exit") == EXIT_UNCERTAIN:
            deferred.append(_deferred(row, "执行窗口缺少开盘价，需补齐日线后重试"))
            continue
        outcomes.append(outcome)

    journal.record_many(list(outcomes))
    for reason, count in Counter(item["reason"] for item in deferred).items():
        notes.append(f"{reason}：{count} 条，留待下次核对")
    return {
        "checked": len(pending),
        "resolved": len(outcomes),
        "outcomes": [item.to_dict() for item in outcomes],
        "deferred": deferred,
        "notes": notes,
    }


def _deferred(row: dict, reason: str) -> dict:
    return (
        {key: row.get(key) for key in ("symbol", "trade_date", "source")}
        | {
            key: row[key]
            for key in ("decision_id", "revision", "strategy_version", "plan_id")
            if row.get(key) is not None
        }
        | {"reason": reason}
    )


def _anchor_date(row: dict, trade_date: date, dated: list[tuple[dict, date]]) -> date:
    """百分比价位锚定在哪一根日线的收盘上。

    盘前晨报的参考价是**上一交易日**收盘（`jobs/morning_record.py`），`trade_date`
    是计划要执行的那一天。按 `trade_date` 的收盘锚定会犯两个错：止损/目标的百分比
    被套到一根建议发出时还不存在的 K 线上（它已经含了计划日的涨跌），并且计划日
    本身——建议发出后的第一个交易日——被整个跳过。

    新记录带 `reference_date`，核对从它的下一根开始；晚于 `trade_date` 的值是坏数据，
    当作没有。没有这个字段的老记录用 `reference_price` 去对：先看 `trade_date` 那根
    （盘后问价的参考价就是当日收盘，两根都对得上时维持旧口径），再看它前一根；
    都对不上就回退到 `trade_date`。
    """
    explicit = parse_bar_date(row.get("reference_date"))
    if explicit is not None and explicit <= trade_date:
        return explicit

    reference = positive(row.get("reference_price"))
    if reference is None:
        return trade_date
    closes = {stamp: positive(bar.get("close")) for bar, stamp in dated if stamp <= trade_date}
    previous = max((stamp for stamp in closes if stamp < trade_date), default=None)
    for candidate in (trade_date, previous):
        close = closes.get(candidate) if candidate is not None else None
        if close is not None and abs(close / reference - 1.0) <= LEGACY_ANCHOR_TOLERANCE:
            return candidate
    return trade_date


def _verification_window(
    row: dict,
    bars: list[dict],
    today: date,
    horizon: int,
) -> tuple[date, date, float, list[dict]]:
    if not isinstance(row.get("symbol"), str) or not row["symbol"].strip():
        raise ValueError("建议缺少有效代码")
    if not isinstance(row.get("source"), str) or not row["source"].strip():
        raise ValueError("建议缺少来源，无法关联兑现记录")
    for key in ("dip", "market_state", "trade_flow", "adjudication", "macro_context", "setup"):
        if row.get(key) is not None and not isinstance(row[key], dict):
            raise ValueError(f"建议特征格式无效（{key}）")
    trade_date = parse_bar_date(row.get("trade_date"))
    if trade_date is None:
        raise ValueError("建议日期无效")
    if not bars:
        raise ValueError("缺少日线")

    dated = [(bar, stamp) for bar, stamp in _dated_bars(bars) if stamp <= today]
    anchor_date = _anchor_date(row, trade_date, dated)
    anchor = next((bar for bar, stamp in dated if stamp == anchor_date), {})
    base = _reference_base(row, anchor)

    # 参考日只是价位的锚，不能把建议发出前的行情算成事后表现。盘前计划可从
    # 计划日开始；分时问价只能用之后的完整日线，不能倒推当日开盘成交。
    forward = [
        (bar, stamp)
        for bar, stamp in dated
        if stamp > anchor_date
        and (stamp >= trade_date if row["source"] == "morning" else stamp > trade_date)
    ][:horizon]
    # 停牌或取数残缺时，自然日过去再久也不能把一日数据当成五日结果永久定案。
    if len(forward) < horizon:
        raise ValueError(f"兑现窗口未满 {horizon} 个交易日")
    previous_closes = {
        stamp: positive(dated[index - 1][0].get("close"))
        for index, (_, stamp) in enumerate(dated)
        if index
    }
    return (
        trade_date,
        anchor_date,
        base,
        [
            _window_bar(bar, stamp) | {"previous_close": previous_closes.get(stamp)}
            for bar, stamp in forward
        ],
    )


def _reference_base(row: dict, anchor: dict) -> float:
    reference_open = row.get("reference_open_price")
    if reference_open is not None:
        opening = positive(reference_open)
        adjusted_open = positive(anchor.get("open"))
        reference = positive(row.get("reference_price"))
        if opening is None or adjusted_open is None or reference is None:
            raise ValueError("参考时点的开盘价缺失或无效，无法对齐复权尺度")
        base = positive(reference * (adjusted_open / opening))
        if base is None:
            raise ValueError("参考价格比例无效")
        return base
    base = positive(anchor.get("close"))
    if base is None:
        raise ValueError("建议日收盘缺失或无效")
    return base


def _resolve(row: dict, bars: list[dict], today: date, horizon: int) -> Outcome:
    trade_date, anchor_date, base, forward = _verification_window(row, bars, today, horizon)

    entry_low = _level(row.get("entry_low_pct"), base)
    entry_high = _level(row.get("entry_high_pct"), base)
    stop = _level(row.get("stop_pct"), base)
    target_pcts = row.get("target_pcts") or []
    if not isinstance(target_pcts, list):
        raise ValueError("建议目标比例格式无效")
    targets = [_level(pct, base) for pct in target_pcts]
    first_target = min((level for level in targets if level is not None), default=None)

    entered = False
    stop_hit = False
    target_hit = False
    first_hit: str | None = None
    highs: list[float] = []
    lows: list[float] = []

    for bar in forward:
        high, low = bar["high"], bar["low"]
        highs.append(high)
        lows.append(low)
        if _touches_entry(bar, entry_low, entry_high, row.get("playbook")):
            entered = True
        if stop is not None and low <= stop:
            stop_hit = True
            if first_hit is None:
                first_hit = "stop"
        if first_target is not None and high >= first_target:
            target_hit = True
            if first_hit is None:
                first_hit = "target"

    # T+1 口径。forward[0] 就是建议发出后的第一个交易日——「买入后第二天卖」
    # 这条策略的全部收益都落在这一根 bar 里，而 5 日 MFE/MAE 会把它稀释掉。
    # 开盘单独取：持仓过夜的收益里隔夜跳空常常比日内择时贡献更大，两者不拆开
    # 就无法归因。
    t1 = forward[0]
    return Outcome(
        symbol=normalize_symbol(row["symbol"]),
        trade_date=str(trade_date),
        source=row["source"],
        resolved_on=str(today),
        sessions=len(forward),
        entered=entered,
        stop_hit=stop_hit,
        target_hit=target_hit,
        first_hit=first_hit,
        mfe_pct=_relative(max(highs), base),
        mae_pct=_relative(min(lows), base),
        close_pct=_relative(forward[-1]["close"], base),
        next_open_pct=_relative(t1.get("open"), base),
        next_high_pct=_relative(t1.get("high"), base),
        next_low_pct=_relative(t1.get("low"), base),
        next_close_pct=_relative(t1.get("close"), base),
        score=row.get("score"),
        sector=row.get("sector"),
        trend_state=row.get("trend_state"),
        wyckoff_bias=row.get("wyckoff_bias"),
        playbook=row.get("playbook"),
        rec_kind=row.get("kind") or KIND_RECOMMENDATION,
        dip=row.get("dip") or {},
        market_state=row.get("market_state") or {},
        trade_flow=row.get("trade_flow") or {},
        adjudication=row.get("adjudication") or {},
        macro_context=row.get("macro_context") or {},
        decision_id=row.get("decision_id"),
        recorded_at=row.get("recorded_at"),
        strategy_version=row.get("strategy_version"),
        plan_id=row.get("plan_id"),
        revision=row.get("revision"),
        strategy_revision=row.get("strategy_revision"),
        anchor_date=str(anchor_date),
        rrr=finite(row.get("rrr")),
        stop_pct=finite(row.get("stop_pct")),
        execution=_replay_execution(row, base, entry_low, entry_high, stop, first_target, forward),
    )


def _replay_execution(
    row: dict,
    base: float,
    entry_low: float | None,
    entry_high: float | None,
    stop: float | None,
    target: float | None,
    forward: list[dict],
) -> dict[str, Any]:
    """只模拟首个可用交易日的价格触发；未成交不顺延，确认信号仍需分钟数据。"""
    first = forward[0]
    result: dict[str, Any] = {
        "model_version": EXECUTION_MODEL_VERSION,
        "simulation": True,
        "transaction_costs_included": False,
        "slippage_included": False,
        "price_alignment": _price_alignment(row),
        "entry_model": "open_proxy" if entry_high is None else "price_trigger",
        "eligible": True,
        "replayable": True,
        "filled": False,
        "plan_session": first["date"],
        "fill_session": None,
        "t_plus_one": True,
    }
    block = _entry_plan_block(row, entry_low, entry_high, stop, target)
    if block:
        return {**result, "eligible": False, "replayable": False, "basis": block}
    opening = first.get("open")
    if opening is None:
        return {**result, "replayable": False, "basis": EXEC_NO_OPEN}
    previous_close = positive(first.get("previous_close")) or base
    if _one_price(first) and first["close"] > previous_close:
        return {**result, "basis": EXEC_LOCKED_UP, "open_pct": _relative(opening, base)}
    if stop is not None and opening <= stop:
        return {**result, "basis": EXEC_SKIP_STOP, "open_pct": _relative(opening, base)}
    if target is not None and opening >= target:
        return {**result, "basis": EXEC_SKIP_TARGET, "open_pct": _relative(opening, base)}

    candidate = _entry_fill(first, entry_low, entry_high, row.get("playbook"))
    if isinstance(candidate, str):
        return {**result, "basis": candidate}
    fill, basis, at_open = candidate
    exit_price, exit_kind, exit_index, gap_exit, locked = _first_exit(forward, stop, target)
    realized = exit_kind in CLOSED_EXITS
    change = None if exit_kind == EXIT_UNCERTAIN else _relative(exit_price, fill)
    mfe, mae = _execution_excursions(
        forward[: exit_index + 1], fill, exit_price, exit_kind, at_open, gap_exit
    )
    return {
        **result,
        "filled": True,
        "basis": basis,
        "fill_session": first["date"],
        "fill_at_open": at_open,
        "fill_pct": _relative(fill, base),
        "exit": exit_kind,
        "realized": realized,
        "exit_session": forward[exit_index]["date"] if realized else None,
        "valuation_session": forward[exit_index]["date"],
        "gap_exit": gap_exit,
        "hold_sessions": exit_index,
        "locked_sessions": locked,
        # return_pct 保留旧字段；消费者必须同时读取 realized / exit。
        "return_pct": change,
        "realized_return_pct": change if realized else None,
        "unrealized_return_pct": change if not realized else None,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "excursion_basis": "持有日高低点；盘中买入日高点不计；离场日只计开盘和成交价",
    }


def _price_alignment(row: dict) -> str:
    if row.get("reference_open_price") is not None:
        return "session_open"
    return "reference_close" if row.get("source") in _DAILY_PLAN_SOURCES else "legacy_close_proxy"


def _touches_entry(bar: dict, low: float | None, high: float | None, playbook: str | None) -> bool:
    if high is None:
        return False
    if playbook == Playbook.BREAKOUT.value:
        return bar["high"] >= (high if low is None else low)
    return bar["low"] <= high and (low is None or bar["high"] >= low)


def _entry_plan_block(
    row: dict,
    low: float | None,
    high: float | None,
    stop: float | None,
    target: float | None,
) -> str | None:
    if (row.get("adjudication") or {}).get("abstained"):
        return EXEC_ABSTAINED
    if row.get("kind") == KIND_VETOED:
        return EXEC_VETOED
    setup = row.get("setup") or {}
    if setup and not (row.get("playbook") or setup.get("primary") or setup.get("matched")):
        return EXEC_NO_SETUP
    if stop is None or target is None:
        return EXEC_NO_PLAN
    if high is None:
        if low is not None or row.get("source") not in _DAILY_PLAN_SOURCES:
            return EXEC_NO_PLAN
        return None if stop < target else EXEC_INVALID_PLAN
    floor = high if low is None else low
    return None if stop < floor <= high < target else EXEC_INVALID_PLAN


def _entry_fill(
    first: dict,
    low: float | None,
    high: float | None,
    playbook: str | None,
) -> tuple[float, str, bool] | str:
    opening = first["open"]
    if high is None:
        return opening, EXEC_FILL_OPEN, True
    if playbook == Playbook.BREAKOUT.value:
        trigger = high if low is None else low
        if opening > high:
            return EXEC_ABOVE_BREAKOUT
        if first["high"] < trigger:
            return EXEC_BREAKOUT_NOT_TRIGGERED
        return max(opening, trigger), EXEC_FILL_BREAKOUT, opening >= trigger
    if low is not None and opening < low:
        return EXEC_BELOW_BAND
    if opening <= high:
        return opening, EXEC_FILL_LIMIT, True
    if first["low"] <= high:
        return high, EXEC_FILL_LIMIT, False
    return EXEC_NOT_FILLED


def _execution_excursions(
    held: list[dict],
    fill: float,
    exit_price: float,
    exit_kind: str,
    at_open: bool,
    gap_exit: bool,
) -> tuple[float | None, float | None]:
    """日线下的有利下界和不利估计；不把开盘已卖出之后的涨跌算进持有期。"""
    if exit_kind == EXIT_UNCERTAIN:
        return None, None
    terminal_fill = exit_kind in (EXIT_STOP, EXIT_TARGET)
    complete = held[:-1] if terminal_fill else held
    highs, lows = [fill], [fill]
    for index, bar in enumerate(complete):
        highs.append(bar["high"] if index or at_open else max(fill, bar["close"]))
        lows.append(bar["low"])
    if terminal_fill:
        terminal = held[-1]
        prices = [exit_price] if gap_exit else [terminal["open"], exit_price]
        highs.extend(prices)
        lows.extend(prices)
    return _relative(max(highs), fill), _relative(min(lows), fill)


def _one_price(bar: dict) -> bool:
    """全天单一价格：缺盘口时按可能封板保守处理，不等于已核验交易所涨跌停价。"""
    return bar["high"] == bar["low"]


def _first_exit(
    forward: list[dict],
    stop: float | None,
    target: float | None,
) -> tuple[float, str, int, bool, int]:
    """日线模拟离场；未能卖出的止损继续挂起，期末仍锁住则只保留估值。"""
    locked = 0
    stop_pending = False
    last_locked = False
    for index in range(1, len(forward)):
        bar = forward[index]
        if bar.get("open") is None:
            return bar["close"], EXIT_UNCERTAIN, index, False, locked
        limit_down = _one_price(bar) and bar["close"] < forward[index - 1]["close"]
        candidate = _bar_exit(bar, stop, target, stop_pending)
        last_locked = limit_down and (candidate is not None or index == len(forward) - 1)
        if last_locked:
            locked += 1
            stop_pending = stop_pending or (candidate is not None and candidate[1] == EXIT_STOP)
            continue
        if candidate is not None:
            price, kind, gap = candidate
            return price, kind, index, gap, locked
    if len(forward) == 1:
        return forward[0]["close"], EXIT_T1_LOCKED, 0, False, locked
    kind = EXIT_LIMIT_LOCKED if last_locked else EXIT_TIME
    return forward[-1]["close"], kind, len(forward) - 1, False, locked


def _bar_exit(
    bar: dict, stop: float | None, target: float | None, stop_pending: bool
) -> tuple[float, str, bool] | None:
    opening = bar["open"]
    if stop_pending or (stop is not None and opening <= stop):
        return opening, EXIT_STOP, True
    if target is not None and opening >= target:
        return opening, EXIT_TARGET, True
    # 同一根同时触及两侧时无法判断先后，按先止损的情景估计。
    if stop is not None and bar["low"] <= stop:
        return stop, EXIT_STOP, False
    if target is not None and bar["high"] >= target:
        return target, EXIT_TARGET, False
    return None


def _relative(value: Any, base: float) -> float | None:
    number = positive(value)
    if number is None:
        return None
    change = finite(number / base - 1.0)
    if change is None:
        raise ValueError("兑现窗口价格比例无效")
    return round(change, 6)


def _level(pct: Any, base: float) -> float | None:
    if pct is None:
        return None
    number = finite(pct)
    level = None if number is None else positive(base * (1.0 + number))
    if level is None:
        raise ValueError("建议价位比例无效")
    return level


def _window_bar(bar: dict, stamp: date) -> dict[str, Any]:
    """检验窗口内每一日，不丢掉坏行后拿更晚的一日冒充 T+1/T+5。"""
    values = {key: positive(bar.get(key)) for key in ("open", "high", "low", "close")}
    high, low, close, opening = (values[key] for key in ("high", "low", "close", "open"))
    if high is None or low is None or close is None or not low <= close <= high:
        raise ValueError("兑现窗口价格无效，需补齐或修复日线")
    if bar.get("open") is not None and (opening is None or not low <= opening <= high):
        raise ValueError("兑现窗口价格无效，需补齐或修复日线")
    return {"date": stamp.isoformat(), **values}


def _dated_bars(bars: list[dict]) -> list[tuple[dict, date]]:
    """使用统一日期解析；每个交易日只取最后一条，和日线缓存一致。"""
    if not isinstance(bars, list):
        return []
    dated = {
        stamp: bar for bar in bars if isinstance(bar, dict) and (stamp := bar_date(bar)) is not None
    }
    return [(dated[stamp], stamp) for stamp in sorted(dated)]
