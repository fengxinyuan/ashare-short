# QMT And MiniQMT Field Mapping

This document defines the initial normalization profiles used by the project.
They are not treated as immutable broker truth. They are the system-side mapping
contract that keeps engine inputs stable.

## Auction-stage target fields

- `symbol`
- `matched_qty`
- `matched_amount`
- `unmatched_qty`
- `unmatched_side`
- `indicative_match_price`
- `open_gap_pct`
- `cancel_pre920_ratio`
- `seal_ratio`
- `tail_flow_distortion`
- `data_stale`
- `source_ts`

## Open-follow target fields

- `symbol`
- `minutes_since_open`
- `price_vs_open_pct`
- `price_vs_vwap_pct`
- `volume_ratio`
- `open_gap_pct`
- `data_stale`
- `source_ts`

## Alias strategy

Each normalized field accepts a list of aliases. The normalizer tries them in
order and uses the first one present in the raw payload.

Examples:

- `matched_qty`: `MatchedQty`, `matchedQty`, `matched_qty`
- `indicative_match_price`: `IndicativeMatchPrice`, `indicativeMatchPrice`, `BidPrice1`
- `source_ts`: `source_ts`, `sourceTs`, `timestamp`, `SourceTs`

## Derived fields

- `matched_amount`
  - use raw `MatchedAmount` if present
  - otherwise compute `matched_qty * indicative_match_price`
- `open_gap_pct`
  - use raw field if present
  - otherwise derive from `prev_close` and `indicative_match_price` or current price

## Optional fields

- `cancel_pre920_ratio`
  - only valid when the upstream data source can reconstruct `09:15-09:19:59`
    order-detail or cancel data
- `seal_ratio`
  - optional
- `tail_flow_distortion`
  - optional

## Safety rules

- Missing required fields raise a normalization error.
- Optional fields default to `None` or a safe fallback.
- Strategy engines never read raw broker field names directly.
