"""Long-term book v2: standard multi-factor selection, monthly rebalance.

v1 (S15) tested trailing insider net buying and found it decisively negative
— the insider effect is a short-window one. v2 does what long-horizon quant
books actually do: combine several weak, well-documented factors across a
wide universe and rebalance slowly. Pre-registered books, all long-only,
top-N equal weight, monthly:

  composite   equal-weight mean of value, quality, momentum, low-vol z-scores
  value       book/market + earnings yield
  quality     gross profitability, ROE, accruals, asset growth
  momentum_12_1   the price-only control from v1
  insider_net_6m  kept from v1 as the negative control

Universe: listed point-in-time, ADV >= $5M, and a fundamentals row filed
within the last 200 days (no filings = not investable here). Costs and
execution as in the short book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market
from agent.books.factors import factor_scores

ADV_FLOOR = 5e6
TOP_N = 30
NET_WINDOW = 126


def _month_ends(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.Series(days, index=days).groupby([days.year, days.month]).last().to_numpy())


def net_buying_panel(market: Market, flows: pd.DataFrame) -> pd.DataFrame:
    f = flows[flows["ticker"].isin(market.adj.columns)]
    net = (f.assign(net=f["buy_usd"] - f["sell_usd"])
            .pivot_table(index="date", columns="ticker", values="net", aggfunc="sum")
            .reindex(market.adj.index).fillna(0.0))
    net = net.reindex(columns=market.adj.columns).fillna(0.0)
    return net.rolling(NET_WINDOW, min_periods=20).sum()


def scores(market: Market, flows: pd.DataFrame, fund: pd.DataFrame | None, start: str, end: str,
           top_n: int = TOP_N) -> dict[str, dict[pd.Timestamp, list[str]]]:
    days = market.adj.loc[start:end].index
    rebal = _month_ends(days)
    tradable = market.tradable(ADV_FLOOR, np.inf)
    net = net_buying_panel(market, flows)
    mom = market.adj.shift(21) / market.adj.shift(252) - 1
    out: dict[str, dict] = {"momentum_12_1": {}, "insider_net_6m": {}}
    if fund is not None:
        out.update({"composite": {}, "value": {}, "quality": {}})
    for d in rebal:
        ok = tradable.loc[d]
        universe = ok[ok].index
        s_mom = mom.loc[d, universe].dropna()
        out["momentum_12_1"][d] = s_mom.sort_values(ascending=False).head(top_n).index.tolist()
        s_net = (net.loc[d, universe] / market.adv20.loc[d, universe]).replace([np.inf, -np.inf], np.nan).dropna()
        out["insider_net_6m"][d] = s_net[s_net > 0].sort_values(ascending=False).head(top_n).index.tolist()
        if fund is not None:
            fs = factor_scores(market, fund, d, universe)
            fs = fs[fs["n_families"] >= 3]                     # need most of the picture, not one family
            for book, col in (("composite", "composite"), ("value", "value"), ("quality", "quality")):
                s = fs[col].dropna()
                out[book][d] = s.sort_values(ascending=False).head(top_n).index.tolist()
    return out
