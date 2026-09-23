"""S30 — the explicit momentum book, pre-registered 2026-09-22 before any run.

S24 (sector-neutral kills the alpha) and S29 (rank-normal scores kill it)
showed that v1's four-family composite is, in effect, "the names at the
momentum winsor cap". This is that rule stated plainly, with nothing else:

  universe   listed point-in-time, 20-day dollar volume >= $5M, close >= $2,
             >= 253 sessions of history. No fundamentals at all.
  signal     12-1 momentum: adj close 21 sessions ago / 252 sessions ago - 1
  book       scored daily; enter rank <= 30, keep while rank <= 60 (v1's slot rule),
             equal-dollar slots, half a quoted spread per side, idle in SPY
Variants (all three registered together):
  mom_top           the rule above
  mom_top_momcrash  same, but while SPY is > 20% below its 2-year high no new entries are
                    made (holdings stay until they fall out of the top 60)
  mom_top_lowvol    candidate pool = momentum top 60; entry order within it by LOWEST
                    trailing-252 vol; keep = the pool. Tests whether low vol adds anything
                    as a tie-breaker inside the momentum tail.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market
from agent.books.long_v2 import spy_in_crash_regime

ADV_FLOOR = 5e6
MIN_PRICE = 2.0
TOP_N, KEEP_MULT = 30, 2


def momentum_lists(market: Market, start: str, end: str, variant: str = "mom_top"):
    """Returns (targets, sizes): targets[day] = ranked list; sizes[day] = {ticker: 0} on no-entry days."""
    mom = market.adj.shift(21) / market.adj.shift(252) - 1
    hist_ok = market.adj.notna().rolling(253, min_periods=253).sum() >= 240
    vol = np.log(market.adj / market.adj.shift(1)).rolling(252, min_periods=200).std()
    tradable = market.tradable(ADV_FLOOR, np.inf) & (market.close >= MIN_PRICE) & hist_ok
    crash = spy_in_crash_regime(market) if variant == "mom_top_momcrash" else None
    targets, sizes = {}, {}
    for d in market.adj.loc[start:end].index:
        ok = tradable.loc[d]
        s = mom.loc[d][ok[ok].index].dropna().sort_values(ascending=False)
        pool = s.head(KEEP_MULT * TOP_N)
        if variant == "mom_top_lowvol":
            v = vol.loc[d].reindex(pool.index)
            pool = pool.loc[v.sort_values().index]                # lowest vol first within the momentum tail
        targets[d] = pool.index.tolist()
        if crash is not None and bool(crash.get(d, False)):
            sizes[d] = {t: 0.0 for t in targets[d]}                # keep what is held, enter nothing
    return targets, sizes
