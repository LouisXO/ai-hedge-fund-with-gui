# S53 — composite_long_fpi backtest (2026-10-14)

2017-01-01 → 2026-08-31. v1 rules on a universe widened to foreign issuers (fpi.db). Same data snapshot for both lines. Survivors only for the new issuers (S53 补充 2): passing is necessary, not sufficient.

| | CAGR | alpha2/yr | t | excess vs SPY | MaxDD | Sharpe | trades | turnover/yr |
|---|---|---|---|---|---|---|---|---|
| v1 (S53a data) | +18.1% | +6.81% | 1.15 | +2.8% | -53% | 0.70 | 1541 | 5.00 |
| composite_long_fpi | +15.1% | +4.26% | 0.72 | -0.2% | -58% | 0.62 | 1543 | 4.96 |

Checks (pre-registered):
- (a) alpha2 not lower than v1 by more than 1%/yr: **不过**
- (b) turnover <= 1.15 x v1: 过
- (c) max drawdown not deeper by more than 3 points: **不过**
- (d) family z-scores of foreign vs US names: 过 — value mean diff +0.24 sd ratio 1.25, quality mean diff -0.06 sd ratio 0.80, momentum mean diff +0.08 sd ratio 1.10, lowvol mean diff -0.11 sd ratio 1.08
- (d) foreign share of the top 30 22.6% vs of the scored universe 10.1% (<= 2x): **不过**

**All four: fail.**

Counterfactuals (same scored lists, names removed, same engine): without new issuers alpha2 +6.15% (t 1.00), MaxDD -56%; without foreign alpha2 +6.04% (t 1.01), MaxDD -55%.
Raw trade returns (not relative to SPY, and not net of what the slot would have earned otherwise; sum / 30 slots, percentage points): foreign issuers +35.0, of which the 288 new issuers +5.9, all trades +142.5. New issuers in the top 30 per month end: 1.91.

By year (total return)

| | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| v1 | +7% | -9% | +6% | +25% | +19% | +6% | +38% | +24% | +22% | +46% |
| fpi | +10% | -10% | +3% | +14% | +11% | +6% | +27% | +21% | +27% | +46% |
