"""S32 — "unusual options activity" from our own data (what Unusual Whales sells as alerts).

Alpaca's daily option bars (options.db, S&P names, monthlies within 15% of
spot, 2024-01 →) give total traded contracts per underlying per day, split
by call / put. An "unusual" day is defined before looking at returns:

  volume ratio   today's contracts / trailing 20-session mean (>= 3x)
  direction      call share of that volume >= 70% (bullish flow) or <= 30% (bearish)
  outcome        underlying's abnormal return (vs SPY) from the next open over
                 1 / 5 / 10 / 20 sessions; also the same day's close-to-close
                 (the part an alert subscriber cannot trade, for reference)

Reading, pre-registered 2026-09-23: worth a line if bullish-flow days show
mean 5-session abnormal return >= +0.30% with NW t >= 2 and the sign holds in
2024, 2025 and 2026; bearish flow is reported as a factor (no shorting).
Caveat: this is monthlies near the money only (no weeklies, no far OTM), so
it undercounts the lottery-ticket flow that alert services highlight.

Usage: python -m agent.s32_unusual_options [--ratio 3] [--share 0.7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent.books.data import load_market
from agent.s8_insider import event_stats
from agent.sources.alpaca_options import OPTIONS_DB
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ratio", type=float, default=3.0)
    ap.add_argument("--share", type=float, default=0.7)
    ap.add_argument("--start", default="2024-02-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    vol = con.execute("""SELECT c.underlying, b.trade_date, sum(b.volume) AS v,
                                sum(CASE WHEN c.cp = 'C' THEN b.volume ELSE 0 END) AS vc
                         FROM opt_bars b JOIN opt_contracts c USING (symbol) GROUP BY 1, 2""").df()
    con.close()
    vol["trade_date"] = pd.to_datetime(vol["trade_date"])
    vol = vol.sort_values(["underlying", "trade_date"])
    vol["avg20"] = vol.groupby("underlying")["v"].transform(lambda s: s.shift(1).rolling(20, min_periods=15).mean())
    vol["ratio"] = vol["v"] / vol["avg20"]
    vol["call_share"] = vol["vc"] / vol["v"]
    sig = vol[(vol["ratio"] >= args.ratio) & (vol["v"] >= 500) & (vol["trade_date"] >= args.start) & (vol["trade_date"] <= args.end)].copy()
    sig["kind"] = np.where(sig["call_share"] >= args.share, "bullish", np.where(sig["call_share"] <= 1 - args.share, "bearish", "mixed"))
    print(f"unusual days: {len(sig)} of {len(vol)} underlying-days; kinds {sig['kind'].value_counts().to_dict()}", flush=True)

    with PanelStore(read_only=True) as store:
        market = load_market(store, "2023-10-01")
    tr = market.tradable(5e6, np.inf)
    ev = pd.DataFrame({"date": sig["trade_date"], "ticker": sig["underlying"], "kind": sig["kind"], "ratio": sig["ratio"]})
    ev = ev[ev["ticker"].isin(tr.columns)]
    ev = ev[[bool(tr.at[d, t]) if d in tr.index else False for d, t in zip(ev["date"], ev["ticker"])]]
    spy = market.spy
    stats = {}
    same = (market.adj / market.adj.shift(1) - 1) * 100
    same_spy = (spy / spy.shift(1) - 1) * 100
    for kind in ("bullish", "bearish", "mixed"):
        g = ev[ev["kind"] == kind]
        stats[f"{kind}_same_day"] = event_stats(g, same, same_spy, 1, f"{kind} same day (not tradable)")
        for h in (1, 5, 10, 20):
            fwd = (market.adj.shift(-h) / market.adj_open.shift(-1) - 1) * 100
            mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
            stats[f"{kind}_h{h}"] = event_stats(g, fwd, mkt, h, f"{kind} h{h}")
    # bullish flow, by size of the spike
    big = ev[(ev["kind"] == "bullish") & (ev["ratio"] >= 2 * args.ratio)]
    fwd5 = (market.adj.shift(-5) / market.adj_open.shift(-1) - 1) * 100
    stats["bullish_big_h5"] = event_stats(big, fwd5, (spy.shift(-5) / spy.shift(-1) - 1) * 100, 5, f"bullish >= {2 * args.ratio:.0f}x h5")

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"s32_unusual_options_{stamp}.json"), "w") as f:
        json.dump({"n_events": int(len(ev)), "params": vars(args), "stats": stats}, f, indent=1, default=float)
    L = [f"# S32 — unusual options activity as a signal ({stamp})", "",
         f"S&P names, monthlies within 15% of spot, {args.start} → {args.end}. Unusual = volume >= {args.ratio:.0f}x trailing 20-day mean and >= 500 contracts; "
         f"bullish = call share >= {args.share:.0%}, bearish <= {1 - args.share:.0%}. {len(ev)} tradable events. Abnormal = minus SPY, clustered by date.", "",
         "| cut | h | events | dates | mean abn % | NW t | hit | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        if "mean_abn_pct" not in s:
            L.append(f"| {s['label']} | — | — | {s['n_dates']} | too few | | | | | |")
            continue
        L.append(f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['t_nw']:.2f} | {s['hit_rate']:.0%} | "
                 f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    text = "\n".join(L) + "\n"
    with open(os.path.join(OUT_DIR, f"s32_unusual_options_{stamp}.md"), "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
