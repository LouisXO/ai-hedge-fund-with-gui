"""S49 — why does selection add nothing among large caps? Diagnostics, pre-registered 2026-10-06 before any run.

Not candidate rules: nothing here can change a running book or be adopted. What looks useful goes to the
hypothesis queue with its own pre-registration and data after 2026-10 (docs/AGENT_PLAN.md, S49).
The user asked (2026-10-06) why S37 found no large-cap edge; S37 ran on 2026-09-24, before the S47/S47b
data corrections, whose share-count errors concentrate in large caps (multi-class, 20-F).

  D1  S37 re-run on the corrected data: `python -m agent.s37_largecap`, unchanged (run separately).
  D2  power: each D1 book's alpha2 standard error (alpha2 / t) and the alpha that t = 2 needs (2 x SE).
  D3  weighting: daily equal- vs cap-weighted return of the large-cap universe against SPY, by year
      (members and share counts as of the previous month end, weights at the previous close, no costs).
  D4  information coefficient: on the last trading day of each month, Spearman correlation between each
      family score (z-scored within the universe; n_families >= 3, as the books require) and the return
      from the next close to 21 sessions later. Large caps (>= $10B) and the rest of v1's universe
      ($100M–$10B), each scored within itself. Mean, Newey-West t (lag 3), mean by year.
  D5  family correlations: the same dates, Spearman between family scores, averaged.
  D6  coverage: share of the large-cap universe each month with fewer than 3 families, by reason (no
      fundamentals row; equity <= 5% of assets, negative included; other), and the equal-weighted return
      from the next close to 252 sessions later of the excluded vs the included.
Readings (fixed now):
  D1  a book that now passes S37's three tests becomes a candidate under S37's procedure (shadow first);
      otherwise S37 stands on the corrected data.
  D2  if the alpha that t = 2 needs is above 4%/yr for every book, S37 could not have detected a factor
      premium of the size the literature reports for large caps (1-3%/yr): "not detected", not "absent".
  D3  the equal- vs cap-weighted gap is a handicap no equal-weight selection book escapes; reported only.
  D4  a family with |NW t| >= 2 among large caps has content the top-30 books did not harvest (a
      construction question); |t| < 2 for every family means no detectable content (a signal question).
  D5  a value-momentum correlation below -0.3 means the equal-weight composite cancels its two largest bets.
  D6  more than 10% of large caps excluded AND the excluded beating the included by more than 3%/yr means
      the equity filter removes a large-cap return source (buyback compounders with small or negative equity).
Multiple testing: D1 re-evaluates S37's nine hypotheses on corrected data (+0, as the S47 restatements);
D2-D6 are diagnostics (+0).

Usage: python -m agent.s49_largecap_diag [--start 2017-01-01] [--end 2026-08-31] [--s37 PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import time

import numpy as np
import pandas as pd

from agent.books.data import fundamentals, load_market
from agent.books.factors import factor_scores, latest_before
from agent.books.long_term import ADV_FLOOR
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
LARGE = 10e9
SMALL_FLOOR = 1e8                     # factor_scores' own market-cap floor
FAMS = ["value", "quality", "momentum", "lowvol", "composite"]
H_IC, H_COV = 21, 252
MIN_NAMES = 30


def month_ends(index: pd.DatetimeIndex, start: str, end: str) -> list[pd.Timestamp]:
    d = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    return list(pd.Series(d, index=d).groupby([d.year, d.month]).last())


def split_universe(tradable_row: pd.Series, close_row: pd.Series, fund: pd.DataFrame, day: pd.Timestamp) -> tuple[pd.Index, pd.Index, pd.Series]:
    names = tradable_row[tradable_row].index
    f = latest_before(fund, day).reindex(names)
    mcap = close_row.reindex(names) * f["shares"]
    return mcap[mcap >= LARGE].index, mcap[(mcap < LARGE) & (mcap >= SMALL_FLOOR)].index, mcap


def fwd(adj: pd.DataFrame, day: pd.Timestamp, h: int) -> pd.Series | None:
    pos = adj.index.get_loc(day)
    if pos + 1 + h >= len(adj):
        return None
    return adj.iloc[pos + 1 + h] / adj.iloc[pos + 1] - 1


def summarize(series: dict[pd.Timestamp, float]) -> dict:
    s = pd.Series(series).dropna()
    if s.empty:
        return {"n": 0}
    by = s.groupby(s.index.year).mean()
    return {"n": int(len(s)), "mean": float(s.mean()), "t_nw": newey_west_t(s.to_numpy(), lag=3),
            "by_year": {int(y): round(float(v), 4) for y, v in by.items()}}


def ic_and_corr(market, fund, days, trad) -> tuple[dict, dict, dict, dict]:
    ic = {u: {f: {} for f in FAMS} for u in ("large", "small")}
    corr = {u: [] for u in ("large", "small")}
    cov_rows, sizes = [], []
    for d in days:
        large, small, mcap = split_universe(trad.loc[d], market.close.loc[d], fund, d)
        r21 = fwd(market.adj, d, H_IC)
        for u, names in (("large", large), ("small", small)):
            if len(names) < MIN_NAMES:
                continue
            fs = factor_scores(market, fund, d, names)
            if u == "large":
                cov_rows.append(coverage_row(fs, fund, d, names, market))
                sizes.append(len(names))
            fs = fs[fs["n_families"] >= 3]
            corr[u].append(fs[["value", "quality", "momentum", "lowvol"]].corr(method="spearman"))
            if r21 is None:
                continue
            y = r21.reindex(fs.index)
            for f in FAMS:
                x = fs[f]
                ok = x.notna() & y.notna()
                if ok.sum() >= MIN_NAMES:
                    ic[u][f][d] = float(x[ok].corr(y[ok], method="spearman"))
    ic_sum = {u: {f: summarize(v) for f, v in fam.items()} for u, fam in ic.items()}
    corr_sum = {u: (sum(c) / len(c)).round(3).to_dict() if c else {} for u, c in corr.items()}
    cov = pd.DataFrame(cov_rows)
    return ic_sum, corr_sum, coverage_summary(cov), {"large_n_median": float(np.median(sizes)) if sizes else None}


def coverage_row(fs: pd.DataFrame, fund: pd.DataFrame, d: pd.Timestamp, names: pd.Index, market) -> dict:
    f = latest_before(fund, d).reindex(names)
    excl = fs.index[fs["n_families"] < 3]
    no_row = f.index[f["assets"].isna() & f["equity"].isna()]
    low_eq = f.index[(f["equity"] <= 0.05 * f["assets"]) & f["assets"].notna()]
    r252 = fwd(market.adj, d, H_COV)
    row = {"day": d, "n": len(names), "excluded": len(excl), "no_fundamentals": len(set(excl) & set(no_row)),
           "low_equity": len(set(excl) & set(low_eq)), "excluded_names": list(excl)}
    row["other"] = row["excluded"] - row["no_fundamentals"] - row["low_equity"]
    if r252 is not None:
        inc = fs.index[fs["n_families"] >= 3]
        row["ret_excluded"] = float(r252.reindex(excl).mean()) if len(excl) else np.nan
        row["ret_included"] = float(r252.reindex(inc).mean())
    return row


def coverage_summary(cov: pd.DataFrame) -> dict:
    if cov.empty:
        return {}
    share = cov["excluded"] / cov["n"]
    out = {"share_excluded_mean": float(share.mean()),
           "share_by_year": {int(y): round(float(v), 3) for y, v in share.groupby(cov["day"].dt.year).mean().items()},
           "reasons_mean": {k: float((cov[k] / cov["n"]).mean()) for k in ("no_fundamentals", "low_equity", "other")}}
    if "ret_excluded" in cov:
        r = cov.dropna(subset=["ret_excluded", "ret_included"])
        out["ret12m_excluded_mean"] = float(r["ret_excluded"].mean())
        out["ret12m_included_mean"] = float(r["ret_included"].mean())
        out["ret12m_gap_t_nw"] = newey_west_t((r["ret_excluded"] - r["ret_included"]).to_numpy(), lag=12)
    last = cov.iloc[-1]
    out["last_day"] = str(last["day"].date())
    out["last_excluded_examples"] = list(last["excluded_names"])[:40]
    return out


def weighting(market, fund, days_all: pd.DatetimeIndex, months: list[pd.Timestamp], trad) -> dict:
    """Daily EW and CW return of the large-cap universe, membership and shares from the previous month end."""
    members: dict[pd.Timestamp, tuple[pd.Index, pd.Series]] = {}
    for d in months:
        large, _, _ = split_universe(trad.loc[d], market.close.loc[d], fund, d)
        sh = latest_before(fund, d).reindex(large)["shares"]
        members[d] = (large, sh)
    keys = sorted(members)
    ew, cw, spy = {}, {}, {}
    ret = market.adj.pct_change()
    for i in range(1, len(days_all)):
        t, prev = days_all[i], days_all[i - 1]
        k = [m for m in keys if m < t]
        if not k:
            continue
        names, sh = members[k[-1]]
        r = ret.loc[t].reindex(names)
        w = (market.close.loc[prev].reindex(names) * sh).where(r.notna())
        if r.notna().sum() < MIN_NAMES:
            continue
        ew[t] = float(r.mean())
        cw[t] = float((w * r).sum() / w.sum())
        spy[t] = float(market.spy.loc[t] / market.spy.loc[prev] - 1)
    df = pd.DataFrame({"ew": ew, "cw": cw, "spy": spy})
    yearly = (1 + df).groupby(df.index.year).prod() - 1
    total = (1 + df).prod() - 1
    years = (df.index[-1] - df.index[0]).days / 365.25
    return {"by_year": {int(y): {k: round(float(v), 4) for k, v in row.items()} for y, row in yearly.iterrows()},
            "cagr": {k: float((1 + v) ** (1 / years) - 1) for k, v in total.items()},
            "tracking_cw_vs_spy_ann": float((df["cw"] - df["spy"]).std() * np.sqrt(252))}


def power(s37_path: str | None) -> dict:
    if not s37_path:
        return {}
    d = json.load(open(s37_path))
    out = {"source": os.path.basename(s37_path)}
    for k, m in d["books"].items():
        a, t = m.get("alpha2_ann_pct"), m.get("alpha2_t_nw")
        se = a / t if t not in (None, 0) and np.isfinite(t) else np.nan
        out[k] = {"alpha2": a, "t": t, "se": float(abs(se)) if np.isfinite(se) else None,
                  "alpha_for_t2": float(2 * abs(se)) if np.isfinite(se) else None,
                  "years_for_2pct_at_t2": float((2 * abs(se) / 2.0) ** 2 * m.get("years", 9.67)) if np.isfinite(se) else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--s37", default=None, help="D1's s37_largecap_*.json (default: the newest)")
    args = ap.parse_args()
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
    trad = market.tradable(ADV_FLOOR, np.inf)
    months = month_ends(market.adj.index, args.start, args.end)
    print(f"market {market.adj.shape}, {len(months)} month ends [{time.time() - t0:.0f}s]", flush=True)
    ic, corr, cov, meta = ic_and_corr(market, fund, months, trad)
    print(f"D4/D5/D6 done [{time.time() - t0:.0f}s]", flush=True)
    days_all = market.adj.loc[args.start:args.end].index
    wt = weighting(market, fund, days_all, months, trad)
    print(f"D3 done [{time.time() - t0:.0f}s]", flush=True)
    s37 = args.s37 or (sorted(glob.glob(os.path.join(OUT_DIR, "s37_largecap_*.json")))[-1:] or [None])[0]
    pw = power(s37)
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end,
           "D2_power": pw, "D3_weighting": wt, "D4_ic": ic, "D5_family_corr": corr, "D6_coverage": cov, **meta}
    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"s49_largecap_diag_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump(out, f, indent=1, default=lambda x: None if isinstance(x, float) and np.isnan(x) else str(x))
    L = [f"# S49 — why selection adds nothing among large caps: diagnostics ({stamp})", "",
         f"{args.start} → {args.end}; large = point-in-time market cap >= $10B (median {meta['large_n_median']:.0f} names at month ends); "
         "small = the rest of v1's universe ($100M–$10B). Families z-scored within each universe.", "",
         "## D4 information coefficient (Spearman, next close → +21 sessions, month ends)", "",
         "| family | large mean | large t | small mean | small t |", "|---|---|---|---|---|"]
    for f in FAMS:
        a, b = ic["large"][f], ic["small"][f]
        L.append(f"| {f} | {a.get('mean', float('nan')):+.4f} | {a.get('t_nw', float('nan')):.2f} | {b.get('mean', float('nan')):+.4f} | {b.get('t_nw', float('nan')):.2f} |")
    L += ["", "## D5 family correlations (mean Spearman)", "", "```", json.dumps(corr, indent=1), "```", "",
          "## D3 weighting (large-cap universe, no costs)", "",
          f"CAGR: equal {wt['cagr']['ew']:+.2%}, cap {wt['cagr']['cw']:+.2%}, SPY {wt['cagr']['spy']:+.2%}; "
          f"cap-weighted vs SPY tracking {wt['tracking_cw_vs_spy_ann']:.2%}/yr", "",
          "| year | equal | cap | SPY |", "|---|---|---|---|"]
    for y, r in wt["by_year"].items():
        L.append(f"| {y} | {r['ew']:+.1%} | {r['cw']:+.1%} | {r['spy']:+.1%} |")
    L += ["", "## D6 coverage (large caps with fewer than 3 families)", "", "```", json.dumps({k: v for k, v in cov.items() if k != 'last_excluded_examples'}, indent=1),
          "```", "", f"Excluded on {cov.get('last_day')}: {', '.join(cov.get('last_excluded_examples', []))}", "",
          "## D2 power (from D1)", "", "| book | alpha2 | t | SE | alpha for t=2 | years for 2%/yr at t=2 |", "|---|---|---|---|---|---|"]
    for k, v in pw.items():
        if isinstance(v, dict):
            L.append(f"| {k} | {v['alpha2']:+.1f}% | {v['t']:.2f} | {v['se'] or float('nan'):.1f}% | {v['alpha_for_t2'] or float('nan'):.1f}% | "
                     f"{v['years_for_2pct_at_t2'] or float('nan'):.0f} |")
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
