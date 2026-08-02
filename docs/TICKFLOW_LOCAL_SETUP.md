# TickFlow Local Setup

TickFlow supplies two things this system depends on: **daily bars in batch**
and the **申万 industry taxonomy**. Both are on the free tier and need no key.

## Install

```bash
cd /root/a-share-ultra-short-ai-system
python -m pip install -e ".[tickflow]"

# Free tier needs no key. A paid key unlocks the intraday path:
export TICKFLOW_API_KEY=your_key_here
```

## Check

```bash
ashare-ds tick-health
```

## Measured capability matrix

Verified against the live service, not copied from marketing material.

| Capability | Free (no key) | Full (API key) |
| --- | --- | --- |
| daily bars, batched (`klines.batch`) | yes | yes |
| 申万 1/2/3 级行业 + 成分股 (`universes`) | **yes** | yes |
| security master (`instruments`) | yes | yes |
| realtime quotes (`quotes`, incl. by universe) | no | yes |
| minute bars (`klines.intraday`) | no | yes |
| order book depth (`depth`) | no | yes |
| websocket push (`stream`) | no | yes |
| tick-by-tick order detail | **no** | **no** |
| auction matched / unmatched quantity | **no** | **no** |

The last two rows are the important ones: **no TickFlow tier can support 09:25
auction interception.** Combined with the fact that the exchange does not accept
cancellations between 09:20 and 09:25, that path is closed regardless of
spending. See `docs/PLAN_REVIEW.md`.

Measured throughput on the free tier: `klines.batch` returns 59 symbols in
~0.4s, so the full 4400-constituent sector scan takes 30–60s.

## 申万 taxonomy — the non-obvious part

`universes.list()` returns 1013 entries. Level 1 arrives as **335 shards that
collapse to 31 distinct sector names**, so they have to be merged by name before
use; `universes.get(id)` then returns the constituent symbols. Coverage is
4430 of 5528 A-shares.

This is what makes the whole sector chain workable without a subscription, and
it is not prominent in the SDK docs. Handled by
`sectors/taxonomy.py:TickFlowSwTaxonomy`, cached to
`<output-dir>/sector_taxonomy_sw1.json`.

## Price adjustment boundary

The SDK defaults to **forward adjustment** (前复权). The adapter passes it
explicitly (`TickFlowAdapter(adjust=...)`), accepting `none`, `forward`,
`backward`, `forward_additive`, `backward_additive`.

- **forward** (default): latest bar carries real prices, history is rescaled by
  the dividend factor. Correct for moving averages, support/resistance, and
  Wyckoff structure. History shows values like `24.100876692935397`.
- **none**: raw traded prices throughout, two decimals.

Anything that depends on the actual traded price grid — order prices, limit-up
detection, sealing ratios, tick-size alignment — must use raw prices. The
system gets those from the Sina minute feed instead, and converts daily levels
into raw space before quoting them. See `docs/SOLUTION.md` §2.

Volume is in **lots (手)**, amount in **yuan**. The adapter reconciles the two
before deriving VWAP; if the unit cannot be established it declines to compute
VWAP rather than emitting a value off by 100x.

## Notes

- Without an API key the adapter runs in free mode and reports it in
  `health_check()`; the jobs read that and degrade rather than guess.
- Prefer full symbols like `603983.SH`; bare 6-digit codes are auto-normalized.
- `TICKFLOW_BASE_URL` overrides the endpoint if you need to.
