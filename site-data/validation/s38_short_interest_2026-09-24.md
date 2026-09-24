# S38 — short interest as a negative filter (2026-09-24)

FINRA consolidated short interest, 2020-06-01 → 2026-08-31, point-in-time = settlement + 9 business days. SI ratio = short shares / shares outstanding.

## Cross-section: abnormal return from the next open over 20 sessions

| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| hi_si20 flagged h20 | 20 | 8604 | 149 | -0.87 | -0.66 | 43% | -1.56 | -0.20 |
| hi_si20 others h20 | 20 | 353114 | 149 | -0.22 | -0.54 | 29% | +0.17 | -0.61 |
| hi_dtc10 flagged h20 | 20 | 28956 | 149 | -0.32 | -0.63 | 57% | -0.32 | -0.33 |
| hi_dtc10 others h20 | 20 | 332762 | 149 | -0.19 | -0.46 | 29% | +0.23 | -0.60 |
| hi_decile flagged h20 | 20 | 31493 | 149 | +0.03 | 0.03 | 43% | +0.37 | -0.31 |
| hi_decile others h20 | 20 | 330225 | 149 | -0.24 | -0.62 | 29% | +0.15 | -0.61 |

## Books (veto blocks entries only)

| book | CAGR | alpha2/yr | alpha2 t | trades |
|---|---|---|---|---|
| long_base | +34.2% | +16.3% | 2.15 | 894 |
| insider_base | +23.9% | +8.1% | 1.44 | 4241 |
| long_hi_si20 | +36.0% | +17.6% | 2.37 | 898 |
| insider_hi_si20 | +21.9% | +6.4% | 1.17 | 4175 |
| long_hi_dtc10 | +35.5% | +17.3% | 2.28 | 902 |
| insider_hi_dtc10 | +22.4% | +6.7% | 1.22 | 3857 |
| long_hi_decile | +32.2% | +14.7% | 2.05 | 930 |
| insider_hi_decile | +20.6% | +5.2% | 0.99 | 3953 |

## Vetoed vs kept target names

| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| long hi_si20 vetoed h20 | 20 | 1748 | 1059 | +3.77 | 3.05 | 57% | +0.63 | +6.90 |
| long hi_si20 kept h20 | 20 | 44825 | 1567 | +1.18 | 3.97 | 86% | +1.63 | +0.73 |
| insider hi_si20 vetoed h5 | 5 | 180 | 166 | +1.83 | 1.91 | 43% | -1.61 | +5.27 |
| insider hi_si20 kept h5 | 5 | 6790 | 1428 | +0.30 | 1.88 | 71% | +0.62 | -0.01 |
| long hi_dtc10 vetoed h20 | 20 | 1714 | 1029 | -0.03 | -0.04 | 43% | +0.08 | -0.13 |
| long hi_dtc10 kept h20 | 20 | 44859 | 1567 | +1.38 | 4.46 | 86% | +1.79 | +0.97 |
| insider hi_dtc10 vetoed h5 | 5 | 979 | 642 | +0.78 | 2.13 | 100% | +0.68 | +0.89 |
| insider hi_dtc10 kept h5 | 5 | 5991 | 1405 | +0.32 | 1.91 | 71% | +0.50 | +0.14 |
| long hi_decile vetoed h20 | 20 | 5582 | 1529 | +2.53 | 3.46 | 57% | +1.06 | +4.00 |
| long hi_decile kept h20 | 20 | 40991 | 1567 | +1.02 | 3.50 | 86% | +1.40 | +0.65 |
| insider hi_decile vetoed h5 | 5 | 699 | 524 | +0.90 | 2.01 | 86% | +0.28 | +1.52 |
| insider hi_decile kept h5 | 5 | 6271 | 1410 | +0.31 | 1.91 | 71% | +0.50 | +0.11 |

## Verdict

| variant | book improves | vetoed negative | adopt |
|---|---|---|---|
| long_hi_si20 | ✓ |  | no |
| insider_hi_si20 |  |  | no |
| long_hi_dtc10 | ✓ |  | no |
| insider_hi_dtc10 |  |  | no |
| long_hi_decile |  |  | no |
| insider_hi_decile |  |  | no |
