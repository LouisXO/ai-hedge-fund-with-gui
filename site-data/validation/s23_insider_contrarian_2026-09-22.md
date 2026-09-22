# S23 — insider_contrarian as a short-term event line (2026-09-22)

Hypothesis: v1 insider rule restricted by 'contrarian' keeps the informative purchases.

2017-01-01 → 2026-08-31, 5201 tradable events (ADV $3M–$100M), entry at the open after the event date, abnormal = minus SPY, clustered by event date.

| cut | h | events | dates | mean abn % | median | NW t | boot CI95 | hit | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|---|---|---|
| insider_contrarian all h1 | 1 | 5201 | 1638 | +0.28 | +0.10 | 3.03 | [+0.11, +0.47] | 51% | 90% | +0.37 | +0.20 |
| insider_contrarian mid h1 | 1 | 2376 | 1120 | +0.31 | +0.27 | 2.79 | [+0.09, +0.52] | 54% | 90% | +0.23 | +0.39 |
| insider_contrarian small h1 | 1 | 2825 | 1279 | +0.28 | +0.03 | 2.45 | [+0.06, +0.51] | 50% | 70% | +0.38 | +0.18 |
| insider_contrarian all h5 | 5 | 5200 | 1638 | +0.23 | +0.04 | 1.33 | [-0.15, +0.62] | 50% | 70% | +0.41 | +0.06 |
| insider_contrarian mid h5 | 5 | 2376 | 1120 | +0.29 | +0.33 | 1.33 | [-0.22, +0.78] | 53% | 70% | -0.02 | +0.59 |
| insider_contrarian small h5 | 5 | 2824 | 1279 | +0.33 | -0.27 | 1.49 | [-0.13, +0.78] | 47% | 80% | +0.71 | -0.06 |
| insider_contrarian all h10 | 10 | 5200 | 1638 | +0.37 | +0.17 | 1.11 | [-0.36, +1.10] | 51% | 70% | +0.27 | +0.46 |
| insider_contrarian mid h10 | 10 | 2376 | 1120 | +0.25 | +0.29 | 0.77 | [-0.50, +0.96] | 52% | 70% | -0.28 | +0.78 |
| insider_contrarian small h10 | 10 | 2824 | 1279 | +0.62 | -0.05 | 1.52 | [-0.29, +1.56] | 49% | 60% | +0.61 | +0.63 |
| insider_contrarian all h20 | 20 | 5199 | 1638 | +0.22 | -0.04 | 0.53 | [-0.83, +1.18] | 50% | 60% | +0.87 | -0.44 |
| insider_contrarian mid h20 | 20 | 2376 | 1120 | +0.21 | +0.11 | 0.44 | [-0.80, +1.21] | 51% | 50% | -0.13 | +0.54 |
| insider_contrarian small h20 | 20 | 2823 | 1279 | +0.41 | +0.07 | 0.79 | [-0.93, +1.73] | 50% | 50% | +1.35 | -0.53 |
| insider_contrarian all h40 | 40 | 5198 | 1638 | -0.89 | -1.65 | -1.32 | [-2.63, +0.80] | 46% | 40% | -0.05 | -1.73 |
| insider_contrarian mid h40 | 40 | 2375 | 1120 | -0.73 | -1.78 | -0.95 | [-2.58, +1.02] | 45% | 40% | -0.92 | -0.54 |
| insider_contrarian small h40 | 40 | 2823 | 1279 | -0.80 | -1.49 | -0.94 | [-2.76, +1.16] | 45% | 30% | +0.50 | -2.10 |
| placebo +60d h20 | 20 | 5139 | 1619 | -0.49 | -0.93 | -1.15 | [-1.56, +0.51] | 46% | 20% | -0.72 | -0.27 |

## As a book (equal slots, half spread per side, idle in SPY)

| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | avg trade % |
|---|---|---|---|---|---|---|---|---|---|---|
| insider_contrarian_h5 | +13.1% | +15.3% | -0.2% | -0.05 | 1.09 | 0.47 | -47% | 3437 | 51% | +0.23 |
| insider_contrarian_h10 | +18.0% | +15.3% | +8.5% | 0.94 | 1.09 | 0.80 | -59% | 2619 | 52% | +1.02 |
| insider_5d | +20.4% | +15.3% | +8.4% | 1.93 | 1.04 | 0.75 | -46% | 6589 | 51% | +0.45 |
