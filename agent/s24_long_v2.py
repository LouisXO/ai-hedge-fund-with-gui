"""S24 runner: the long-book construction variants, one report.

Usage:
  python -m agent.s24_long_v2 --variants base,n50,invvol,stop20,momcrash,issuance
  python -m agent.s24_long_v2 --variants secneutral,combo        # after agent.sources.sec_sic
Each call appends to site-data/validation/s24_long_v2_<date>.json and rewrites the .md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import pandas as pd

from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.books import long_v2
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
VARIANTS = {
    #  name        top_n  scoring kwargs                           sizing     stop
    "base":       (30,   {},                                       None,      None),
    "n50":        (50,   {},                                       None,      None),
    "invvol":     (30,   {},                                       "invvol",  None),
    "stop20":     (30,   {},                                       None,      20.0),
    "momcrash":   (30,   {"momcrash": True},                       None,      None),
    "issuance":   (30,   {"issuance": True},                       None,      None),
    "secneutral": (30,   {"groups": "ff12"},                       None,      None),
    "combo":      (50,   {"groups": "ff12"},                       "invvol",  None),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="base,n50,invvol,stop20,momcrash,issuance")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    names = args.variants.split(",")
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s24_long_v2_{stamp}.json")
    report = json.load(open(path)) if os.path.exists(path) else {"books": {}, "start": args.start, "end": args.end}

    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        groups = None
        if any(VARIANTS[v][1].get("groups") for v in names):
            from agent.books.industry import industry_by_ticker
            groups = industry_by_ticker(store)
    print(f"market {market.adj.shape}, fundamentals {len(fund)} rows", flush=True)

    score_cache: dict[str, dict] = {}
    for v in names:
        top_n, skw, sizing, stop = VARIANTS[v]
        skey = json.dumps({k: (True if k == "groups" else val) for k, val in skw.items()}, sort_keys=True)
        t0 = time.time()
        if skey not in score_cache:
            kw = dict(skw)
            if kw.get("groups"):
                kw["groups"] = groups
            score_cache[skey] = long_v2.daily_scores(market, fund, args.start, args.end, **kw)
            print(f"  scored {skey} in {time.time() - t0:.0f}s", flush=True)
        tg = long_v2.targets_from_scores(score_cache[skey], top_n)
        sizes = long_v2.invvol_sizes(market, tg, top_n) if sizing == "invvol" else None
        res = simulate(market, tg, args.start, args.end, top_n, None, args.exec_frac, cash_in_spy=True,
                       sizes=sizes, stop_pct=stop)
        m = res.metrics
        # concentration: share of the total trade P&L (in %) from the best 5 trades
        rets = sorted((t.ret_pct for t in res.trades), reverse=True)
        pos = sum(r for r in rets if r > 0)
        m["top5_share_of_gains"] = (sum(rets[:5]) / pos) if pos else float("nan")
        report["books"][v] = m
        print(f"  {v}: CAGR {m['cagr_pct']:+.1f}% alpha2 {m['alpha2_ann_pct']:+.1f}% (t {m['alpha2_t_nw']:.2f}) "
              f"β {m['beta_mkt']:.2f}/{m['beta_size']:.2f} MaxDD {m['max_drawdown_pct']:.0f}% turnover {m['turnover_ann']:.1f} "
              f"[{time.time() - t0:.0f}s]", flush=True)
        with open(path, "w") as f:
            json.dump(report, f, indent=1, default=float)

    L = [f"# S24 — long book construction variants ({stamp})", "",
         f"{args.start} → {args.end}, half spread per side, next-open entry, idle in SPY. Each variant changes one thing vs v1 (base).", "",
         "| variant | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | Sharpe | MaxDD | turnover | trades | hit | top-5 share | 2018 | 2022 | 2026 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, m in report["books"].items():
        by = m.get("by_year", {})
        L.append(f"| {v} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | "
                 f"{m['beta_mkt']:.2f} | {m['beta_size']:.2f} | {m['sharpe']:.2f} | {m['max_drawdown_pct']:.0f}% | {m['turnover_ann']:.1f} | "
                 f"{m['n_trades']} | {m['trade_hit_rate']:.0%} | {m.get('top5_share_of_gains', float('nan')):.0%} | "
                 f"{by.get('2018', by.get(2018, float('nan'))) * 100:+.1f}% | {by.get('2022', by.get(2022, float('nan'))) * 100:+.1f}% | {by.get('2026', by.get(2026, float('nan'))) * 100:+.1f}% |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
