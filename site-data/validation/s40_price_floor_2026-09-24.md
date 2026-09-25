# S40 — minimum share price (2026-09-24)

2017-01-01 → 2026-08-31. Price = raw close on the signal day. Vetoes block entries only.

## Insider line by price bucket (h5, abnormal vs SPY from the next open)

| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| insider < $1 h5 | 5 | 70 | 64 | -1.64 | -0.59 | 50% | +2.78 | -6.05 |
| insider $1–2 h5 | 5 | 262 | 240 | +3.13 | 2.98 | 80% | +4.81 | +1.46 |
| insider $2–5 h5 | 5 | 853 | 654 | +0.84 | 1.52 | 70% | +1.09 | +0.60 |
| insider >= $5 h5 | 5 | 10122 | 2197 | +0.24 | 2.06 | 90% | +0.29 | +0.19 |

## Books

| book | CAGR | alpha2/yr | alpha2 t | trades | vetoed pairs |
|---|---|---|---|---|---|
| long_base | +19.4% | +8.1% | 1.46 | 1431 |  |
| insider_base | +20.4% | +8.4% | 1.93 | 6589 |  |
| long_floor1 | +19.6% | +8.3% | 1.49 | 1432 | 25 |
| insider_floor1 | +21.5% | +9.3% | 2.18 | 6564 | 70 |
| long_floor2 | +20.3% | +8.9% | 1.57 | 1435 | 471 |
| insider_floor2 | +17.4% | +5.8% | 1.35 | 6458 | 332 |
| long_floor5 | +20.0% | +8.7% | 1.58 | 1417 | 3432 |
| insider_floor5 | +14.0% | +2.3% | 0.60 | 6153 | 1187 |

## Vetoed vs kept

| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| long floor1 vetoed h20 | — | — | 21 | too few | | | | |
| long floor1 kept h20 | 20 | 72200 | 2425 | +0.43 | 1.94 | 70% | -0.16 | +1.01 |
| insider floor1 vetoed h5 | 5 | 70 | 64 | -1.64 | -0.59 | 50% | +2.78 | -6.05 |
| insider floor1 kept h5 | 5 | 11237 | 2227 | +0.31 | 2.64 | 90% | +0.38 | +0.25 |
| long floor2 vetoed h20 | 20 | 286 | 284 | -4.60 | -2.36 | 43% | -7.34 | -1.86 |
| long floor2 kept h20 | 20 | 71935 | 2425 | +0.45 | 2.04 | 70% | -0.14 | +1.04 |
| insider floor2 vetoed h5 | 5 | 332 | 290 | +2.37 | 2.23 | 80% | +4.53 | +0.21 |
| insider floor2 kept h5 | 5 | 10975 | 2222 | +0.27 | 2.25 | 90% | +0.32 | +0.21 |
| long floor5 vetoed h20 | 20 | 1965 | 1357 | -1.29 | -1.61 | 40% | -3.29 | +0.70 |
| long floor5 kept h20 | 20 | 70256 | 2425 | +0.46 | 2.08 | 70% | -0.10 | +1.02 |
| insider floor5 vetoed h5 | 5 | 1185 | 851 | +1.28 | 2.72 | 90% | +1.53 | +1.03 |
| insider floor5 kept h5 | 5 | 10122 | 2197 | +0.24 | 2.06 | 90% | +0.29 | +0.19 |

## Verdict

| variant | book improves | vetoed negative | adopt |
|---|---|---|---|
| long_floor1 |  |  | no |
| insider_floor1 | ✓ |  | no |
| long_floor2 | ✓ | ✓ | **ADOPT** |
| insider_floor2 |  |  | no |
| long_floor5 | ✓ |  | no |
| insider_floor5 |  |  | no |
