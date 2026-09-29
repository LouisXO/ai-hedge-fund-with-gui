# S45 D3 — long v1 base vs factor ETFs (2026-09-29)

Base: s47_base_nav.csv (S47 restatement), daily, 2017-01-01 → 2026-08-31. Factor ETFs in excess of SPY (small value and AVUV in excess of IWM). NW t, lag 5. Report only (S45).

| model | days | alpha/yr | t | R² | betas |
|---|---|---|---|---|---|
| alpha2 (SPY, IWM-SPY) | 2427 | +8.37% | 1.40 | 0.58 | SPY +1.09, IWM-SPY +0.77 |
| + QQQ-SPY | 2427 | +6.90% | 1.17 | 0.61 | SPY +0.97, IWM-SPY +0.88, QQQ-SPY +0.64 |
| + momentum, value, quality, low vol | 2427 | +7.18% | 1.50 | 0.74 | SPY +0.93, IWM-SPY +0.79, MTUM +1.08, VLUE +0.02, QUAL -0.58, USMV -0.27 |
| + small value, small growth-value | 2427 | +7.11% | 1.49 | 0.74 | SPY +0.94, IWM-SPY +0.78, MTUM +1.05, VLUE +0.04, QUAL -0.61, USMV -0.25, IJS-IWM +0.06, IWO-IWN +0.09 |
| AVUV window: alpha2 | 1738 | +12.98% | 1.62 | 0.57 | SPY +1.13, IWM-SPY +0.80 |
| AVUV window: + all above + AVUV-IWM | 1738 | +10.98% | 1.81 | 0.77 | SPY +0.97, IWM-SPY +0.83, MTUM +1.06, VLUE -0.01, QUAL -0.77, USMV -0.15, IJS-IWM -0.02, IWO-IWN +0.50, AVUV-IWM +0.60 |
