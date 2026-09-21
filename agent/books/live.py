"""The long book, live: what the composite would hold from the next open.

Rebalance rule mirrors the backtest exactly (agent/books/long_term.py):
month-end scores, entered at the following open, held until the next
month-end. In live terms: on the first run of a new month, score the
universe as of the last completed bar (the prior month's last trading
day) and record the top-N; on every other day, return nothing.

Shorting was allowed but not favoured (2026-09-20), so momentum stays a
component of the composite and there is no long-short book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market, fundamentals
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR, TOP_N
from hedge_fund.features.panel import PanelStore

SIGNAL = "composite_long"


def is_rebalance_day(last_bar: pd.Timestamp, today: pd.Timestamp, last_recorded: pd.Timestamp | None) -> bool:
    """The morning run right after a month-end: today is in a new month, the last completed
    bar is still the old month's (so it IS the month-end bar), and that bar has not been
    recorded yet. Scoring on it and entering at today's open is exactly the backtest."""
    if (last_bar.year, last_bar.month) == (today.year, today.month):
        return False
    return last_recorded is None or last_recorded.normalize() != last_bar.normalize()


def month_end_scores(store: PanelStore, market: Market, day: pd.Timestamp) -> pd.DataFrame:
    fund = fundamentals(store)
    if fund is None:
        return pd.DataFrame()
    tradable = market.tradable(ADV_FLOOR, np.inf)
    ok = tradable.loc[day]
    universe = ok[ok].index
    fs = factor_scores(market, fund, day, universe)
    fs = fs[fs["n_families"] >= 3]
    return fs.sort_values("composite", ascending=False)


def targets(store: PanelStore, market: Market, day: pd.Timestamp, top_n: int = TOP_N) -> list[dict]:
    fs = month_end_scores(store, market, day)
    if fs.empty:
        return []
    out = []
    for rank, (ticker, row) in enumerate(fs.head(top_n).iterrows(), 1):
        out.append({"ticker": ticker, "signal_name": SIGNAL, "side": "L", "rank": rank,
                    "value": float(row["composite"]), "instrument": "stock",
                    "limit_ref": float(market.close.at[day, ticker]),
                    "spread_pct": market.spread_pct(ticker, day),
                    "gate_passed": True, "gate_reason": f"v{row['value']:+.2f} q{row['quality']:+.2f} "
                                                        f"m{row['momentum']:+.2f} lv{row['lowvol']:+.2f}",
                    "expected_net_pct": None, "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None})
    return out
