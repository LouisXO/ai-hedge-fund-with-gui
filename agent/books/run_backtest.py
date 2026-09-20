"""Backtest both books against SPY and write the report.

Usage:
  python -m agent.books.run_backtest [--start 2017-01-01] [--end 2026-08-31] [--exec-frac 0.5]
Writes site-data/validation/books_<date>.{json,md}.

Read the alpha line, not the total-return line: a long-only book carries
market beta, and beta is not skill.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

from agent.books import long_term, short_term
from agent.books.data import insider_flows, load_market
from agent.books.engine import simulate
from hedge_fund.features.panel import PanelStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5, help="fraction of the quoted spread paid per side")
    ap.add_argument("--cash-in-spy", action="store_true", help="idle slots hold SPY instead of cash")
    args = ap.parse_args()

    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        flows = insider_flows(store, (dt.date.fromisoformat(args.start) - dt.timedelta(days=200)).isoformat())

    runs = {}
    st = short_term.targets(market, flows, args.start, args.end)
    print(f"short-term: {sum(len(v) for v in st.values())} candidate-days", flush=True)
    runs["short_insider_5d"] = simulate(market, st, args.start, args.end, short_term.MAX_POSITIONS,
                                        short_term.HOLD_DAYS, args.exec_frac, cash_in_spy=args.cash_in_spy)
    for name, tg in long_term.scores(market, flows, args.start, args.end).items():
        print(f"long-term {name}: {len(tg)} rebalances", flush=True)
        runs[f"long_{name}"] = simulate(market, tg, args.start, args.end, long_term.TOP_N, None, args.exec_frac,
                                        cash_in_spy=args.cash_in_spy)

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "end": args.end, "exec_frac": args.exec_frac, "cash_in_spy": args.cash_in_spy,
              "books": {k: v.metrics for k, v in runs.items()}}
    tag = "_spycash" if args.cash_in_spy else ""
    path = os.path.join(OUT_DIR, f"books_{stamp}{tag}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# Books backtest — {stamp}", "",
         f"{args.start} → {args.end}, point-in-time universe, delisted names included, "
         f"{args.exec_frac:.2f} x quoted spread paid per side, entry at next open, equal weight, "
         f"idle slots in {'SPY' if args.cash_in_spy else 'cash'}.", "",
         "| book | CAGR | SPY CAGR | excess | alpha/yr | alpha t | beta | Sharpe | MaxDD | SPY MaxDD | exposure | turnover/yr | trades | hit |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, r in runs.items():
        m = r.metrics
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m['excess_cagr_pct']:+.1f}% | "
                 f"{m['alpha_ann_pct']:+.1f}% | {m['alpha_t_nw']:+.2f} | {m['beta']:.2f} | {m['sharpe']:.2f} | "
                 f"{m['max_drawdown_pct']:.1f}% | {m['spy_max_drawdown_pct']:.1f}% | {m['avg_exposure']:.0%} | "
                 f"{m['turnover_ann']:.1f} | {m['n_trades']} | "
                 f"{m['trade_hit_rate']:.0%} |" if m['n_trades'] else f"{m['turnover_ann']:.1f} | 0 | — |")
    L += ["", "## By year (book vs SPY)", "", "| year | " + " | ".join(runs) + " | SPY |",
          "|---|" + "---|" * (len(runs) + 1)]
    years = sorted(next(iter(runs.values())).metrics["by_year"])
    for y in years:
        row = [f"{runs[k].metrics['by_year'].get(y, float('nan')):+.1%}" for k in runs]
        L.append(f"| {y} | " + " | ".join(row) + f" | {next(iter(runs.values())).metrics['spy_by_year'].get(y, float('nan')):+.1%} |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
