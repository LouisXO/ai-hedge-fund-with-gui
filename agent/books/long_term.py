"""Long-term book: monthly rebalance, hold what insiders have been net buying.

This one is NOT validated yet — the backtest is its first test, so the
hypotheses are written here first and only these are run:

  L1 insider_net_6m   trailing 126 trading days of insider net buying
                      (buys − sales, $) scaled by 20-day dollar volume;
                      long the top N each month-end
  L2 momentum_12_1    the classic control: 12-1 month return, top N
  L3 insider_x_mom    names in the top half of BOTH (a conjunction, not a
                      weighted blend, so there is nothing to tune)

Universe: listed point-in-time, ADV >= $5M (wider than the short book —
a month-long hold can absorb a larger spread), no ceiling. Equal weight,
rebalanced at the first open after each month-end; positions not in the
new list are sold. Costs as in the short book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market

ADV_FLOOR = 5e6
TOP_N = 30
NET_WINDOW = 126


def _month_ends(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.Series(days, index=days).groupby([days.year, days.month]).last().to_numpy())


def net_buying_panel(market: Market, flows: pd.DataFrame) -> pd.DataFrame:
    """Rolling 126-day net insider dollars per name, aligned to trading days."""
    f = flows[flows["ticker"].isin(market.adj.columns)]
    net = (f.assign(net=f["buy_usd"] - f["sell_usd"])
            .pivot_table(index="date", columns="ticker", values="net", aggfunc="sum")
            .reindex(market.adj.index).fillna(0.0))
    net = net.reindex(columns=market.adj.columns).fillna(0.0)
    return net.rolling(NET_WINDOW, min_periods=20).sum()


def scores(market: Market, flows: pd.DataFrame, start: str, end: str) -> dict[str, dict[pd.Timestamp, list[str]]]:
    days = market.adj.loc[start:end].index
    rebal = _month_ends(days)
    tradable = market.tradable(ADV_FLOOR, np.inf)
    net = net_buying_panel(market, flows)
    mom = market.adj.shift(21) / market.adj.shift(252) - 1
    out = {"insider_net_6m": {}, "momentum_12_1": {}, "insider_x_mom": {}}
    for d in rebal:
        ok = tradable.loc[d]
        universe = ok[ok].index
        s_net = (net.loc[d, universe] / market.adv20.loc[d, universe]).replace([np.inf, -np.inf], np.nan).dropna()
        s_mom = mom.loc[d, universe].dropna()
        out["insider_net_6m"][d] = s_net[s_net > 0].sort_values(ascending=False).head(TOP_N).index.tolist()
        out["momentum_12_1"][d] = s_mom.sort_values(ascending=False).head(TOP_N).index.tolist()
        both = s_net.index.intersection(s_mom.index)
        if len(both) >= 2 * TOP_N:
            top_net = s_net[both].rank(pct=True) >= 0.5
            top_mom = s_mom[both].rank(pct=True) >= 0.5
            joint = both[(top_net & top_mom).reindex(both).fillna(False).to_numpy()]
            # among the conjunction, order by net buying so the list is deterministic
            out["insider_x_mom"][d] = s_net[joint].sort_values(ascending=False).head(TOP_N).index.tolist()
    return out
