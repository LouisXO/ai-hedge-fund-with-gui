# S23 — insider_officer as a short-term event line (2026-09-22)

Hypothesis: v1 insider rule restricted by 'officer' keeps the informative purchases.

2017-01-01 → 2026-08-31, 4539 tradable events (ADV $3M–$100M), entry at the open after the event date, abnormal = minus SPY, clustered by event date.

| cut | h | events | dates | mean abn % | median | NW t | boot CI95 | hit | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|---|---|---|
| insider_officer all h1 | 1 | 4539 | 1694 | +0.36 | +0.06 | 4.52 | [+0.21, +0.51] | 51% | 80% | +0.37 | +0.34 |
| insider_officer mid h1 | 1 | 1850 | 1059 | +0.33 | +0.13 | 3.37 | [+0.14, +0.52] | 51% | 90% | +0.24 | +0.41 |
| insider_officer small h1 | 1 | 2689 | 1369 | +0.49 | +0.08 | 4.56 | [+0.27, +0.70] | 52% | 80% | +0.42 | +0.55 |
| insider_officer all h5 | 5 | 4535 | 1693 | +0.32 | +0.10 | 1.90 | [-0.07, +0.69] | 51% | 90% | +0.50 | +0.13 |
| insider_officer mid h5 | 5 | 1850 | 1059 | +0.24 | +0.20 | 1.12 | [-0.29, +0.74] | 52% | 70% | +0.07 | +0.41 |
| insider_officer small h5 | 5 | 2685 | 1368 | +0.48 | -0.02 | 2.22 | [+0.01, +0.95] | 50% | 90% | +0.65 | +0.31 |
| insider_officer all h10 | 10 | 4530 | 1690 | +0.30 | +0.20 | 1.23 | [-0.31, +0.90] | 52% | 70% | +0.44 | +0.17 |
| insider_officer mid h10 | 10 | 1849 | 1058 | +0.30 | +0.24 | 0.88 | [-0.52, +1.10] | 52% | 50% | -0.18 | +0.79 |
| insider_officer small h10 | 10 | 2681 | 1366 | +0.36 | -0.03 | 1.19 | [-0.43, +1.03] | 50% | 70% | +0.57 | +0.15 |
| insider_officer all h20 | 20 | 4528 | 1689 | +0.06 | -0.29 | 0.16 | [-0.86, +0.95] | 48% | 70% | +0.41 | -0.30 |
| insider_officer mid h20 | 20 | 1849 | 1058 | +0.41 | +0.23 | 0.84 | [-0.69, +1.44] | 51% | 50% | +0.03 | +0.78 |
| insider_officer small h20 | 20 | 2679 | 1365 | -0.02 | -0.37 | -0.05 | [-1.19, +1.06] | 48% | 70% | +0.56 | -0.60 |
| insider_officer all h40 | 40 | 4526 | 1689 | -0.47 | -1.41 | -0.83 | [-1.71, +0.87] | 45% | 40% | -0.41 | -0.54 |
| insider_officer mid h40 | 40 | 1849 | 1058 | -0.34 | -1.39 | -0.48 | [-1.93, +1.16] | 45% | 40% | -1.04 | +0.36 |
| insider_officer small h40 | 40 | 2677 | 1365 | -0.45 | -1.34 | -0.63 | [-2.00, +1.04] | 45% | 40% | -0.30 | -0.60 |
| placebo +60d h20 | 20 | 4473 | 1669 | -0.46 | -0.80 | -1.23 | [-1.40, +0.47] | 45% | 50% | -0.93 | +0.01 |

## As a book (equal slots, half spread per side, idle in SPY)

| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | avg trade % |
|---|---|---|---|---|---|---|---|---|---|---|
| insider_officer_h5 | +20.0% | +15.3% | +5.7% | 1.68 | 1.06 | 0.42 | -43% | 3386 | 51% | +0.53 |
| insider_officer_h10 | +18.1% | +15.3% | +5.4% | 1.37 | 1.06 | 0.63 | -56% | 2710 | 53% | +0.90 |
| insider_5d | +20.4% | +15.3% | +8.4% | 1.93 | 1.04 | 0.75 | -46% | 6589 | 51% | +0.45 |
