"""S40 — a minimum share price for the insider line (and the long book). Pre-registered 2026-09-24, before any run.

Trigger: BFRG ($0.95, gapped -15% the morning we tried to buy it). Sub-$1 and sub-$2 stocks have
wide spreads, frequent reverse splits and dilution; whether they help or hurt the insider line has
never been checked.

Price = the raw close on the signal day (what we would have seen). Floors:
  floor1   skip entries priced < $1
  floor2   skip entries priced < $2
  floor5   skip entries priced < $5
Tested on the insider line (v1, 5-session hold) and the long book (v1), 2017-01 → 2026-08, as
entry vetoes (same machinery as S33).

Also reported: the insider line's event study by price bucket (< $1, $1–2, $2–5, >= $5), h5.

Adopt a floor for a book only if (a) its alpha2 improves by >= 0.5%/yr with the NW t not lower,
AND (b) the vetoed names' abnormal return over the book's horizon has NW t <= -2. The lowest floor
that passes is the one adopted. Holm budget +6.

Usage: python -m agent.s40_price_floor
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import numpy as np
import pandas as pd

from agent.books import long_v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.events import LINES
from agent.s8_insider import event_stats
from agent.s33_negative_filters import direct_test, veto_sizes
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2017-01-01", "2026-08-31"
FLOORS = {"floor1": 1.0, "floor2": 2.0, "floor5": 5.0}
HORIZON = {"long": 20, "insider": 5}


def main() -> int:
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, START)
        fund = fundamentals(store)
        ins = LINES["insider_buy"]
        ins_ev = ins.events(store, START, END)
    raw = market.close
    sc = long_v2.daily_scores(market, fund, START, END)
    long_tg = long_v2.targets_from_scores(sc, TOP_N)
    long_entry = [(d, t) for d, s in sc.items() for t in s.head(TOP_N).index]
    ins_tg = ins.targets(market, ins_ev, START, END)
    ins_pairs = [(d, t) for d, names in ins_tg.items() for t in names]
    print(f"scored [{time.time() - t0:.0f}s]", flush=True)

    books = {"long_base": simulate(market, long_tg, START, END, TOP_N, None, 0.5, cash_in_spy=True).metrics,
             "insider_base": simulate(market, ins_tg, START, END, ins.spec.max_slots, ins.spec.hold_days, 0.5, cash_in_spy=True).metrics}
    direct, verdict = {}, {}
    for name, floor in FLOORS.items():
        veto = (raw < floor).fillna(False)
        for b, tg, pairs_all, hold, slots in (("long", long_tg, long_entry, None, TOP_N), ("insider", ins_tg, ins_pairs, ins.spec.hold_days, ins.spec.max_slots)):
            sz, pairs = veto_sizes(tg, veto)
            m = simulate(market, tg, START, END, slots, hold, 0.5, cash_in_spy=True, sizes=sz).metrics
            m["n_vetoed_pairs"] = len(pairs)
            books[f"{b}_{name}"] = m
            vp = [p for p in pairs if p in set(pairs_all)]
            direct[f"{b}_{name}"] = direct_test(market, vp, pairs_all, HORIZON[b], f"{b} {name}")
            base, dv = books[f"{b}_base"], direct[f"{b}_{name}"]["vetoed"]
            a = m["alpha2_ann_pct"] - base["alpha2_ann_pct"] >= 0.5 and m["alpha2_t_nw"] >= base["alpha2_t_nw"]
            c = "t_nw" in dv and dv["t_nw"] <= -2.0
            verdict[f"{b}_{name}"] = {"book_improves": bool(a), "vetoed_negative": bool(c), "adopt": bool(a and c)}
        print(f"  {name}: long {books[f'long_{name}']['alpha2_ann_pct']:+.1f}%, insider {books[f'insider_{name}']['alpha2_ann_pct']:+.1f}% [{time.time() - t0:.0f}s]", flush=True)

    # insider event study by price bucket
    ev = pd.DataFrame(ins_pairs, columns=["date", "ticker"])
    ev["px"] = [raw.at[d, t] if d in raw.index and t in raw.columns else np.nan for d, t in ev.itertuples(index=False)]
    fwd = (market.adj.shift(-5) / market.adj_open.shift(-1) - 1) * 100
    mkt = (market.spy.shift(-5) / market.spy.shift(-1) - 1) * 100
    buckets = {"< $1": (0, 1), "$1–2": (1, 2), "$2–5": (2, 5), ">= $5": (5, 1e9)}
    study = {k: event_stats(ev[(ev["px"] >= lo) & (ev["px"] < hi)][["date", "ticker"]], fwd, mkt, 5, f"insider {k} h5") for k, (lo, hi) in buckets.items()}

    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s40_price_floor_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"start": START, "end": END, "floors": FLOORS, "books": books, "direct": direct, "verdict": verdict, "by_price": study}, f, indent=1, default=float)

    def row(s):
        if "mean_abn_pct" not in s:
            return f"| {s['label']} | — | — | {s['n_dates']} | too few | | | | |"
        return (f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['t_nw']:.2f} | "
                f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    L = [f"# S40 — minimum share price ({stamp})", "", f"{START} → {END}. Price = raw close on the signal day. Vetoes block entries only.", "",
         "## Insider line by price bucket (h5, abnormal vs SPY from the next open)", "",
         "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    L += [row(s) for s in study.values()]
    L += ["", "## Books", "", "| book | CAGR | alpha2/yr | alpha2 t | trades | vetoed pairs |", "|---|---|---|---|---|---|"]
    for k, m in books.items():
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['n_trades']} | {m.get('n_vetoed_pairs', '')} |")
    L += ["", "## Vetoed vs kept", "", "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    for d in direct.values():
        L += [row(d["vetoed"]), row(d["kept"])]
    L += ["", "## Verdict", "", "| variant | book improves | vetoed negative | adopt |", "|---|---|---|---|"]
    for k, v in verdict.items():
        L.append(f"| {k} | {'✓' if v['book_improves'] else ''} | {'✓' if v['vetoed_negative'] else ''} | {'**ADOPT**' if v['adopt'] else 'no'} |")
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
