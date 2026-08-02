# Research Pipeline

This stage generates the daily `DecisionMatrix` before the auction logic runs.

## Inputs

- `market_context`
  - `limit_down_count`
  - `blowup_rate`
  - `breadth_score`
  - `style_decay_factor`
- `sectors`
  - route
  - policy score and weight
  - anchor score
  - flow score
  - fake-tail penalty
  - decay factor
- `candidates`
  - symbol
  - sector
  - thesis
  - optional route override
  - optional stop, RRR, and position band overrides
- `blacklist`
- optional structure overlay
  - Wyckoff bias
  - spring / upthrust
  - SOS / LPS
  - effort-vs-result

## Core logic

1. Compute `position_band_factor` from market context.
2. Score sectors and keep only the strongest names by route.
3. Drop blacklisted or low-liquidity candidates.
4. Apply route defaults for stop loss and minimum RRR.
5. Scale default position bands by `position_band_factor`.
6. Optionally use Wyckoff structure as a cross-check before final ranking.

## Outputs

- `market_regime`
- `position_band_factor`
- `sector_whitelist`
- `sector_hedge`
- filtered `candidates`

## Default route rules

- `MAINLINE`
  - `stop_bps = 350`
  - `rrr_min = 2.0`
  - base position band `10%-15%`
- `PULSE`
  - `stop_bps = 200`
  - `rrr_min = 3.0`
  - base position band `5%-10%`
- `HEDGE`
  - `stop_bps = 350`
  - `rrr_min = 2.0`
  - base position band `5%-8%`
