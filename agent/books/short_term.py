"""Short-term book: insider buys, 5-day hold, small/mid caps, limit-order execution.

Pre-registered from S11-S14 (docs/AGENT_PLAN.md §9) — nothing here was
tuned on the backtest that follows:
  event      Form 4 open-market purchase, dated by filing date
  qualify    cluster (>= 2 distinct buyers that day) or >= $250k
  universe   listed point-in-time, ADV in [$3M, $100M]
  entry      next open;  exit  close-to-open after HOLD_DAYS
  slots      MAX_POSITIONS equal-dollar; if more candidates than slots,
             larger dollar purchases first
  cost       exec_frac x quoted spread each way (0.5 = cross once)
"""
from __future__ import annotations

import pandas as pd

from agent.books.data import Market

ADV_FLOOR, ADV_CEILING = 3e6, 1e8
BIG_USD = 250_000
HOLD_DAYS = 5
MAX_POSITIONS = 20


def targets(market: Market, flows: pd.DataFrame, start: str, end: str) -> dict[pd.Timestamp, list[str]]:
    tradable = market.tradable(ADV_FLOOR, ADV_CEILING)
    ev = flows[(flows["n_buyers"] >= 2) | (flows["buy_usd"] >= BIG_USD)]
    ev = ev[(ev["date"] >= pd.Timestamp(start)) & (ev["date"] <= pd.Timestamp(end))]
    ev = ev[ev["ticker"].isin(tradable.columns)]
    out: dict[pd.Timestamp, list[str]] = {}
    idx = tradable.index
    for d, g in ev.groupby("date"):
        if d not in idx:
            continue
        ok = g[[bool(tradable.at[d, t]) for t in g["ticker"]]]
        if ok.empty:
            continue
        out[d] = ok.sort_values("buy_usd", ascending=False)["ticker"].tolist()
    return out
