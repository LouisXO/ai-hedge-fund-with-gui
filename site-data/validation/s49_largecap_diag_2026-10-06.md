# S49 — why selection adds nothing among large caps: diagnostics (2026-10-06)

2017-01-01 → 2026-08-31; corrected universes (S49 补充): large >= $10B (median 582 names at month ends), small = $100M–$10B, one ticker per CIK, no 20-F/40-F filers, split-safe market caps. Families z-scored within each universe.

## D4 information coefficient (Spearman, next close → +21 sessions, month ends)

| family | large mean | large t | small mean | small t |
|---|---|---|---|---|
| value | +0.0022 | 0.11 | +0.0274 | 1.66 |
| quality | +0.0159 | 2.19 | +0.0230 | 2.35 |
| momentum | +0.0022 | 0.14 | +0.0136 | 1.05 |
| lowvol | +0.0017 | 0.08 | +0.0404 | 1.99 |
| composite | +0.0122 | 0.64 | +0.0381 | 2.09 |

## D5 family correlations (mean Spearman)

```
{
 "large": {
  "value": {
   "value": 1.0,
   "quality": -0.279,
   "momentum": -0.222,
   "lowvol": 0.222
  },
  "quality": {
   "value": -0.279,
   "quality": 1.0,
   "momentum": 0.034,
   "lowvol": -0.018
  },
  "momentum": {
   "value": -0.222,
   "quality": 0.034,
   "momentum": 1.0,
   "lowvol": -0.015
  },
  "lowvol": {
   "value": 0.222,
   "quality": -0.018,
   "momentum": -0.015,
   "lowvol": 1.0
  }
 },
 "small": {
  "value": {
   "value": 1.0,
   "quality": 0.042,
   "momentum": -0.142,
   "lowvol": 0.391
  },
  "quality": {
   "value": 0.042,
   "quality": 1.0,
   "momentum": 0.008,
   "lowvol": 0.151
  },
  "momentum": {
   "value": -0.142,
   "quality": 0.008,
   "momentum": 1.0,
   "lowvol": 0.077
  },
  "lowvol": {
   "value": 0.391,
   "quality": 0.151,
   "momentum": 0.077,
   "lowvol": 1.0
  }
 }
}
```

## D3 weighting (corrected large-cap universe, no costs)

CAGR: equal +12.64%, cap +15.51%, SPY +15.32%; cap-weighted vs SPY tracking 1.65%/yr

| year | equal | cap | SPY |
|---|---|---|---|
| 2017 | +17.9% | +19.8% | +19.6% |
| 2018 | -6.5% | -3.8% | -4.6% |
| 2019 | +31.4% | +31.6% | +31.2% |
| 2020 | +21.7% | +23.7% | +18.3% |
| 2021 | +22.6% | +25.1% | +28.7% |
| 2022 | -16.4% | -19.8% | -18.2% |
| 2023 | +17.6% | +27.1% | +26.2% |
| 2024 | +15.7% | +25.2% | +24.9% |
| 2025 | +13.0% | +17.9% | +17.7% |
| 2026 | +12.8% | +12.6% | +13.1% |

## D6 coverage (large caps with fewer than 3 families)

```
{
 "share_excluded_mean": 0.09776224912920493,
 "share_by_year": {
  "2017": 0.081,
  "2018": 0.086,
  "2019": 0.095,
  "2020": 0.095,
  "2021": 0.111,
  "2022": 0.11,
  "2023": 0.114,
  "2024": 0.102,
  "2025": 0.091,
  "2026": 0.09
 },
 "reasons_mean": {
  "no_fundamentals": 0.006675443234741661,
  "low_equity": 0.07844481117441872,
  "other": 0.012641994720044548
 },
 "ret12m_excluded_mean": 0.15739225701102175,
 "ret12m_included_mean": 0.12742628629234526,
 "ret12m_gap_t_nw": 1.6030480833568705,
 "last_day": "2026-08-31"
}
```

Excluded on 2026-08-31: SPCX, DELL, CBRS, CRCL, MCD, ABBV, BA, BKNG, MCK, PM, HONA, AZO, SBUX, LOW, MO, HLT, MAR, TDG, RBLX, HCA, EXPE, COR, APO, HPQ, CAH, ORLY, IBKR, RBRK, CL, YUM, MSCI, W, GH, LYV, FICO, CLX, MET, DPZ, IT, MTD

## D2 power (S37 as run: s37_largecap_2026-09-24.json)

| book | alpha2 | t | SE | alpha for t=2 | years for 2%/yr at t=2 |
|---|---|---|---|---|---|
| lc_composite | +0.8% | 0.23 | 3.5% | 6.9% | 116 |
| lc_rankz | +0.1% | 0.03 | 3.1% | 6.2% | 93 |
| lc_qlv | +2.1% | 0.81 | 2.5% | 5.0% | 61 |
| lc_qv | +2.8% | 0.73 | 3.9% | 7.8% | 149 |
| lc_mom | +6.4% | 0.87 | 7.3% | 14.6% | 515 |
| lc_value | +1.7% | 0.41 | 4.0% | 8.0% | 156 |
| lc_payout | +0.0% | 0.00 | 3.5% | 7.0% | 118 |
| lc_secneutral | +0.9% | 0.34 | 2.5% | 5.1% | 63 |
| lc_composite50 | -0.9% | -0.30 | 3.0% | 6.0% | 88 |
| lc_ew | -0.9% | -0.82 | 1.1% | 2.1% | 11 |
