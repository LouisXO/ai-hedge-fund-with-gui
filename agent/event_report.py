"""One report for any event line: event study + the line run as a book.

  1. Event study: abnormal return (vs SPY) from the open after the event over
     1/5/10/20/40 sessions, clustered by event date (Newey-West t, block
     bootstrap), overall / by ADV bucket / by year halves, plus a placebo with
     the same names 60 sessions later.
  2. Book: the engine as the line would trade — equal slots, next-open entry,
     half a quoted spread each side, idle in SPY — for each holding period,
     next to the insider line on the same window and SPY.

Usage: python -m agent.event_report --line pead_small --tag s22 [--holds 10,20,40] [--start 2017-01-01]
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
HORIZONS = (1, 5, 10, 20, 40)


def forward_from_open(market, h: int) -> pd.DataFrame:
    return (market.adj.shift(-h) / market.adj_open.shift(-1) - 1) * 100


def run(line_name: str, tag: str, start: str, end: str, holds: tuple[int, ...], exec_frac: float,
        extra_lines: dict | None = None) -> str:
    line = LINES[line_name]
    with PanelStore(read_only=True) as store:
        market = load_market(store, start)
        ev = line.events(store, start, end)
        ins = LINES["insider_buy"].events(store, start, end)
        extra_ev = {k: v.events(store, start, end) for k, v in (extra_lines or {}).items()}
    tradable = market.tradable(line.spec.adv_floor, line.spec.adv_ceiling)
    ev = ev[ev["ticker"].isin(tradable.columns)]
    ev["tradable"] = [bool(tradable.at[d, t]) if d in tradable.index else False for d, t in zip(ev["date"], ev["ticker"])]
    ok = ev[ev["tradable"]].copy()
    ok["bucket"] = [market.bucket(t, d) for d, t in zip(ok["date"], ok["ticker"])]
    print(f"{line_name}: {len(ev)} events with bars, {len(ok)} tradable; {ok['bucket'].value_counts().to_dict()}", flush=True)

    spy = market.spy
    stats = {}
    for h in HORIZONS:
        fwd, mkt = forward_from_open(market, h), (spy.shift(-h) / spy.shift(-1) - 1) * 100
        stats[f"all_h{h}"] = event_stats(ok, fwd, mkt, h, f"{line_name} all h{h}")
        for b, g in ok.groupby("bucket"):
            stats[f"{b}_h{h}"] = event_stats(g, fwd, mkt, h, f"{line_name} {b} h{h}")
    sh = ok.copy()
    idx = market.adj.index
    pos = idx.searchsorted(sh["date"].to_numpy())
    sh["date"] = [idx[min(p + 60, len(idx) - 1)] for p in pos]
    stats["placebo_shift60_h20"] = event_stats(sh, forward_from_open(market, 20), (spy.shift(-20) / spy.shift(-1) - 1) * 100, 20, "placebo +60d h20")

    books = {}
    tg = line.targets(market, ev, start, end)
    for hold in holds:
        books[f"{line_name}_h{hold}"] = simulate(market, tg, start, end, line.spec.max_slots, hold, exec_frac, cash_in_spy=True).metrics
    for k, l in (extra_lines or {}).items():
        books[f"{k}_h{l.spec.hold_days}"] = simulate(market, l.targets(market, extra_ev[k], start, end), start, end,
                                                     l.spec.max_slots, l.spec.hold_days, exec_frac, cash_in_spy=True).metrics
    il = LINES["insider_buy"]
    books["insider_5d"] = simulate(market, il.targets(market, ins, start, end), start, end, il.spec.max_slots,
                                   il.spec.hold_days, exec_frac, cash_in_spy=True).metrics

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"{tag}_{line_name}_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "line": line.spec.__dict__,
                   "n_events": int(len(ok)), "event_stats": stats, "books": books}, f, indent=1, default=float)
    L = [f"# {tag.upper()} — {line_name} as a short-term event line ({stamp})", "",
         f"Hypothesis: {line.spec.hypothesis}.", "",
         f"{start} → {end}, {len(ok)} tradable events (ADV ${line.spec.adv_floor/1e6:.0f}M–${line.spec.adv_ceiling/1e6:.0f}M), "
         "entry at the open after the event date, abnormal = minus SPY, clustered by event date.", "",
         "| cut | h | events | dates | mean abn % | median | NW t | boot CI95 | hit | years>0 | 1st half | 2nd half |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in stats.values():
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
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as f:
        f.write(text)
    print(text)
    return base + ".md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=list(LINES))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--holds", default="10,20,40")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    run(args.line, args.tag, args.start, args.end, tuple(int(h) for h in args.holds.split(",")), args.exec_frac)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
