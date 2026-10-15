"""S53 step 4: shadow line composite_long_fpi — long v1 on a universe widened to foreign issuers (never traded).

docs/AGENT_PLAN.md "### S53" and "### S53 补充". Same code and rules as v1 (four families equal weight, top 30
in / out below 60, entry zone, half the quoted spread, idle cash in SPY); only the inputs widen:
  market     panel.db bars + fpi.db bars of the new issuers (listing mask from panel's listing_status)
  rows       panel.db fundamentals for every CIK that is not in fpi_issuers; fpi.db fundamentals for those that
             are (every unit, IFRS, 6-K quarters). Per scoring day: the latest row filed on or before it,
             no older than 200 days, or 480 for an annual_only row; money in USD at the previous trading
             day's rate (fx_daily, last on or before the day before)
Backtest 2017-01-01 -> 2026-08-31 against v1 on the same data snapshot, with the pre-registered checks:
  (a) alpha2 not lower than v1's by more than 1%/yr   (b) annual turnover <= 1.15 x v1's
  (c) max drawdown not deeper than v1's by more than 3 points
  (d) foreign issuers' family z-scores: mean within 0.25 of US names', sd ratio 0.67-1.5 (each family, month
      ends); their mean share of the top 30 <= 2x their share of the scored universe
Survivors only for the new issuers: passing is necessary, not sufficient (S53 补充 2). Report also the
return the new issuers contributed. Multiple testing: one variant.

Usage: python -m agent.s53_fpi backtest [--start 2017-01-01 --end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import numpy as np
import pandas as pd

from agent.books import long_v2_bundle as v2
from agent.books.data import Market, fundamentals, listed_mask, load_market
from agent.books.engine import simulate
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR, TOP_N
from agent.s44_v2_bundle import entry_zone
from agent.sources import fpi_data
from hedge_fund.features.panel import PanelStore

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site-data", "validation")
SIGNAL = "composite_long_fpi"
MAX_AGE, MAX_AGE_ANNUAL = 200, 480
MONEY = ["rev_ttm", "gp_ttm", "cogs_ttm", "opinc_ttm", "cfo_ttm", "ni_ttm", "assets", "equity", "debt", "assets_1y"]
FAMILIES = ["value", "quality", "momentum", "lowvol"]
KEYS = ["cagr_pct", "alpha2_ann_pct", "alpha2_t_nw", "excess_cagr_pct", "active_t_nw", "max_drawdown_pct", "sharpe",
        "n_trades", "trade_hit_rate", "avg_exposure", "by_year"]


def union_market(start: str) -> tuple[Market, Market, list[str]]:
    """(panel market, widened market, the new tickers)."""
    with PanelStore(read_only=True) as p:
        m = load_market(p, start)
        with fpi_data.store(read_only=True) as s:
            new = s.con.execute("SELECT ticker FROM fpi_issuers WHERE NOT in_panel").df()["ticker"].tolist()
            lb = (pd.Timestamp(start) - pd.Timedelta(days=400)).date().isoformat()
            wide = {k: s.bars_wide(k, start=lb) for k in ("close", "adj_close", "open", "volume")}
        new = [t for t in new if t in wide["close"].columns]
        idx = m.adj.index
        close, adj, opn, vol = (wide[k].reindex(index=idx, columns=new) for k in ("close", "adj_close", "open", "volume"))
        listed = listed_mask(p, idx, new)
    u = Market(adj=pd.concat([m.adj, adj], axis=1), adj_open=pd.concat([m.adj_open, opn * (adj / close)], axis=1),
               close=pd.concat([m.close, close], axis=1),
               adv20=pd.concat([m.adv20, (close * vol).rolling(20).mean()], axis=1),
               listed=pd.concat([m.listed, listed.reindex(index=idx, columns=new).fillna(False)], axis=1),
               spy=m.spy, spread=m.spread, iwm=m.iwm)
    return m, u, new


def union_rows() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, set[int]]:
    """(v1's fundamentals, the shadow line's, fx wide (date x currency, USD per unit), foreign CIKs)."""
    with PanelStore(read_only=True) as p:
        dom = fundamentals(p)
    with fpi_data.store(read_only=True) as s:
        fpi = s.con.execute("SELECT * FROM fundamentals_pit").df()
        iss = set(s.con.execute("SELECT cik FROM fpi_issuers").df()["cik"].astype(int))
        fx = s.con.execute("SELECT currency, trade_date, usd_per_unit FROM fx_daily").df()
    fpi["filed"] = pd.to_datetime(fpi["filed"])
    keep = dom[~dom["cik"].astype(int).isin(iss)].assign(currency="USD", annual_only=False)
    rows = pd.concat([keep, fpi[[c for c in fpi.columns if c in set(keep.columns)]]], ignore_index=True)
    fxw = fx.assign(trade_date=pd.to_datetime(fx["trade_date"])).pivot_table(index="trade_date", columns="currency",
                                                                             values="usd_per_unit").sort_index().ffill()
    foreign = set(dom.loc[dom["cik"].astype(int).isin(iss), "cik"].astype(int)) | iss
    return dom, rows, fxw, foreign


def day_rows(rows: pd.DataFrame, fxw: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """Latest row per ticker for `day` under the shadow staleness rule, money in USD."""
    age = (day - rows["filed"]).dt.days
    ok = (age >= 0) & ((age <= MAX_AGE) | (rows["annual_only"].fillna(False).astype(bool) & (age <= MAX_AGE_ANNUAL)))
    f = rows[ok].sort_values("filed").drop_duplicates("ticker", keep="last").set_index("ticker")
    non = f["currency"].ne("USD")
    if non.any():
        prior = fxw.loc[:day - pd.Timedelta(days=1)]
        rate = prior.iloc[-1] if len(prior) else pd.Series(dtype=float)
        k = f.loc[non, "currency"].map(rate)                     # a currency without a rate: the row is dropped
        cols = [c for c in MONEY if c in f.columns]
        f.loc[non, cols] = f.loc[non, cols].mul(k, axis=0)
        f = f[~(non & k.reindex(f.index).isna())]
    return f


def scored(market: Market, latest: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    ok = market.tradable(ADV_FLOOR, np.inf).loc[day]
    fs = factor_scores(market, None, day, ok[ok].index, latest=latest)
    fs = fs[(fs["n_families"] >= 3) & fs["composite"].notna()]
    return fs.sort_values("composite", ascending=False)


def backtest(start: str, end: str) -> dict:
    t0 = time.time()
    m, u, new = union_market(start)
    dom, rows, fxw, foreign = union_rows()
    tick_cik = rows.drop_duplicates("ticker", keep="last").set_index("ticker")["cik"].astype(int)
    is_foreign = lambda idx: pd.Index(idx).map(lambda t: tick_cik.get(t, -1) in foreign)
    days = m.adj.loc[start:end].index
    lv1, lfp, dz = {}, {}, []
    for i, d in enumerate(days):
        s1 = v2.scored(m, dom, d)
        lv1[d] = s1.index[:v2.KEEP_MULT * TOP_N].tolist()
        sf = scored(u, day_rows(rows, fxw, d), d)
        lfp[d] = sf.index[:v2.KEEP_MULT * TOP_N].tolist()
        if i == len(days) - 1 or days[i + 1].month != d.month:   # month end: family distributions, top-30 share
            fo = np.asarray(is_foreign(sf.index), dtype=bool)
            top = np.asarray(is_foreign(sf.index[:TOP_N]), dtype=bool)
            rec = {"day": d, "n": len(sf), "n_foreign": int(fo.sum()), "share_scored": float(fo.mean()) if len(fo) else np.nan,
                   "share_top30": float(top.mean()) if len(top) else np.nan,
                   "new_in_top30": int(pd.Index(sf.index[:TOP_N]).isin(new).sum())}
            for fam in FAMILIES:
                a, b = sf.loc[fo, fam].dropna(), sf.loc[~fo, fam].dropna()
                rec[f"{fam}_mean_diff"] = float(a.mean() - b.mean()) if len(a) > 4 and len(b) > 4 else np.nan
                rec[f"{fam}_sd_ratio"] = float(a.std() / b.std()) if len(a) > 4 and len(b) > 4 and b.std() > 0 else np.nan
            dz.append(rec)
        if i % 250 == 0:
            print(f"{d.date()} v1 {len(s1)} scored, fpi {len(sf)} scored [{time.time() - t0:.0f}s]", flush=True)
    r1 = simulate(m, lv1, start, end, TOP_N, None, 0.5, cash_in_spy=True, sizes=entry_zone(lv1, TOP_N))
    rf = simulate(u, lfp, start, end, TOP_N, None, 0.5, cash_in_spy=True, sizes=entry_zone(lfp, TOP_N))
    turn = {k: float(r.metrics["turnover_ann"]) for k, r in (("v1", r1), ("fpi", rf))}
    z = pd.DataFrame(dz).set_index("day")
    fam = {f: {"mean_diff": float(z[f"{f}_mean_diff"].mean()), "sd_ratio": float(z[f"{f}_sd_ratio"].mean())} for f in FAMILIES}
    share = {"scored": float(z["share_scored"].mean()), "top30": float(z["share_top30"].mean())}
    t = pd.DataFrame(rf.trades)
    t["net"] = t["ret_pct"] - t["cost_pct"]
    contrib = {"foreign": float(t.loc[t["ticker"].map(lambda x: tick_cik.get(x, -1) in foreign), "net"].sum() / TOP_N),
               "new_issuers": float(t.loc[t["ticker"].isin(new), "net"].sum() / TOP_N), "all": float(t["net"].sum() / TOP_N)}
    # counterfactual lists (review 2026-10-14): the shadow line without the new issuers, and without any foreign name
    is_f = {t: tick_cik.get(t, -1) in foreign for d in lfp for t in lfp[d]}
    drop_new = {d: [t for t in l if t not in set(new)] for d, l in lfp.items()}
    drop_all = {d: [t for t in l if not is_f.get(t, False)] for d, l in lfp.items()}
    cf = {}
    for k, lists in (("without_new_issuers", drop_new), ("without_foreign", drop_all)):
        rr = simulate(u, lists, start, end, TOP_N, None, 0.5, cash_in_spy=True, sizes=entry_zone(lists, TOP_N))
        cf[k] = {"alpha2_ann_pct": rr.metrics["alpha2_ann_pct"], "alpha2_t_nw": rr.metrics["alpha2_t_nw"],
                 "max_drawdown_pct": rr.metrics["max_drawdown_pct"]}
    m1, mf = r1.metrics, rf.metrics
    checks = {
        "a_alpha": mf["alpha2_ann_pct"] >= m1["alpha2_ann_pct"] - 1.0,
        "b_turnover": turn["fpi"] <= 1.15 * turn["v1"],
        "c_drawdown": mf["max_drawdown_pct"] >= m1["max_drawdown_pct"] - 3.0,
        "d_families": all(abs(v["mean_diff"]) <= 0.25 and 0.67 <= v["sd_ratio"] <= 1.5 for v in fam.values()),
        "d_top30_share": share["top30"] <= 2 * share["scored"],
    }
    out = {"start": start, "end": end, "v1": {k: m1.get(k) for k in KEYS}, "fpi": {k: mf.get(k) for k in KEYS},
           "turnover_annual": turn, "families": fam, "foreign_share": share, "contribution_pct_points": contrib,
           "new_issuers": len(new), "new_in_top30_mean": float(z["new_in_top30"].mean()), "checks": checks,
           "counterfactuals": cf,
           "pass": all(checks.values()), "elapsed_s": round(time.time() - t0)}
    return out, rf, r1, z


def report(out: dict, rf, z: pd.DataFrame) -> str:
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s53_fpi_backtest_{stamp}")
    with open(path + ".json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    z.to_csv(path + "_monthly.csv")
    rf.nav.rename(SIGNAL).to_frame().to_csv(path + "_nav.csv")
    v1, fp, c = out["v1"], out["fpi"], out["checks"]
    yes = lambda b: "过" if b else "**不过**"
    L = [f"# S53 — composite_long_fpi backtest ({stamp})", "",
         f"{out['start']} → {out['end']}. v1 rules on a universe widened to foreign issuers (fpi.db). Same data snapshot "
         "for both lines. Survivors only for the new issuers (S53 补充 2): passing is necessary, not sufficient.", "",
         "| | CAGR | alpha2/yr | t | excess vs SPY | MaxDD | Sharpe | trades | turnover/yr |", "|---|---|---|---|---|---|---|---|---|"]
    for lab, m, k in (("v1 (S53a data)", v1, "v1"), ("composite_long_fpi", fp, "fpi")):
        L.append(f"| {lab} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.2f}% | {m['alpha2_t_nw']:.2f} | {m['excess_cagr_pct']:+.1f}% | "
                 f"{m['max_drawdown_pct']:.0f}% | {m['sharpe']:.2f} | {m['n_trades']} | {out['turnover_annual'][k]:.2f} |")
    L += ["", "Checks (pre-registered):",
          f"- (a) alpha2 not lower than v1 by more than 1%/yr: {yes(c['a_alpha'])}",
          f"- (b) turnover <= 1.15 x v1: {yes(c['b_turnover'])}",
          f"- (c) max drawdown not deeper by more than 3 points: {yes(c['c_drawdown'])}",
          f"- (d) family z-scores of foreign vs US names: {yes(c['d_families'])} — "
          + ", ".join(f"{k} mean diff {v['mean_diff']:+.2f} sd ratio {v['sd_ratio']:.2f}" for k, v in out["families"].items()),
          f"- (d) foreign share of the top 30 {out['foreign_share']['top30']:.1%} vs of the scored universe "
          f"{out['foreign_share']['scored']:.1%} (<= 2x): {yes(c['d_top30_share'])}",
          "", f"**All four: {'pass' if out['pass'] else 'fail'}.**", "",
          "Counterfactuals (same scored lists, names removed, same engine): "
          + "; ".join(f"{k.replace('_', ' ')} alpha2 {v['alpha2_ann_pct']:+.2f}% (t {v['alpha2_t_nw']:.2f}), MaxDD {v['max_drawdown_pct']:.0f}%"
                      for k, v in out["counterfactuals"].items()) + ".",
          f"Raw trade returns (not relative to SPY, and not net of what the slot would have earned otherwise; sum / {TOP_N} slots, "
          f"percentage points): foreign issuers {out['contribution_pct_points']['foreign']:+.1f}, of which the "
          f"{out['new_issuers']} new issuers {out['contribution_pct_points']['new_issuers']:+.1f}, all trades "
          f"{out['contribution_pct_points']['all']:+.1f}. New issuers in the top 30 per month end: {out['new_in_top30_mean']:.2f}.", "",
          "By year (total return)", "", "| | " + " | ".join(str(y) for y in fp["by_year"]) + " |", "|---|" + "---|" * len(fp["by_year"])]
    for lab, m in (("v1", v1), ("fpi", fp)):
        L.append(f"| {lab} | " + " | ".join(f"{m['by_year'].get(y, float('nan')) * 100:+.0f}%" for y in fp["by_year"]) + " |")
    text = "\n".join(L) + "\n"
    with open(path + ".md", "w") as f:
        f.write(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backtest"])
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    a = ap.parse_args(argv)
    out, rf, r1, z = backtest(a.start, a.end)
    print(report(out, rf, z))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
