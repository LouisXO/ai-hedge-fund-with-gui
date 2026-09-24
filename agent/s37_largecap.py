"""S37 — what selects large caps? Pre-registered 2026-09-24, before any run.

The live long book (v1) holds a $1.7B median name: equal-weight family z-scores reward one
extreme family, and extremes live in small caps (2026-09-24 reading of the top 30: 27 of 30
below $10B). The user wants both sizes. This is the research step for a separate large-cap
book; the live book is not touched before its evaluation point.

Universe  point-in-time market cap >= $10B on the scoring day (shares from the latest filing
          x that day's close, the same mcap factor_scores uses), listed, ADV >= $5M.
          ~350 names in 2017, ~600 in 2026. No S&P membership (index inclusion is itself a
          selection); no survivorship (the panel is point-in-time).
Books     top 30 / keep 60, equal weight, next-open entry, half spread per side, idle in SPY —
          exactly v1's rule, only the universe and the score change:
  lc_composite   v1's four-family winsor-z composite, z-scored within large caps   (control)
  lc_rankz       the same with rank-normal scores (S29 showed winsor pile-up drives v1;
                 extremes are rarer in large caps, so the rank version may behave differently)
  lc_qlv         quality + low volatility only (defensive / QMJ; the families large caps score
                 well on)
  lc_qv          quality + value ("quality at a reasonable price")
  lc_mom         momentum only (12-1)
  lc_value       value only (book/market + earnings yield)
  lc_payout      composite with net share issuance added to quality (buybacks score high:
                 the one factor the literature finds robust in large caps post-2000)
  lc_secneutral  composite z-scored within FF12 industry (S24 killed alpha in the full
                 universe; large-cap sector bets are bigger, so it is re-tested here)
  lc_composite50 composite, top 50 / keep 100 (more names, less idiosyncratic risk)
  lc_ew          baseline: every large-cap name, equal weight (what "large-cap equal weight"
                 earns with no selection; RSP-like)
Reading (fixed now): a rule is worth a paper book only if, 2017-01 → 2026-08,
  (a) alpha2 (SPY + size) >= +3%/yr with NW t >= 2.0,
  (b) it beats lc_ew's alpha2 by >= 2%/yr (selection must add to the equal-weight effect), and
  (c) beta_size <= 0.3 (it is actually a large-cap book).
Anything else: recorded, not adopted. Holm budget +9 (lc_ew is a baseline, not a candidate).
If several pass, the one with the highest alpha2 t goes to paper as a THIRD book (shadow first),
funded from the unused $10k option reserve; the user decides the allocation.

Usage: python -m agent.s37_largecap [--start 2017-01-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import numpy as np
import pandas as pd

from agent.books.data import Market, fundamentals, load_market
from agent.books.engine import simulate
from agent.books.factors import _rank_z, _z, factor_scores, latest_before
from agent.books.long_term import ADV_FLOOR
from agent.books.long_v2 import targets_from_scores
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
MCAP_FLOOR = 10e9
FAMS = ["value", "quality", "momentum", "lowvol"]


def large_universe(market: Market, fund: pd.DataFrame, day: pd.Timestamp) -> pd.Index:
    tradable = market.tradable(ADV_FLOOR, np.inf).loc[day]
    names = tradable[tradable].index
    f = latest_before(fund, day).reindex(names)
    mcap = market.close.loc[day].reindex(names) * f["shares"]
    return mcap[mcap >= MCAP_FLOOR].index


def score_all(market: Market, fund: pd.DataFrame, start: str, end: str, groups: pd.Series | None) -> tuple[dict, dict]:
    """day -> {variant: composite Series} for every variant except lc_ew, plus the day's universe."""
    days = market.adj.loc[start:end].index
    scores: dict[str, dict] = {k: {} for k in ("lc_composite", "lc_rankz", "lc_qlv", "lc_qv", "lc_mom", "lc_value", "lc_payout", "lc_secneutral")}
    universe: dict = {}
    t0 = time.time()
    for i, d in enumerate(days):
        u = large_universe(market, fund, d)
        universe[d] = list(u)
        if len(u) < 50:
            continue
        base = factor_scores(market, fund, d, u)
        base = base[base["n_families"] >= 3]
        rz = factor_scores(market, fund, d, u, norm="rank")
        rz = rz[rz["n_families"] >= 3]
        pay = factor_scores(market, fund, d, u, issuance=True)
        pay = pay[pay["n_families"] >= 3]
        sn = factor_scores(market, fund, d, u, groups=groups) if groups is not None else base
        sn = sn[sn["n_families"] >= 3]
        scores["lc_composite"][d] = base["composite"].dropna().sort_values(ascending=False)
        scores["lc_rankz"][d] = rz["composite"].dropna().sort_values(ascending=False)
        scores["lc_qlv"][d] = base[["quality", "lowvol"]].mean(axis=1).dropna().sort_values(ascending=False)
        scores["lc_qv"][d] = base[["quality", "value"]].mean(axis=1).dropna().sort_values(ascending=False)
        scores["lc_mom"][d] = base["momentum"].dropna().sort_values(ascending=False)
        scores["lc_value"][d] = base["value"].dropna().sort_values(ascending=False)
        scores["lc_payout"][d] = pay["composite"].dropna().sort_values(ascending=False)
        scores["lc_secneutral"][d] = sn["composite"].dropna().sort_values(ascending=False)
        if i % 250 == 0:
            print(f"  {d.date()} universe {len(u)} [{time.time() - t0:.0f}s]", flush=True)
    return scores, universe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        try:
            from agent.books.industry import industry_by_ticker
            groups = industry_by_ticker(store)
        except Exception as exc:
            print(f"no industry groups ({exc}); lc_secneutral = lc_composite")
            groups = None
    print(f"market {market.adj.shape}, fundamentals {len(fund)} [{time.time() - t0:.0f}s]", flush=True)
    scores, universe = score_all(market, fund, args.start, args.end, groups)
    sizes = pd.Series({d: len(u) for d, u in universe.items()})
    print(f"scored; universe size min {sizes.min()} median {sizes.median():.0f} max {sizes.max()} [{time.time() - t0:.0f}s]", flush=True)

    books = {}
    for name, sc in scores.items():
        tg = targets_from_scores(sc, 30)
        books[name] = simulate(market, tg, args.start, args.end, 30, None, args.exec_frac, cash_in_spy=True).metrics
        print(f"  {name}: alpha2 {books[name]['alpha2_ann_pct']:+.1f}% (t {books[name]['alpha2_t_nw']:.2f}) β_size {books[name]['beta_size']:.2f} CAGR {books[name]['cagr_pct']:+.1f}% [{time.time() - t0:.0f}s]", flush=True)
    tg50 = targets_from_scores(scores["lc_composite"], 50)
    books["lc_composite50"] = simulate(market, tg50, args.start, args.end, 50, None, args.exec_frac, cash_in_spy=True).metrics
    # baseline: the whole universe, equal weight (rebalanced by the engine's slot rule as names enter/leave)
    cap = int(sizes.max()) + 5
    books["lc_ew"] = simulate(market, {d: u for d, u in universe.items() if u}, args.start, args.end, cap, None, args.exec_frac, cash_in_spy=True).metrics
    print(f"  lc_ew: alpha2 {books['lc_ew']['alpha2_ann_pct']:+.1f}% (t {books['lc_ew']['alpha2_t_nw']:.2f}) [{time.time() - t0:.0f}s]", flush=True)

    ew = books["lc_ew"]["alpha2_ann_pct"]
    verdict = {}
    for k, m in books.items():
        if k == "lc_ew":
            continue
        verdict[k] = {"alpha_ok": m["alpha2_ann_pct"] >= 3.0 and m["alpha2_t_nw"] >= 2.0, "beats_ew": m["alpha2_ann_pct"] - ew >= 2.0,
                      "large_cap": m["beta_size"] <= 0.3}
        verdict[k]["adopt"] = all(verdict[k].values())

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"s37_largecap_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end, "mcap_floor": MCAP_FLOOR,
                   "universe_size": {"min": int(sizes.min()), "median": float(sizes.median()), "max": int(sizes.max())}, "books": books, "verdict": verdict}, f, indent=1, default=float)
    L = [f"# S37 — large-cap book candidates ({stamp})", "",
         f"Universe: point-in-time market cap >= $10B (size {sizes.min()}–{sizes.max()}, median {sizes.median():.0f}), {args.start} → {args.end}, "
         "top 30 / keep 60 equal weight (lc_composite50: 50/100), next-open entry, half spread per side, idle in SPY. lc_ew = every name equal weight (baseline).", "",
         "| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | Sharpe | MaxDD | turnover | trades | hit | 2018 | 2022 | 2026 |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, m in books.items():
        by = m.get("by_year", {})
        g = lambda y: by.get(str(y), by.get(y, float("nan"))) * 100
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['beta_mkt']:.2f} | {m['beta_size']:.2f} | "
                 f"{m['sharpe']:.2f} | {m['max_drawdown_pct']:.0f}% | {m['turnover_ann']:.1f} | {m['n_trades']} | {m['trade_hit_rate']:.0%} | {g(2018):+.1f}% | {g(2022):+.1f}% | {g(2026):+.1f}% |")
    L += ["", "## Verdict (pre-registered: alpha2 >= 3%/yr with t >= 2, beats lc_ew by >= 2%/yr, beta_size <= 0.3)", "",
          "| book | alpha ok | beats EW | large-cap | adopt |", "|---|---|---|---|---|"]
    for k, v in verdict.items():
        L.append(f"| {k} | {'✓' if v['alpha_ok'] else ''} | {'✓' if v['beats_ew'] else ''} | {'✓' if v['large_cap'] else ''} | {'**ADOPT**' if v['adopt'] else 'no'} |")
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
