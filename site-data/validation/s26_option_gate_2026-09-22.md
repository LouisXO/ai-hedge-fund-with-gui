# S26 — option cheapness gate on real prices (2026-09-22)

2024-02-01 → 2026-08-31, 179874 underlying-days with an ATM call+put pair (25–50 DTE, monthly, traded that day), S&P names. Entry at the contract's close, exit at its close 5/10 sessions later, gross of spread. Median ATM IV 29.2% vs median RV20 28.5%. Gate passes 26% of days.

## Gate pass − fail, mean buyer return %, clustered by entry date

| instrument | h | dates | pass | fail | diff | NW t | hit(pass>0) | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| straddle | 5 | 494 | -0.15 | -0.46 | +0.31 | 1.06 | 43% | +0.73 | -0.05 | +0.22 |
| straddle | 10 | 494 | -2.19 | -1.72 | -0.47 | -0.73 | 31% | +1.12 | -1.44 | -1.38 |
| call | 5 | 494 | +3.02 | +3.32 | -0.30 | -0.21 | 52% | +0.69 | -2.02 | +0.90 |
| call | 10 | 494 | +2.93 | +4.32 | -1.39 | -0.59 | 54% | +1.42 | -4.95 | -0.12 |
| put | 5 | 494 | -3.96 | -6.01 | +2.04 | 1.67 | 35% | +2.32 | +3.63 | -0.88 |
| put | 10 | 494 | -10.15 | -11.22 | +1.07 | 0.56 | 27% | +1.34 | +2.98 | -2.35 |

## IV / RV20 deciles (1 = cheapest), mean buyer return % — diagnostic

        ret_S_5  ret_S_10  ret_C_10  ret_P_10
decile                                       
1          1.41     -0.12      3.51     -7.63
2          0.61     -0.98      3.99    -11.30
3          0.44     -0.82      5.66    -12.03
4         -0.00     -1.75      4.61    -12.99
5         -0.42     -2.20      3.54    -12.86
6         -0.71     -2.33      3.69    -13.35
7         -1.08     -2.20      3.36    -12.55
8         -1.50     -3.06      1.29    -12.82
9         -1.52     -3.02      0.40    -10.74
10        -2.06     -4.09     -1.81     -9.72

## By year, all days

      ret_S_5  ret_S_10
date                   
2024    -0.45     -1.55
2025    -0.43     -3.01
2026    -0.64     -1.31
