"""Backtest the third-party candidate formula on TickFlow daily bars.

The formula contains two live-only or current-metadata inputs:

* ``DYNAINFO(17)`` is replaced by a close-of-day proxy:
  today's volume / mean volume of the previous five sessions.
* ``CAPITAL``/``FINANCE(40)`` are evaluated with the latest float shares
  returned by TickFlow. This is explicitly a proxy and is not a point-in-time
  historical financial series.

The script reports the literal ``AMOUNT/V`` version separately from the
unit-corrected ``AMOUNT/(V*100)`` version. TickFlow daily volume is in lots,
while amount is in yuan.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter
from ashare_decision_support.numeric import finite

START_DEFAULT = date(2025, 8, 1)
END_DEFAULT = date(2026, 7, 31)
TARGET_PCT = 0.03
STOP_PCT = 0.03
ROUND_TRIP_COST = 0.002


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _row_value(row: dict, key: str) -> float | None:
    return finite(row.get(key))


def _formula_signal(
    rows: list[dict],
    index: int,
    float_shares: float | None,
    literal_average_price: bool,
    use_metadata_filters: bool,
    closes: list[float],
    volumes: list[float],
    ema3: list[float],
    ema8: list[float],
) -> bool:
    if index < 20 or index + 1 >= len(rows):
        return False

    if any(value <= 0 for value in closes[index - 20 : index + 1]):
        return False
    if any(value <= 0 for value in volumes[index - 5 : index + 1]):
        return False

    close = closes[index]
    prev_close = closes[index - 1]
    volume = volumes[index]
    prev_volume = volumes[index - 1]
    prev_prev_volume = volumes[index - 2]
    high = _row_value(rows[index], "high")
    low = _row_value(rows[index], "low")
    amount = _row_value(rows[index], "amount")
    if None in (high, low, amount):
        return False

    ma5 = _mean(closes[index - 4 : index + 1])
    ma10 = _mean(closes[index - 9 : index + 1])
    ma20 = _mean(closes[index - 19 : index + 1])
    prev_ma5 = _mean(closes[index - 5 : index])
    volume_ratio = volume / (_mean(volumes[index - 5 : index]) or 0.0)
    if None in (ma5, ma10, ma20, prev_ma5) or volume_ratio <= 0:
        return False

    momentum = ema3[index] - ema8[index]
    previous_momentum = ema3[index - 1] - ema8[index - 1]

    average_price = amount / (volume * (1.0 if literal_average_price else 100.0))
    amplitude = (high - low) / prev_close

    technical = (
        ma5 > ma10 > ma20
        and 1.02 < close / prev_close < 1.053
        and 1.18 < volume_ratio < 3.15
        and close > ma5
        and low > prev_ma5
        and amplitude < 0.078
        and (prev_volume > prev_prev_volume or volume > prev_volume * 0.85)
        and abs(close - average_price) / average_price < 0.036
        and momentum > previous_momentum
    )
    if not technical:
        return False

    if not use_metadata_filters:
        return True
    if float_shares is None or float_shares <= 0:
        return False

    turnover = volume / (float_shares / 100.0) * 100.0
    market_cap_yi = close * float_shares / 100_000_000.0
    return 2.8 < turnover < 10.6 and 38.5 < market_cap_yi < 232.0


def _trade_outcome(rows: list[dict], index: int) -> dict[str, float | bool | str] | None:
    if index + 1 >= len(rows):
        return None
    entry_row = rows[index + 1]
    entry = _row_value(entry_row, "open")
    close_1 = _row_value(entry_row, "close")
    high_1 = _row_value(entry_row, "high")
    low_1 = _row_value(entry_row, "low")
    if None in (entry, close_1, high_1, low_1) or entry <= 0:
        return None

    outcome: dict[str, float | bool | str] = {
        "t1_close_return": close_1 / entry - 1.0,
        "t1_high_return": high_1 / entry - 1.0,
        "t1_low_return": low_1 / entry - 1.0,
        "t1_target_hit": high_1 >= entry * (1.0 + TARGET_PCT),
        "t1_stop_hit": low_1 <= entry * (1.0 - STOP_PCT),
    }
    if outcome["t1_target_hit"] and outcome["t1_stop_hit"]:
        outcome["first_hit"] = "ambiguous_conservative_stop"
    elif outcome["t1_target_hit"]:
        outcome["first_hit"] = "target"
    elif outcome["t1_stop_hit"]:
        outcome["first_hit"] = "stop"
    else:
        outcome["first_hit"] = "none"

    for days in (3, 5):
        row_index = index + days
        if row_index < len(rows):
            close = _row_value(rows[row_index], "close")
            if close is not None:
                outcome[f"t{days}_close_return"] = close / entry - 1.0
    return outcome


def _metadata_pass(
    rows: list[dict], index: int, float_shares: float | None
) -> bool:
    if float_shares is None or float_shares <= 0:
        return False
    close = _row_value(rows[index], "close")
    volume = _row_value(rows[index], "volume")
    if close is None or close <= 0 or volume is None or volume <= 0:
        return False
    turnover = volume / (float_shares / 100.0) * 100.0
    market_cap_yi = close * float_shares / 100_000_000.0
    return 2.8 < turnover < 10.6 and 38.5 < market_cap_yi < 232.0


def _summarize(trades: list[dict[str, float | bool | str]]) -> dict[str, Any]:
    if not trades:
        return {
            "signals": 0,
            "signal_days": 0,
            "signals_per_signal_day": None,
        }

    t1_close = [float(row["t1_close_return"]) for row in trades]
    t1_high = [float(row["t1_high_return"]) for row in trades]
    t1_net = [value - ROUND_TRIP_COST for value in t1_close]
    target = sum(bool(row["t1_target_hit"]) for row in trades)
    stop = sum(bool(row["t1_stop_hit"]) for row in trades)
    first_hit = {
        key: sum(row["first_hit"] == key for row in trades)
        for key in ("target", "stop", "ambiguous_conservative_stop", "none")
    }
    summary: dict[str, Any] = {
        "signals": len(trades),
        "signal_days": len({row["signal_date"] for row in trades if "signal_date" in row}),
        "signals_per_signal_day": round(
            len(trades)
            / max(1, len({row["signal_date"] for row in trades if "signal_date" in row})),
            3,
        ),
        "t1_close_win_rate": round(sum(value > 0 for value in t1_close) / len(t1_close), 4),
        "t1_close_net_win_rate_after_0_2pct": round(
            sum(value > 0 for value in t1_net) / len(t1_net), 4
        ),
        "t1_high_touch_2pct_rate": round(
            sum(value >= 0.02 for value in t1_high) / len(t1_high), 4
        ),
        "t1_target_3pct_rate": round(target / len(trades), 4),
        "t1_stop_3pct_rate": round(stop / len(trades), 4),
        "t1_mean_close_return": round(statistics.fmean(t1_close), 6),
        "t1_median_close_return": round(statistics.median(t1_close), 6),
        "t1_mean_net_return_after_0_2pct": round(statistics.fmean(t1_net), 6),
        "first_hit_counts": first_hit,
    }
    for days in (3, 5):
        values = [
            float(row[f"t{days}_close_return"])
            for row in trades
            if f"t{days}_close_return" in row
        ]
        if values:
            summary[f"t{days}_close_win_rate"] = round(
                sum(value > 0 for value in values) / len(values), 4
            )
            summary[f"t{days}_mean_close_return"] = round(statistics.fmean(values), 6)
    return summary


def _monthly_summary(
    trades: list[dict[str, float | bool | str]]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, float | bool | str]]] = {}
    for trade in trades:
        signal_date = str(trade.get("signal_date", ""))
        month = signal_date[:7]
        if month:
            grouped.setdefault(month, []).append(trade)
    return {month: _summarize(rows) for month, rows in sorted(grouped.items())}


def _backtest_variant(
    bars_by_symbol: dict[str, list[dict]],
    float_shares: dict[str, float | None],
    literal_average_price: bool,
    use_metadata_filters: bool,
) -> dict[str, Any]:
    trades: list[dict[str, float | bool | str]] = []
    eligible: list[dict[str, float | bool | str]] = []
    metadata_eligible: list[dict[str, float | bool | str]] = []
    for symbol, rows in bars_by_symbol.items():
        rows = sorted(rows, key=lambda row: str(row.get("timestamp") or row.get("date") or ""))
        closes_raw = [_row_value(row, "close") for row in rows]
        volumes_raw = [_row_value(row, "volume") for row in rows]
        if any(value is None for value in closes_raw) or any(value is None for value in volumes_raw):
            continue
        closes = [float(value) for value in closes_raw]
        volumes = [float(value) for value in volumes_raw]
        ema3 = _ema(closes, 3)
        ema8 = _ema(closes, 8)
        for index in range(20, len(rows) - 1):
            outcome = _trade_outcome(rows, index)
            if outcome is not None:
                outcome["signal_date"] = str(rows[index].get("timestamp") or rows[index].get("date"))
                eligible.append(outcome)
                if _metadata_pass(rows, index, float_shares.get(symbol)):
                    metadata_eligible.append(outcome)
            if _formula_signal(
                rows,
                index,
                float_shares.get(symbol),
                literal_average_price=literal_average_price,
                use_metadata_filters=use_metadata_filters,
                closes=closes,
                volumes=volumes,
                ema3=ema3,
                ema8=ema8,
            ) and outcome is not None:
                outcome["symbol"] = symbol
                trades.append(outcome)

    return {
        "variant": {
            "literal_average_price": literal_average_price,
            "metadata_filters": use_metadata_filters,
        },
        "strategy": _summarize(trades),
        "strategy_by_month": _monthly_summary(trades),
        "eligible_baseline": _summarize(eligible),
        "metadata_baseline": _summarize(metadata_eligible),
    }


def _float_shares(adapter: TickFlowAdapter, symbols: list[str]) -> dict[str, float | None]:
    master = adapter.get_security_master(symbols)
    result: dict[str, float | None] = {}
    for symbol, row in master.items():
        ext = row.get("ext") if isinstance(row, dict) else {}
        result[symbol] = finite(ext.get("float_shares")) if isinstance(ext, dict) else None
    return result


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _sample_symbols(symbols: list[str], max_symbols: int) -> list[str]:
    if max_symbols <= 0 or max_symbols >= len(symbols):
        return symbols
    # Spread the sample over the provider's universe order instead of taking
    # only the first block, which may overrepresent one listing segment.
    positions = [min(len(symbols) - 1, int(index * len(symbols) / max_symbols)) for index in range(max_symbols)]
    return list(dict.fromkeys(symbols[position] for position in positions))


def _fetch_bars(
    adapter: TickFlowAdapter,
    symbols: list[str],
    start: date,
    end: date,
) -> tuple[dict[str, list[dict]], int]:
    result: dict[str, list[dict]] = {}
    failed_chunks = 0
    for offset in range(0, len(symbols), 100):
        chunk = symbols[offset : offset + 100]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result.update(adapter.get_historical_bars_batch(chunk, start, end))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
        if last_error is not None:
            failed_chunks += 1
    return result, failed_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=START_DEFAULT.isoformat())
    parser.add_argument("--end", default=END_DEFAULT.isoformat())
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    adapter = TickFlowAdapter(auto_connect=True, rate_limit=-1)
    quote_payload = adapter.client.quotes.get(universes=["CN_Equity_A"])
    quote_rows = quote_payload if isinstance(quote_payload, list) else []
    symbols = [
        str(row["symbol"])
        for row in quote_rows
        if isinstance(row, dict) and row.get("symbol")
    ]
    symbols = _sample_symbols(symbols, args.max_symbols)

    bars_by_symbol, failed_chunks = _fetch_bars(adapter, symbols, start, end)
    bars_by_symbol = {symbol: rows for symbol, rows in bars_by_symbol.items() if rows}
    shares = _float_shares(adapter, list(bars_by_symbol))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "requested_symbols": len(symbols),
        "symbols_with_bars": len(bars_by_symbol),
        "notes": [
            "DYNAINFO(17) is replaced by full-day volume / prior-five-day mean.",
            "Historical CAPITAL/FINANCE(40) use current TickFlow float shares as a proxy.",
            "Signals are generated at close and entered at next-session open.",
            "The original AMOUNT/V version is reported separately from AMOUNT/(V*100).",
            f"Historical data chunks that failed after retry: {failed_chunks}.",
        ],
        "variants": [
            _backtest_variant(bars_by_symbol, shares, literal_average_price=True, use_metadata_filters=True),
            _backtest_variant(bars_by_symbol, shares, literal_average_price=False, use_metadata_filters=True),
            _backtest_variant(bars_by_symbol, shares, literal_average_price=False, use_metadata_filters=False),
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
