"""S27b — could the two books really buy at the open? Auction capacity from the official opening cross.

The paper simulator cannot answer this (it fills against the NBBO after the open and does not
simulate the auction, S36). Alpaca's free historical auctions give the primary exchange's
official opening cross price and size for every name and day. For every trade both books made
in the last 12 months of the backtest (2025-09-01 → 2026-08-31), at the paper sizes
(long $2,000 per slot, insider $1,500):

  participation   our $ / (cross size x cross price) on the entry day and on the exit day
  open_vs_bar     cross price vs the daily bar's open the backtest used (is the model open the cross?)

Pre-registered reading (2026-09-24, before the run):
  - if the median entry participation is <= 5% and <= 10% of trades exceed 25%, the books can
    trade at the open with a real account at these sizes; the backtest's open is achievable.
  - otherwise the high-participation trades are reported with their average return, and a
    capacity rule (skip entries above a participation cap) is pre-registered as a later test,
    not applied now.

Usage: python -m agent.s27b_auction_capacity [--start 2025-09-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent.books import long_v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.events import LINES
from agent.sources.alpaca_auctions import AUCTIONS_DB, load as load_auctions
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
SLOT = {"long": 2000.0, "insider": 1500.0}


def trades(start: str, end: str) -> tuple[pd.DataFrame, object]:
    warm = (pd.Timestamp(start) - pd.Timedelta(days=30)).date().isoformat()
    with PanelStore(read_only=True) as store:
        market = load_market(store, warm)
        fund = fundamentals(store)
        ins = LINES["insider_buy"]
        ev = ins.events(store, warm, end)
    sc = long_v2.daily_scores(market, fund, warm, end)
    rl = simulate(market, long_v2.targets_from_scores(sc, TOP_N), warm, end, TOP_N, None, 0.5, cash_in_spy=True)
    ri = simulate(market, ins.targets(market, ev, warm, end), warm, end, ins.spec.max_slots, ins.spec.hold_days, 0.5, cash_in_spy=True)
    rows = [{"book": "long", "ticker": t.ticker, "entry": t.entry_day.date(), "exit": t.exit_day.date(), "ret": t.ret_pct} for t in rl.trades]
    rows += [{"book": "insider", "ticker": t.ticker, "entry": t.entry_day.date(), "exit": t.exit_day.date(), "ret": t.ret_pct} for t in ri.trades]
    df = pd.DataFrame(rows)
    df = df[(df["entry"] >= pd.Timestamp(start).date()) & (df["entry"] <= pd.Timestamp(end).date())]
    return df, market


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    tr, market = trades(args.start, args.end)
    print(f"{len(tr)} trades ({tr['book'].value_counts().to_dict()}), {tr['ticker'].nunique()} names", flush=True)
    n = load_auctions(sorted(tr["ticker"].unique()), pd.Timestamp(args.start).date(), pd.Timestamp(args.end).date() + dt.timedelta(days=40))
    print(f"auction rows loaded {n}", flush=True)
    con = duckdb.connect(str(AUCTIONS_DB), read_only=True)
    au = con.execute("SELECT ticker, day, open_px, open_size FROM auctions").df()
    con.close()
    au["day"] = pd.to_datetime(au["day"]).dt.date
    key = au.set_index(["ticker", "day"])
    raw_open = market.adj_open * (market.close / market.adj)          # unadjusted open, the bar the model used

    def look(t, d):
        if (t, d) not in key.index:
            return np.nan, np.nan
        r = key.loc[(t, d)]
        return float(r["open_px"]) if pd.notna(r["open_px"]) else np.nan, float(r["open_size"]) if pd.notna(r["open_size"]) else np.nan

    out = []
    for r in tr.itertuples(index=False):
        epx, esz = look(r.ticker, r.entry)
        xpx, xsz = look(r.ticker, r.exit)
        bar = raw_open.at[pd.Timestamp(r.entry), r.ticker] if pd.Timestamp(r.entry) in raw_open.index and r.ticker in raw_open.columns else np.nan
        slot = SLOT[r.book]
        out.append({**r._asdict(), "entry_cross_usd": epx * esz, "exit_cross_usd": xpx * xsz,
                    "entry_part": slot / (epx * esz) * 100 if epx and esz else np.nan,
                    "exit_part": slot / (xpx * xsz) * 100 if xpx and xsz else np.nan,
                    "open_vs_bar": (epx / bar - 1) * 100 if epx and bar and bar > 0 else np.nan})
    df = pd.DataFrame(out)
    res = {}
    for book, g in df.groupby("book"):
        p = g["entry_part"].dropna()
        x = g["exit_part"].dropna()
        hi = g[g["entry_part"] > 25]
        lo = g[g["entry_part"] <= 25]
        res[book] = {"trades": int(len(g)), "with_cross": int(len(p)),
                     "entry_part_median": float(p.median()), "entry_part_p75": float(p.quantile(.75)), "entry_part_p90": float(p.quantile(.9)),
                     "share_entry_gt10": float((p > 10).mean()), "share_entry_gt25": float((p > 25).mean()), "share_entry_gt100": float((p > 100).mean()),
                     "exit_part_median": float(x.median()), "share_exit_gt25": float((x > 25).mean()),
                     "cross_usd_median": float(g["entry_cross_usd"].median()),
                     "open_vs_bar_median_abs": float(g["open_vs_bar"].abs().median()), "open_vs_bar_share_gt_0.5": float((g["open_vs_bar"].abs() > 0.5).mean()),
                     "ret_high_part": float(hi["ret"].mean()) if len(hi) else None, "n_high_part": int(len(hi)),
                     "ret_low_part": float(lo["ret"].mean()) if len(lo) else None, "n_low_part": int(len(lo)),
                     "pass": bool(p.median() <= 5 and (p > 25).mean() <= 0.10)}
    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s27b_auction_capacity_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"start": args.start, "end": args.end, "slot_usd": SLOT, "books": res}, f, indent=1, default=float)
    L = [f"# S27b — opening-auction capacity at paper sizes ({stamp})", "",
         f"Trades both books made {args.start} → {args.end} in the backtest; primary-exchange opening cross (Alpaca auctions, the \"O\" print). "
         "Participation = our slot $ / cross $ (long $2,000, insider $1,500).", "",
         "| book | trades | with cross | median cross $ | entry part. median | p75 | p90 | >10% | >25% | >100% | exit part. median | exit >25% | cross vs bar open, median |Δ| | avg ret, part ≤25% | avg ret, part >25% | pass |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b, r in res.items():
        L.append(f"| {b} | {r['trades']} | {r['with_cross']} | ${r['cross_usd_median']:,.0f} | {r['entry_part_median']:.1f}% | {r['entry_part_p75']:.1f}% | {r['entry_part_p90']:.0f}% | "
                 f"{r['share_entry_gt10']:.0%} | {r['share_entry_gt25']:.0%} | {r['share_entry_gt100']:.0%} | {r['exit_part_median']:.1f}% | {r['share_exit_gt25']:.0%} | "
                 f"{r['open_vs_bar_median_abs']:.2f}% | {r['ret_low_part']:+.2f}% (n {r['n_low_part']}) | "
                 + (f"{r['ret_high_part']:+.2f}% (n {r['n_high_part']})" if r['ret_high_part'] is not None else "—") + f" | {'✓' if r['pass'] else 'no'} |")
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
