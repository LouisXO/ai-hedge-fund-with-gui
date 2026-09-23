"""S31 — when to enter: the open, the close, a VWAP proxy, or the next open?

Same signals, same exits, only the entry print changes. For every entry the
two live books would have made (insider v1 events; the long book's actual
entries from the engine's trade list), the return to the fixed exit from:

  open      the next session's open (what the backtest and the paper book do)
  vwap*     (high + low + close) / 3 of that session — a daily-bar proxy for VWAP
  close     that session's close
  next_open the open one session later (a one-day delay)

Also reported: the entry session's intraday return (close / open - 1), which is
where "the open is the day's high" would show up, clustered by date.

Pre-registered reading (2026-09-23): a timing is worth adopting if its mean
return to exit beats the open's by >= 0.15% with NW t >= 2 for the insider
line (5-day hold) — that is a third of the line's whole edge — and the sign
holds in both halves. For the long book a difference of that size is noise
against a 38-day median hold, so it is reported, not judged.

Usage: python -m agent.s31_entry_timing [--start 2017-01-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from agent.books import long_v2, short_term
from agent.books.data import fundamentals, insider_flows, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.events import LINES
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"


def entry_table(store, market, entries: pd.DataFrame, hold: int) -> pd.DataFrame:
    """entries: columns [date (signal day), ticker]. Returns per-entry returns for each timing."""
    idx = market.adj.index
    adj, close = market.adj, market.close
    ratio = adj / close                                   # adjustment factor per day
    opn = market.adj_open
    hi = store.bars_wide("high", start=str(idx[0].date())).reindex(index=idx, columns=close.columns) * ratio
    lo = store.bars_wide("low", start=str(idx[0].date())).reindex(index=idx, columns=close.columns) * ratio
    vwap = (hi + lo + adj) / 3
    rows = []
    pos = idx.searchsorted(entries["date"].to_numpy())
    for (d, t), p in zip(entries[["date", "ticker"]].itertuples(index=False), pos):
        e = p + (1 if idx[p] == d else 0) if p < len(idx) else None    # entry session = next after signal
        if e is None or e + hold + 1 >= len(idx) or t not in adj.columns:
            continue
        ed, xd, nd = idx[e], idx[e + hold], idx[e + 1]
        px = {"open": opn.at[ed, t], "vwap": vwap.at[ed, t], "close": adj.at[ed, t], "next_open": opn.at[nd, t]}
        exit_px = adj.at[xd, t]
        if any(pd.isna(v) or v <= 0 for v in px.values()) or pd.isna(exit_px):
            continue
        rows.append({"date": ed, "ticker": t, "intraday": (px["close"] / px["open"] - 1) * 100,
                     **{f"ret_{k}": (exit_px / v - 1) * 100 for k, v in px.items()}})
    return pd.DataFrame(rows)


def by_date_stats(df: pd.DataFrame, col: str, base: str | None = None) -> dict:
    g = df.groupby("date")[col].mean()
    x = g.to_numpy()
    if base:
        x = (df.groupby("date")[col].mean() - df.groupby("date")[base].mean()).to_numpy()
    n = len(x)
    return {"mean": float(x.mean()), "t_nw": newey_west_t(x, lag=5), "n_dates": int(n),
            "first_half": float(x[: n // 2].mean()), "second_half": float(x[n // 2:].mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        flows = insider_flows(store, (dt.date.fromisoformat(args.start) - dt.timedelta(days=200)).isoformat())
        # insider entries: the v1 line's tradable events (one per ticker-day)
        ins = LINES["insider_buy"]
        ev = ins.events(store, args.start, args.end)
        tg = ins.targets(market, ev, args.start, args.end)
        ins_entries = pd.DataFrame([(d, t) for d, names in tg.items() for t in names], columns=["date", "ticker"])
        # long-book entries: the engine's actual trades (signal day = entry day - 1 session)
        sc = long_v2.daily_scores(market, fund, args.start, args.end)
        res = simulate(market, long_v2.targets_from_scores(sc, TOP_N), args.start, args.end, TOP_N, None, 0.5, cash_in_spy=True)
        idx = market.adj.index
        long_entries = pd.DataFrame([(idx[idx.get_loc(tr.entry_day) - 1], tr.ticker) for tr in res.trades], columns=["date", "ticker"])
        tabs = {"insider_5d": entry_table(store, market, ins_entries, short_term.HOLD_DAYS),
                "long_20d": entry_table(store, market, long_entries, 20)}
    out, L = {}, [f"# S31 — entry timing ({dt.date.today()})", "",
                  "Same signals and exits; only the entry print moves. Returns in %, clustered by entry date (NW t).", ""]
    for name, df in tabs.items():
        out[name] = {"n_entries": int(len(df)), "intraday": by_date_stats(df, "intraday")}
        for k in ("open", "vwap", "close", "next_open"):
            out[name][k] = by_date_stats(df, f"ret_{k}")
            out[name][f"{k}_minus_open"] = by_date_stats(df, f"ret_{k}", base="ret_open") if k != "open" else None
        L += [f"## {name} — {len(df)} entries", "", "| entry at | mean ret to exit | vs open | NW t (diff) | 1st half diff | 2nd half diff |", "|---|---|---|---|---|---|"]
        for k in ("open", "vwap", "close", "next_open"):
            s = out[name][k]
            d = out[name][f"{k}_minus_open"]
            L.append(f"| {k} | {s['mean']:+.2f} | {'' if d is None else f'{d['mean']:+.2f}'} | {'' if d is None else f'{d['t_nw']:.2f}'} | "
                     f"{'' if d is None else f'{d['first_half']:+.2f}'} | {'' if d is None else f'{d['second_half']:+.2f}'} |")
        i = out[name]["intraday"]
        L += ["", f"Entry-session intraday return (close/open − 1): {i['mean']:+.2f}% (t {i['t_nw']:.2f}), halves {i['first_half']:+.2f} / {i['second_half']:+.2f}", ""]
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    with open(os.path.join(OUT_DIR, f"s31_entry_timing_{stamp}.json"), "w") as f:
        json.dump(out, f, indent=1, default=float)
    with open(os.path.join(OUT_DIR, f"s31_entry_timing_{stamp}.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
