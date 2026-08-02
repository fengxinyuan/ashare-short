# Wyckoff Integration

This project borrows Wyckoff concepts as a lightweight structure overlay, not
as a full discretionary trading system.

## Kept

- accumulation vs distribution bias
- spring and upthrust detection
- SOS and LPS style confirmation
- effort vs result checks
- compression before expansion

## Discarded

- heavyweight agent orchestration
- strategy sprawl
- opaque prompts as the main decision layer

## In this project

- `WyckoffAnalyzer` scores historical bars into a structure bias
- `DailyTechnicalAnalyzer` extracts support, resistance, trend state, and setup hints
- the result is research-only and does not place trades
- `wyckoff-scan` can run on `AkShare`, `TickFlow`, or local `QMT`

## Practical use

- use it to filter candidates before auction/open analysis
- use it to downgrade weak structures in pulse-style names
- use it as a cross-check, not as a single source of truth

## Typical interpretations

- `ACCUMULATION`: compression with controlled downside and recovery attempts
- `MARKUP`: breakout/expansion after accumulation
- `DISTRIBUTION`: failed upside extension with supply showing up
- `MARKDOWN`: breakdown and persistent weakness
