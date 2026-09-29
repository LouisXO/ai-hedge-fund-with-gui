"""S47 — restatement of the two live books' backtests on the corrected data (docs/AGENT_PLAN.md "### S47").

Same rules, same window and engine settings as the numbers they replace:
  long v1     2017-01-01 → 2026-08-31, strict entry zone (enter from the top 30 only, S44), half the quoted
              spread per side, next-open fills, idle cash in SPY. Replaces S44 strict base: alpha2 +8.11%/yr (t 1.46).
  insider v1  same window and costs, the event line's own slots and hold (S40 / S45 D1 base).
              Replaces alpha2 +8.38%/yr (t 1.93).
A restatement is not a new variant: results are written under "restated", which the multiple-testing ledger
(hedge_fund/validation/family_log.py reads "books") does not count. Whatever the numbers are, v1 keeps
running (S47). The long book's daily NAV is written to s47_base_nav.csv for the drift monitor.

S47b (2026-09-29): `--name s47b` restates on the S47b data (reused tickers split, the listing supplement,
moomoo share counts) against the S47 restatement, and writes s47b_restatement_<date> next to it.

Usage: python -m agent.s47_restate [--name s47b]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import pandas as pd

from agent.books import long_v2_bundle as v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.events import LINES
from agent.s44_v2_bundle import entry_zone
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2017-01-01", "2026-08-31"
BEFORE = {"long": {"alpha2_ann_pct": 8.11, "alpha2_t_nw": 1.46, "cagr_pct": 19.4, "source": "s44_v2_bundle_2026-09-26 strict base"},
          "insider": {"alpha2_ann_pct": 8.38, "alpha2_t_nw": 1.93, "cagr_pct": 20.4, "source": "S45 D1 base (2026-09-28)"}}
BEFORE_S47B = {"long": {"alpha2_ann_pct": 8.37, "alpha2_t_nw": 1.40, "cagr_pct": 19.7, "source": "S47 restatement (2026-09-29)"},
               "insider": {"alpha2_ann_pct": 8.80, "alpha2_t_nw": 2.05, "cagr_pct": 21.1, "source": "S47 restatement (2026-09-29)"}}
KEYS = ["cagr_pct", "alpha2_ann_pct", "alpha2_t_nw", "excess_cagr_pct", "active_t_nw", "beta_mkt", "beta_size", "vol_pct",
        "max_drawdown_pct", "sharpe", "n_trades", "trade_hit_rate", "avg_exposure", "by_year"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="s47", choices=["s47", "s47b"])
    name = ap.parse_args().name
    before = BEFORE_S47B if name == "s47b" else BEFORE
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, START)
        fund = fundamentals(store)
        ins = LINES["insider_buy"]
        ins_ev = ins.events(store, START, END)
    lists = {}
    for d in market.adj.loc[START:END].index:
        fs = v2.scored(market, fund, d)
        lists[d] = fs.index[:v2.KEEP_MULT * TOP_N].tolist()
    print(f"scored {len(lists)} days [{time.time() - t0:.0f}s]", flush=True)
    long_res = simulate(market, lists, START, END, TOP_N, None, 0.5, cash_in_spy=True, sizes=entry_zone(lists, TOP_N))
    ins_tg = ins.targets(market, ins_ev, START, END)
    ins_res = simulate(market, ins_tg, START, END, ins.spec.max_slots, ins.spec.hold_days, 0.5, cash_in_spy=True)
    after = {"long": {k: long_res.metrics.get(k) for k in KEYS}, "insider": {k: ins_res.metrics.get(k) for k in KEYS}}
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"{name}_restatement_{stamp}")
    long_res.nav.rename("v1c").to_frame().to_csv(os.path.join(OUT_DIR, "s47_base_nav.csv"))
    with open(path + ".json", "w") as f:
        json.dump({"start": START, "end": END, "before": before, "restated": after,
                   "n_insider_events": int(len(ins_ev))}, f, indent=1, default=float)
    L = [f"# {name.upper()} — restatement on the corrected data ({stamp})", "", f"{START} → {END}. Same rules and engine settings as the numbers replaced. "
         "Not a new variant; v1 keeps running whatever the result (S47).", "",
         "| book | | CAGR | alpha2/yr | alpha2 t | excess vs SPY | active t | β mkt | β size | vol | MaxDD | Sharpe | trades | hit |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in ("long", "insider"):
        o, m = before[b], after[b]
        L.append(f"| {b} | before | {o['cagr_pct']:+.1f}% | {o['alpha2_ann_pct']:+.2f}% | {o['alpha2_t_nw']:.2f} | | | | | | | | | |")
        L.append(f"| {b} | restated | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.2f}% | {m['alpha2_t_nw']:.2f} | {m['excess_cagr_pct']:+.1f}% | "
                 f"{m['active_t_nw']:.2f} | {m['beta_mkt']:.2f} | {m['beta_size']:.2f} | {m['vol_pct']:.0f}% | {m['max_drawdown_pct']:.0f}% | "
                 f"{m['sharpe']:.2f} | {m['n_trades']} | {m['trade_hit_rate']:.0%} |")
    L += ["", "By year (total return, restated)", "", "| book | " + " | ".join(str(y) for y in after["long"]["by_year"]) + " |",
          "|---|" + "---|" * len(after["long"]["by_year"])]
    for b in ("long", "insider"):
        L.append(f"| {b} | " + " | ".join(f"{after[b]['by_year'].get(y, float('nan')) * 100:+.0f}%" for y in after["long"]["by_year"]) + " |")
    text = "\n".join(L) + "\n"
    with open(path + ".md", "w") as f:
        f.write(text)
    print(text)
    print(f"[{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
