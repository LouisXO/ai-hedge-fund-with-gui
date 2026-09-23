# S33 — negative signals as entry vetoes (2026-09-23)

2017-01-01 → 2026-08-31, half spread per side, next-open entry, idle in SPY. A veto blocks entries only; held names are unaffected. Bearish-flow data exists from 2024-02 (S&P names), so bear5 is a no-op before that.

## Books

| book | CAGR | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | vetoed pairs |
|---|---|---|---|---|---|---|---|---|---|
| long_base | +19.4% | +8.1% | 1.46 | 1.05 | 0.73 | -53% | 1431 | 52% |  |
| insider_base | +20.4% | +8.4% | 1.93 | 1.04 | 0.75 | -46% | 6589 | 51% |  |
| long_jump1 | +19.2% | +8.0% | 1.43 | 1.05 | 0.74 | -53% | 1420 | 51% | 1458 |
| insider_jump1 | +16.7% | +4.9% | 1.15 | 1.06 | 0.75 | -46% | 6540 | 51% | 280 |
| long_jump5 | +20.5% | +9.1% | 1.63 | 1.05 | 0.74 | -53% | 1432 | 52% | 6848 |
| insider_jump5 | +20.6% | +7.5% | 1.87 | 1.08 | 0.67 | -44% | 5971 | 51% | 2031 |
| long_bear5 | +19.3% | +8.0% | 1.45 | 1.05 | 0.73 | -53% | 1431 | 52% | 183 |
| insider_bear5 | +20.4% | +8.4% | 1.93 | 1.04 | 0.75 | -46% | 6588 | 51% | 1 |
| long_both5 | +20.4% | +9.0% | 1.62 | 1.05 | 0.74 | -53% | 1432 | 52% | 6990 |
| insider_both5 | +20.6% | +7.5% | 1.87 | 1.08 | 0.67 | -44% | 5971 | 51% | 2031 |

## Direct test — vetoed vs kept target names, abnormal return from the next open

| cut | h | events | dates | mean abn % | NW t | hit | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|---|
| long jump1 vetoed h20 | 20 | 733 | 551 | -0.73 | -1.04 | 44% | 40% | -1.29 | -0.18 |
| long jump1 kept h20 | 20 | 71428 | 2423 | +0.44 | 1.97 | 52% | 70% | -0.15 | +1.02 |
| insider jump1 vetoed h5 | 5 | 280 | 170 | +2.40 | 1.71 | 55% | 80% | +1.94 | +2.85 |
| insider jump1 kept h5 | 5 | 11027 | 2228 | +0.24 | 2.01 | 51% | 80% | +0.37 | +0.11 |
| long jump5 vetoed h20 | 20 | 3456 | 1625 | -0.66 | -1.36 | 45% | 20% | -1.38 | +0.07 |
| long jump5 kept h20 | 20 | 68705 | 2423 | +0.46 | 2.04 | 52% | 70% | -0.13 | +1.05 |
| insider jump5 vetoed h5 | 5 | 2031 | 1010 | +0.28 | 0.85 | 49% | 40% | +0.29 | +0.28 |
| insider jump5 kept h5 | 5 | 9276 | 2190 | +0.34 | 2.76 | 52% | 90% | +0.46 | +0.23 |
| long bear5 vetoed h20 | 20 | 93 | 86 | +2.69 | 0.69 | 47% | 67% | +3.99 | +1.40 |
| long bear5 kept h20 | 20 | 72068 | 2423 | +0.42 | 1.93 | 51% | 70% | -0.16 | +1.01 |
| insider bear5 vetoed h5 | — | — | 1 | too few | | | | | |
| insider bear5 kept h5 | 5 | 11306 | 2231 | +0.27 | 2.25 | 51% | 80% | +0.39 | +0.15 |
| long both5 vetoed h20 | 20 | 3528 | 1647 | -0.63 | -1.31 | 45% | 20% | -1.21 | -0.05 |
| long both5 kept h20 | 20 | 68633 | 2423 | +0.45 | 2.03 | 52% | 70% | -0.13 | +1.04 |
| insider both5 vetoed h5 | 5 | 2031 | 1010 | +0.28 | 0.85 | 49% | 40% | +0.29 | +0.28 |
| insider both5 kept h5 | 5 | 9276 | 2190 | +0.34 | 2.76 | 52% | 90% | +0.46 | +0.23 |

## Verdict (pre-registered rule: adopt only if the book improves >= 0.5%/yr with t not lower AND vetoed names have NW t <= -2)

| variant | book improves | vetoed negative | adopt |
|---|---|---|---|
| long_jump1 |  |  | no |
| long_jump5 | ✓ |  | no |
| long_bear5 |  |  | no |
| long_both5 | ✓ |  | no |
| insider_jump1 |  |  | no |
| insider_jump5 |  |  | no |
| insider_bear5 |  |  | no |
| insider_both5 |  |  | no |
