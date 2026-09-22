"""S21 — is there anything left in an initial 13D after the market has seen it?

Two views, both on the wide point-in-time panel with delisted names:
  1. Event study: abnormal return (vs SPY) from the next open after the filing
     over 1/5/10/20 sessions, clustered by filing date (Newey-West t + block
     bootstrap), by ADV bucket, by year, first vs second half.
  2. Book: the engine run as the line would trade it — equal-dollar slots,
     next-open entry, half a quoted spread each side, hold 5/10/20 — against
     the insider line on the same window, and against SPY.

Pre-registered: universe ADV $3M–$500M, hold 10 goes live if it passes;
5 and 20 are variants counted in the Holm budget.

Usage: python -m agent.s21_13d [--start 2017-01-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from agent.books.data import load_market
from agent.books.engine import simulate
from agent.events import LINES
from agent.s8_insider import event_stats
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
HORIZONS = (1, 5, 10, 20)
HOLDS = (5, 10, 20)


def forward_from_open(market, h: int) -> pd.DataFrame:
    """Return from the open after date d to the close h sessions later, in %."""
    return (market.adj.shift(-h) / market.adj_open.shift(-1) - 1) * 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    line = LINES["sch13d"]
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        ev = line.events(store, args.start, args.end)
        ins = LINES["insider_buy"].events(store, args.start, args.end)
    tradable = market.tradable(line.spec.adv_floor, line.spec.adv_ceiling)
    ev = ev[ev["ticker"].isin(tradable.columns)]
    ev["tradable"] = [bool(tradable.at[d, t]) if d in tradable.index else False for d, t in zip(ev["date"], ev["ticker"])]
    ev_ok = ev[ev["tradable"]].copy()
    ev_ok["bucket"] = [market.bucket(t, d) for d, t in zip(ev_ok["date"], ev_ok["ticker"])]
    print(f"13D events {len(ev)} with bars, {len(ev_ok)} tradable; buckets {ev_ok['bucket'].value_counts().to_dict()}", flush=True)

    spy = market.spy
    stats = {}
    for h in HORIZONS:
        fwd = forward_from_open(market, h)
        mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
        stats[f"all_h{h}"] = event_stats(ev_ok, fwd, mkt, h, f"13D all h{h}")
        for b, g in ev_ok.groupby("bucket"):
            stats[f"{b}_h{h}"] = event_stats(g, fwd, mkt, h, f"13D {b} h{h}")
        stats[f"group_h{h}"] = event_stats(ev_ok[ev_ok["strength"] >= 3], fwd, mkt, h, f"13D group filing h{h}")
    # placebo: same tickers, dates shifted 60 sessions later (no event there)
    sh = ev_ok.copy()
    idx = market.adj.index
    pos = idx.searchsorted(sh["date"].to_numpy())
    sh["date"] = [idx[min(p + 60, len(idx) - 1)] for p in pos]
    fwd10 = forward_from_open(market, 10)
    stats["placebo_shift60_h10"] = event_stats(sh, fwd10, (spy.shift(-10) / spy.shift(-1) - 1) * 100, 10, "placebo +60d h10")

    books = {}
    tg = line.targets(market, ev, args.start, args.end)
    for hold in HOLDS:
        books[f"sch13d_h{hold}"] = simulate(market, tg, args.start, args.end, line.spec.max_slots, hold, args.exec_frac,
                                            cash_in_spy=True).metrics
    ins_line = LINES["insider_buy"]
    books["insider_5d"] = simulate(market, ins_line.targets(market, ins, args.start, args.end), args.start, args.end,
                                   ins_line.spec.max_slots, ins_line.spec.hold_days, args.exec_frac, cash_in_spy=True).metrics

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "line": line.spec.__dict__,
              "n_events": int(len(ev_ok)), "event_stats": stats, "books": books}
    with open(os.path.join(OUT_DIR, f"s21_13d_{stamp}.json"), "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S21 — initial 13D filings as a short-term event line ({stamp})", "",
         f"{args.start} → {args.end}, {len(ev_ok)} tradable events (ADV ${line.spec.adv_floor/1e6:.0f}M–${line.spec.adv_ceiling/1e6:.0f}M), "
         "entry at the open after the filing date, abnormal = minus SPY, clustered by filing date.", "",
         "| cut | h | events | dates | mean abn % | median | NW t | boot CI95 | hit | years>0 | 1st half | 2nd half |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        if "mean_abn_pct" not in s:
            L.append(f"| {s['label']} | — | — | {s['n_dates']} | too few dates | | | | | | | |")
            continue
        L.append(f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['median_abn_pct']:+.2f} | "
                 f"{s['t_nw']:.2f} | [{s['boot_ci95'][0]:+.2f}, {s['boot_ci95'][1]:+.2f}] | {s['hit_rate']:.0%} | "
                 f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    L += ["", "## As a book (equal slots, half spread per side, idle in SPY)", "",
          "| book | CAGR | SPY | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | avg trade % |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, m in books.items():
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['spy_cagr_pct']:+.1f}% | {m.get('alpha2_ann_pct', float('nan')):+.1f}% | "
                 f"{m.get('alpha2_t_nw', float('nan')):.2f} | {m.get('beta_mkt', float('nan')):.2f} | {m.get('beta_size', float('nan')):.2f} | "
                 f"{m['max_drawdown_pct']:.0f}% | {m['n_trades']} | {m['trade_hit_rate']:.0%} | {m['avg_trade_ret_pct']:+.2f} |")
    with open(os.path.join(OUT_DIR, f"s21_13d_{stamp}.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
