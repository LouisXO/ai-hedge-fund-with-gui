"""S30 runner: the explicit momentum book vs v1, full period and 2026 YTD."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import pandas as pd

from agent.books import long_v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.books.momentum_book import momentum_lists
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
VARIANTS = ("mom_top", "mom_top_momcrash", "mom_top_lowvol")


def run(market, fund, start, end, exec_frac=0.5):
    out = {}
    sc = long_v2.daily_scores(market, fund, start, end)
    out["v1_base"] = simulate(market, long_v2.targets_from_scores(sc, TOP_N), start, end, TOP_N, None, exec_frac, cash_in_spy=True)
    for v in VARIANTS:
        tg, sz = momentum_lists(market, start, end, v)
        out[v] = simulate(market, tg, start, end, TOP_N, None, exec_frac, cash_in_spy=True, sizes=sz or None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
    full = run(market, fund, args.start, args.end)
    with PanelStore(read_only=True) as store:
        m26 = load_market(store, "2026-01-02")
    ytd = run(m26, fund, "2026-01-02", "2026-09-18")
    stamp = dt.date.today().isoformat()
    rep = {"full": {k: v.metrics for k, v in full.items()}, "ytd_2026": {k: v.metrics for k, v in ytd.items()}}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"s30_momentum_book_{stamp}.json"), "w") as f:
        json.dump(rep, f, indent=1, default=float)
    L = [f"# S30 — the explicit momentum book vs v1 ({stamp})", "",
         f"{args.start} → {args.end}; half spread per side, next-open entry, 30/60 slot rule, idle in SPY. "
         "Momentum book: ADV >= $5M, close >= $2, no fundamentals.", "",
         "| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | Sharpe | MaxDD | turnover | trades | hit | 2018 | 2020 | 2022 | 2026 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, r in full.items():
        m, by = r.metrics, r.metrics["by_year"]
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['beta_mkt']:.2f} | "
                 f"{m['beta_size']:.2f} | {m['sharpe']:.2f} | {m['max_drawdown_pct']:.0f}% | {m['turnover_ann']:.1f} | {m['n_trades']} | {m['trade_hit_rate']:.0%} | "
                 + " | ".join(f"{by.get(y, float('nan')) * 100:+.1f}%" for y in (2018, 2020, 2022, 2026)) + " |")
    L += ["", "## 2026 YTD (cash start 01-02 → 09-18)", "", "| book | YTD | MaxDD | β | Jul |", "|---|---|---|---|---|"]
    for k, r in ytd.items():
        nav = r.nav
        mo = nav.resample("ME").last().pct_change() * 100
        jul = float(mo[mo.index.month == 7].iloc[0]) if (mo.index.month == 7).any() else float("nan")
        L.append(f"| {k} | {(nav.iloc[-1] / nav.iloc[0] - 1) * 100:+.1f}% | {r.metrics['max_drawdown_pct']:.1f}% | {r.metrics['beta']:.2f} | {jul:+.1f}% |")
    text = "\n".join(L) + "\n"
    with open(os.path.join(OUT_DIR, f"s30_momentum_book_{stamp}.md"), "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
