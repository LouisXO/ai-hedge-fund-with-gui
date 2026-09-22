# S23 — insider_opp_ceo as a short-term event line (2026-09-22)

Hypothesis: v1 insider rule restricted by 'opp_ceo' keeps the informative purchases.

2017-01-01 → 2026-08-31, 3303 tradable events (ADV $3M–$100M), entry at the open after the event date, abnormal = minus SPY, clustered by event date.

| cut | h | events | dates | mean abn % | median | NW t | boot CI95 | hit | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|---|---|---|
| insider_opp_ceo all h1 | 1 | 3303 | 1492 | +0.39 | +0.06 | 4.33 | [+0.22, +0.56] | 51% | 90% | +0.38 | +0.39 |
| insider_opp_ceo mid h1 | 1 | 1390 | 884 | +0.29 | +0.13 | 2.59 | [+0.07, +0.52] | 52% | 100% | +0.25 | +0.33 |
| insider_opp_ceo small h1 | 1 | 1913 | 1139 | +0.57 | +0.08 | 4.46 | [+0.33, +0.83] | 51% | 90% | +0.55 | +0.60 |
| insider_opp_ceo all h5 | 5 | 3299 | 1491 | +0.25 | +0.11 | 1.36 | [-0.18, +0.66] | 51% | 80% | +0.40 | +0.10 |
| insider_opp_ceo mid h5 | 5 | 1390 | 884 | +0.12 | +0.11 | 0.52 | [-0.46, +0.69] | 52% | 70% | -0.01 | +0.26 |
| insider_opp_ceo small h5 | 5 | 1909 | 1136 | +0.39 | -0.02 | 1.63 | [-0.14, +0.91] | 50% | 90% | +0.61 | +0.18 |
| insider_opp_ceo all h10 | 10 | 3295 | 1489 | +0.39 | +0.02 | 1.32 | [-0.28, +1.09] | 50% | 70% | +0.39 | +0.39 |
| insider_opp_ceo mid h10 | 10 | 1390 | 884 | +0.35 | +0.05 | 0.81 | [-0.53, +1.25] | 50% | 60% | -0.17 | +0.87 |
| insider_opp_ceo small h10 | 10 | 1905 | 1133 | +0.41 | -0.07 | 1.19 | [-0.39, +1.17] | 50% | 70% | +0.63 | +0.19 |
| insider_opp_ceo all h20 | 20 | 3293 | 1488 | +0.09 | -0.15 | 0.23 | [-0.84, +0.99] | 49% | 60% | +0.34 | -0.16 |
| insider_opp_ceo mid h20 | 20 | 1390 | 884 | +0.57 | +0.54 | 1.09 | [-0.49, +1.58] | 52% | 60% | +0.42 | +0.71 |
| insider_opp_ceo small h20 | 20 | 1903 | 1132 | -0.08 | -0.38 | -0.15 | [-1.27, +1.11] | 48% | 60% | +0.38 | -0.54 |
| insider_opp_ceo all h40 | 40 | 3291 | 1487 | -0.52 | -1.51 | -0.84 | [-1.92, +0.87] | 44% | 40% | -0.44 | -0.60 |
| insider_opp_ceo mid h40 | 40 | 1390 | 884 | -0.39 | -1.32 | -0.53 | [-1.69, +0.98] | 45% | 40% | -0.58 | -0.21 |
| insider_opp_ceo small h40 | 40 | 1901 | 1131 | -0.50 | -1.45 | -0.61 | [-2.13, +1.09] | 44% | 40% | -0.43 | -0.57 |
| placebo +60d h20 | 20 | 3258 | 1468 | -0.63 | -0.83 | -1.64 | [-1.57, +0.30] | 45% | 20% | -1.17 | -0.10 |

## As a book (equal slots, half spread per side, idle in SPY)

| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | avg trade % |
|---|---|---|---|---|---|---|---|---|---|---|
| insider_opp_ceo_h5 | +16.5% | +15.3% | +2.5% | 0.75 | 1.06 | 0.36 | -52% | 2638 | 51% | +0.37 |
| insider_opp_ceo_h10 | +16.5% | +15.3% | +3.7% | 0.95 | 1.06 | 0.57 | -55% | 2261 | 53% | +0.78 |
| insider_5d | +20.4% | +15.3% | +8.4% | 1.93 | 1.04 | 0.75 | -46% | 6589 | 51% | +0.45 |
