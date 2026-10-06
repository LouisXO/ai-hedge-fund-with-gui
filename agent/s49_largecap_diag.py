"""S49 — why does selection add nothing among large caps? Diagnostics, pre-registered 2026-10-06 before any run.

Not candidate rules: nothing here can change a running book or be adopted. What looks useful goes to the
hypothesis queue with its own pre-registration and data after 2026-10 (docs/AGENT_PLAN.md, S49 and
S49 补充). The user asked (2026-10-06) why S37 found no large-cap edge. S37 ran on 2026-09-24, before
the S47/S47b data corrections, and the same day's code review found its large-cap universe wrong
(V / STZ / BRK.B never in it, GOOG and GOOGL twice, TSM x5, market caps off by the split ratio after a
split) and its lc_ew baseline not an equal weight.

  D1a S37 re-run unchanged on the corrected data: `python -m agent.s37_largecap` (run separately).
  D1b S37's books on the corrected universe (--d1b): shares from the latest filing, else the current
      shares_override count (S28; not point in time, only where the filing has none); split-safe market
      cap = shares x close/adj on the filing day x adj today; one ticker per CIK (most traded that day);
      20-F / 40-F filers out. Variants lc_composite, lc_rankz, lc_qlv, lc_qv, lc_mom, lc_value,
      lc_composite50 (lc_payout and lc_secneutral are void: their factor code is wrong). Baseline: the
      universe equal-weighted and rebalanced daily, alpha2 by the same SPY + size regression.
  D2  power: each book's alpha2 standard error (alpha2 / t) and the alpha that t = 2 needs (2 x SE).
  D3  weighting: daily equal- vs cap-weighted return of the corrected large-cap universe against SPY, by
      year (members and share counts as of the previous month end, weights at the previous close).
  D4  information coefficient: on the last trading day of each month, Spearman correlation between each
      family score (z-scored within the universe; n_families >= 3, as the books require) and the return
      from the next close to 21 sessions later. Large caps (>= $10B) and the rest of v1's universe
      ($100M–$10B), each with the corrected definition and scored within itself. Mean, Newey-West t
      (lag 3), mean by year.
  D5  family correlations: the same dates, Spearman between family scores, averaged.
  D6  coverage: share of the large-cap universe each month with fewer than 3 families, by reason (no
      fundamentals row; equity <= 5% of assets, negative included; other), and the equal-weighted return
      from the next close to 252 sessions later of the excluded vs the included.
Readings (fixed before any run): see docs/AGENT_PLAN.md S49. In short: D1a/D1b a book is a candidate only
if it passes all three S37 tests (alpha2 >= 3%/yr with t >= 2, beats the equal-weight baseline by >= 2%/yr,
beta_size <= 0.3); D2 alpha-for-t2 above 4%/yr everywhere = "not detected", not "absent"; D4 |NW t| >= 2
among large caps = content the books did not harvest, else no detectable content; D5 value-momentum below
-0.3 = the composite cancels its two largest bets; D6 > 10% excluded and the excluded ahead by > 3%/yr =
the equity filter removes a large-cap return source. Multiple testing: +0 (re-estimates and diagnostics).

Usage: python -m agent.s49_largecap_diag [--start 2017-01-01] [--end 2026-08-31] [--s37 PATH]
       python -m agent.s49_largecap_diag --d1b
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

from agent.books.data import fundamentals, load_market, share_overrides
from agent.books.engine import TRADING_DAYS, simulate
from agent.books.factors import factor_scores, latest_before
from agent.books.long_term import ADV_FLOOR
from agent.books.long_v2 import targets_from_scores
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
LARGE = 10e9
SMALL_FLOOR = 1e8                     # factor_scores' own market-cap floor
FAMS = ["value", "quality", "momentum", "lowvol", "composite"]
FOREIGN_FORMS = ("20-F", "20-F/A", "40-F", "40-F/A")
H_IC, H_COV = 21, 252
MIN_NAMES = 30


# --- the corrected universe (S49 补充) -------------------------------------------------------------

class Universe:
    """Split-safe market caps, one ticker per CIK, no 20-F / 40-F filers."""

    def __init__(self, market, fund: pd.DataFrame, overrides: pd.Series, foreign: set[int]):
        self.market, self.overrides, self.foreign = market, overrides, foreign
        self.trad = market.tradable(ADV_FLOOR, np.inf)
        ratio = market.close / market.adj                    # moves by the split ratio at a split
        pos = ratio.index.searchsorted(fund["filed"].to_numpy(), side="right") - 1
        col = ratio.columns.get_indexer(fund["ticker"])
        ok = (pos >= 0) & (col >= 0)
        r = np.full(len(fund), np.nan)
        r[ok] = ratio.to_numpy()[pos[ok], col[ok]]
        self.fund = fund.assign(ratio_f=r)

    def factor(self, day: pd.Timestamp, names: pd.Index) -> tuple[pd.Series, pd.Series]:
        """(k, cik) per name: market cap = k x adj; k = shares x close/adj on the filing day."""
        f = latest_before(self.fund, day).reindex(names)
        sh = f["shares"].fillna(self.overrides.reindex(names))
        k = sh * f["ratio_f"]
        raw = sh * self.market.close.loc[day].reindex(names) / self.market.adj.loc[day].reindex(names)
        return k.fillna(raw), f["cik"]

    def split(self, day: pd.Timestamp) -> tuple[pd.Index, pd.Index, pd.Series]:
        row = self.trad.loc[day]
        names = row[row].index
        k, cik = self.factor(day, names)
        mcap = k * self.market.adj.loc[day].reindex(names)
        d = pd.DataFrame({"mcap": mcap, "cik": cik, "adv": self.market.adv20.loc[day].reindex(names)}).dropna(subset=["mcap"])
        d = d[~d["cik"].isin(self.foreign)].sort_values("adv", ascending=False)
        d = pd.concat([d[d["cik"].isna()], d[d["cik"].notna()].drop_duplicates("cik")])
        large = d.index[d["mcap"] >= LARGE]
        small = d.index[(d["mcap"] < LARGE) & (d["mcap"] >= SMALL_FLOOR)]
        return pd.Index(large), pd.Index(small), d["mcap"]


def load(start: str):
    with PanelStore(read_only=True) as store:
        market = load_market(store, start)
        fund = fundamentals(store)
        ov = share_overrides(store).sort_values("as_of").groupby("ticker")["shares"].last()
        q = ", ".join(f"'{x}'" for x in FOREIGN_FORMS)
        foreign = {int(c) for (c,) in store.con.execute(f"SELECT DISTINCT cik FROM xbrl_facts WHERE form IN ({q})").fetchall()}
    return market, fund, ov, foreign


# --- D4 / D5 / D6 ------------------------------------------------------------------------------------

def month_ends(index: pd.DatetimeIndex, start: str, end: str) -> list[pd.Timestamp]:
    d = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    return list(pd.Series(d, index=d).groupby([d.year, d.month]).last())


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


def coverage_row(fs: pd.DataFrame, uni: Universe, d: pd.Timestamp, names: pd.Index) -> dict:
    f = latest_before(uni.fund, d).reindex(names)
    excl = fs.index[fs["n_families"] < 3]
    no_row = set(f.index[f["assets"].isna() & f["equity"].isna()])
    low_eq = set(f.index[(f["equity"] <= 0.05 * f["assets"]) & f["assets"].notna()])
    r252 = fwd(uni.market.adj, d, H_COV)
    row = {"day": d, "n": len(names), "excluded": len(excl), "no_fundamentals": len(set(excl) & no_row),
           "low_equity": len(set(excl) & low_eq), "excluded_names": list(excl)}
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
    r = cov.dropna(subset=["ret_excluded", "ret_included"]) if "ret_excluded" in cov else cov.iloc[0:0]
    if len(r):
        out["ret12m_excluded_mean"] = float(r["ret_excluded"].mean())
        out["ret12m_included_mean"] = float(r["ret_included"].mean())
        out["ret12m_gap_t_nw"] = newey_west_t((r["ret_excluded"] - r["ret_included"]).to_numpy(), lag=12)
    last = cov.iloc[-1]
    out["last_day"] = str(last["day"].date())
    out["last_excluded_examples"] = list(last["excluded_names"])[:40]
    return out


def ic_and_corr(uni: Universe, fund: pd.DataFrame, days: list[pd.Timestamp], overrides: pd.Series) -> tuple[dict, dict, dict, dict]:
    market = uni.market
    ic = {u: {f: {} for f in FAMS} for u in ("large", "small")}
    corr = {u: [] for u in ("large", "small")}
    cov_rows, sizes = [], []
    for d in days:
        large, small, _ = uni.split(d)
        r21 = fwd(market.adj, d, H_IC)
        for u, names in (("large", large), ("small", small)):
            if len(names) < MIN_NAMES:
                continue
            fs = factor_scores(market, fund, d, names, shares_override=overrides)
            if u == "large":
                cov_rows.append(coverage_row(fs, uni, d, names))
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
    return ic_sum, corr_sum, coverage_summary(pd.DataFrame(cov_rows)), {"large_n_median": float(np.median(sizes)) if sizes else None}


# --- D3 ----------------------------------------------------------------------------------------------

def weighting_series(uni: Universe, days_all: pd.DatetimeIndex, months: list[pd.Timestamp]) -> pd.DataFrame:
    """Daily EW and CW return of the large-cap universe; members and k (mcap / adj) from the previous month end."""
    market = uni.market
    members = {}
    for d in months:
        large, _, _ = uni.split(d)
        k, _ = uni.factor(d, large)
        members[d] = k
    keys = sorted(members)
    ret = market.adj.pct_change()
    rows = {}
    for i in range(1, len(days_all)):
        t, prev = days_all[i], days_all[i - 1]
        past = [m for m in keys if m < t]
        if not past:
            continue
        k = members[past[-1]]
        r = ret.loc[t].reindex(k.index)
        w = (k * market.adj.loc[prev].reindex(k.index)).where(r.notna())
        if r.notna().sum() < MIN_NAMES:
            continue
        rows[t] = {"ew": float(r.mean()), "cw": float((w * r).sum() / w.sum()),
                   "spy": float(market.spy.loc[t] / market.spy.loc[prev] - 1)}
    return pd.DataFrame.from_dict(rows, orient="index")


def weighting(df: pd.DataFrame) -> dict:
    yearly = (1 + df).groupby(df.index.year).prod() - 1
    total = (1 + df).prod() - 1
    years = (df.index[-1] - df.index[0]).days / 365.25
    return {"by_year": {int(y): {k: round(float(v), 4) for k, v in row.items()} for y, row in yearly.iterrows()},
            "cagr": {k: float((1 + v) ** (1 / years) - 1) for k, v in total.items()},
            "tracking_cw_vs_spy_ann": float((df["cw"] - df["spy"]).std() * np.sqrt(TRADING_DAYS))}


def alpha2(r: pd.Series, spy: pd.Series, iwm: pd.Series) -> dict:
    """The engine's two-factor regression (SPY + IWM - SPY) on a daily return series."""
    m = spy.pct_change().reindex(r.index).fillna(0)
    f = (iwm.pct_change() - spy.pct_change()).reindex(r.index).fillna(0)
    X = np.column_stack([np.ones(len(r)), m.to_numpy(), f.to_numpy()])
    b = np.linalg.lstsq(X, r.to_numpy(), rcond=None)[0]
    resid = r.to_numpy() - X @ b
    return {"alpha2_ann_pct": float(b[0] * TRADING_DAYS * 100), "alpha2_t_nw": newey_west_t(resid + b[0], lag=5),
            "beta_mkt": float(b[1]), "beta_size": float(b[2])}


# --- D2 ----------------------------------------------------------------------------------------------

def power(books: dict) -> dict:
    out = {}
    for k, m in books.items():
        a, t = m.get("alpha2_ann_pct"), m.get("alpha2_t_nw")
        se = abs(a / t) if a is not None and t not in (None, 0) and np.isfinite(t) else np.nan
        yrs = m.get("years", 9.67)
        out[k] = {"alpha2": a, "t": t, "se": float(se) if np.isfinite(se) else None,
                  "alpha_for_t2": float(2 * se) if np.isfinite(se) else None,
                  "years_for_2pct_at_t2": float(se ** 2 * yrs) if np.isfinite(se) else None}   # SE 1%/yr at t = 2 for 2%/yr
    return out


# --- D1b ---------------------------------------------------------------------------------------------

VARIANTS = ("lc_composite", "lc_rankz", "lc_qlv", "lc_qv", "lc_mom", "lc_value")


def d1b(uni: Universe, fund: pd.DataFrame, overrides: pd.Series, start: str, end: str, exec_frac: float) -> dict:
    market = uni.market
    days = market.adj.loc[start:end].index
    scores = {k: {} for k in VARIANTS}
    sizes = {}
    t0 = time.time()
    for i, d in enumerate(days):
        large, _, _ = uni.split(d)
        sizes[d] = len(large)
        if len(large) < 50:
            continue
        base = factor_scores(market, fund, d, large, shares_override=overrides)
        base = base[base["n_families"] >= 3]
        rz = factor_scores(market, fund, d, large, norm="rank", shares_override=overrides)
        rz = rz[rz["n_families"] >= 3]
        scores["lc_composite"][d] = base["composite"].dropna().sort_values(ascending=False)
        scores["lc_rankz"][d] = rz["composite"].dropna().sort_values(ascending=False)
        scores["lc_qlv"][d] = base[["quality", "lowvol"]].mean(axis=1).dropna().sort_values(ascending=False)
        scores["lc_qv"][d] = base[["quality", "value"]].mean(axis=1).dropna().sort_values(ascending=False)
        scores["lc_mom"][d] = base["momentum"].dropna().sort_values(ascending=False)
        scores["lc_value"][d] = base["value"].dropna().sort_values(ascending=False)
        if i % 250 == 0:
            print(f"  {d.date()} universe {len(large)} [{time.time() - t0:.0f}s]", flush=True)
    books = {}
    for name, sc in scores.items():
        books[name] = simulate(market, targets_from_scores(sc, 30), start, end, 30, None, exec_frac, cash_in_spy=True).metrics
        print(f"  {name}: alpha2 {books[name]['alpha2_ann_pct']:+.1f}% (t {books[name]['alpha2_t_nw']:.2f}) β_size {books[name]['beta_size']:.2f}", flush=True)
    books["lc_composite50"] = simulate(market, targets_from_scores(scores["lc_composite"], 50), start, end, 50, None, exec_frac,
                                       cash_in_spy=True).metrics
    months = month_ends(market.adj.index, start, end)
    ws = weighting_series(uni, market.adj.loc[start:end].index, months)
    years = (ws.index[-1] - ws.index[0]).days / 365.25
    base_ew = {**alpha2(ws["ew"], market.spy, market.iwm), "years": round(years, 2),
               "cagr_pct": float(((1 + ws["ew"]).prod() ** (1 / years) - 1) * 100)}
    ew = base_ew["alpha2_ann_pct"]
    verdict = {k: {"alpha_ok": m["alpha2_ann_pct"] >= 3.0 and m["alpha2_t_nw"] >= 2.0, "beats_ew": m["alpha2_ann_pct"] - ew >= 2.0,
                   "large_cap": m["beta_size"] <= 0.3} for k, m in books.items()}
    for v in verdict.values():
        v["adopt"] = all(v.values())
    s = pd.Series(sizes)
    return {"books": books, "baseline_ew_daily": base_ew, "verdict": verdict,
            "universe_size": {"min": int(s.min()), "median": float(s.median()), "max": int(s.max())}}


# --- report ------------------------------------------------------------------------------------------

def _f(x, fmt):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else format(x, fmt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--s37", default=None, help="D1a's s37_largecap_*.json (default: the newest)")
    ap.add_argument("--d1b", action="store_true", help="run D1b (S37's books on the corrected universe) instead of D2-D6")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    t0 = time.time()
    market, fund, ov, foreign = load(args.start)
    uni = Universe(market, fund, ov, foreign)
    print(f"market {market.adj.shape}, fundamentals {len(fund)}, overrides {len(ov)}, foreign CIKs {len(foreign)} [{time.time() - t0:.0f}s]", flush=True)
    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.d1b:
        res = d1b(uni, fund, ov, args.start, args.end, args.exec_frac)
        res["D2_power"] = power(res["books"])
        base = os.path.join(OUT_DIR, f"s49_d1b_{stamp}")
        with open(base + ".json", "w") as f:
            json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end, **res},
                      f, indent=1, default=float)
        L = [f"# S49 D1b — S37's books on the corrected large-cap universe ({stamp})", "",
             f"Universe size {res['universe_size']['min']}–{res['universe_size']['max']} (median {res['universe_size']['median']:.0f}). "
             f"Baseline: equal weight, rebalanced daily: alpha2 {res['baseline_ew_daily']['alpha2_ann_pct']:+.1f}% "
             f"(t {res['baseline_ew_daily']['alpha2_t_nw']:.2f}), β size {res['baseline_ew_daily']['beta_size']:.2f}, CAGR {res['baseline_ew_daily']['cagr_pct']:+.1f}%.", "",
             "| book | CAGR | SPY | alpha2/yr | t | β mkt | β size | Sharpe | MaxDD | turnover | SE | alpha for t=2 | adopt |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, m in res["books"].items():
            p, v = res["D2_power"][k], res["verdict"][k]
            L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['beta_mkt']:.2f} | "
                     f"{m['beta_size']:.2f} | {m['sharpe']:.2f} | {m['max_drawdown_pct']:.0f}% | {m['turnover_ann']:.1f} | {_f(p['se'], '.1f')}% | "
                     f"{_f(p['alpha_for_t2'], '.1f')}% | {'**ADOPT**' if v['adopt'] else 'no'} |")
        text = "\n".join(L) + "\n"
        with open(base + ".md", "w") as f:
            f.write(text)
        print(text)
        return 0

    months = month_ends(market.adj.index, args.start, args.end)
    print(f"{len(months)} month ends", flush=True)
    ic, corr, cov, meta = ic_and_corr(uni, fund, months, ov)
    print(f"D4/D5/D6 done [{time.time() - t0:.0f}s]", flush=True)
    ws = weighting_series(uni, market.adj.loc[args.start:args.end].index, months)
    wt = weighting(ws)
    print(f"D3 done [{time.time() - t0:.0f}s]", flush=True)
    s37 = args.s37 or (sorted(glob.glob(os.path.join(OUT_DIR, "s37_largecap_*.json")))[-1:] or [None])[0]
    pw = {"source": os.path.basename(s37), **power(json.load(open(s37))["books"])} if s37 else {}
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end,
           "D2_power_s37": pw, "D3_weighting": wt, "D4_ic": ic, "D5_family_corr": corr, "D6_coverage": cov, **meta}
    base = os.path.join(OUT_DIR, f"s49_largecap_diag_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump(out, f, indent=1, default=lambda x: None if isinstance(x, float) and np.isnan(x) else str(x))
    L = [f"# S49 — why selection adds nothing among large caps: diagnostics ({stamp})", "",
         f"{args.start} → {args.end}; corrected universes (S49 补充): large >= $10B (median {_f(meta['large_n_median'], '.0f')} names at month ends), "
         "small = $100M–$10B, one ticker per CIK, no 20-F/40-F filers, split-safe market caps. Families z-scored within each universe.", "",
         "## D4 information coefficient (Spearman, next close → +21 sessions, month ends)", "",
         "| family | large mean | large t | small mean | small t |", "|---|---|---|---|---|"]
    for f in FAMS:
        a, b = ic["large"][f], ic["small"][f]
        L.append(f"| {f} | {_f(a.get('mean'), '+.4f')} | {_f(a.get('t_nw'), '.2f')} | {_f(b.get('mean'), '+.4f')} | {_f(b.get('t_nw'), '.2f')} |")
    L += ["", "## D5 family correlations (mean Spearman)", "", "```", json.dumps(corr, indent=1), "```", "",
          "## D3 weighting (corrected large-cap universe, no costs)", "",
          f"CAGR: equal {wt['cagr']['ew']:+.2%}, cap {wt['cagr']['cw']:+.2%}, SPY {wt['cagr']['spy']:+.2%}; "
          f"cap-weighted vs SPY tracking {wt['tracking_cw_vs_spy_ann']:.2%}/yr", "",
          "| year | equal | cap | SPY |", "|---|---|---|---|"]
    for y, r in wt["by_year"].items():
        L.append(f"| {y} | {r['ew']:+.1%} | {r['cw']:+.1%} | {r['spy']:+.1%} |")
    L += ["", "## D6 coverage (large caps with fewer than 3 families)", "", "```",
          json.dumps({k: v for k, v in cov.items() if k != "last_excluded_examples"}, indent=1), "```", "",
          f"Excluded on {cov.get('last_day')}: {', '.join(cov.get('last_excluded_examples', []))}", "",
          f"## D2 power (S37 as run: {pw.get('source')})", "", "| book | alpha2 | t | SE | alpha for t=2 | years for 2%/yr at t=2 |", "|---|---|---|---|---|---|"]
    for k, v in pw.items():
        if isinstance(v, dict):
            L.append(f"| {k} | {_f(v['alpha2'], '+.1f')}% | {_f(v['t'], '.2f')} | {_f(v['se'], '.1f')}% | {_f(v['alpha_for_t2'], '.1f')}% | {_f(v['years_for_2pct_at_t2'], '.0f')} |")
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
