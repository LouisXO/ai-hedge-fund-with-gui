"""The long book, live: what the composite would hold from the next open.

Cadence is signal-driven, not calendar-driven (decided 2026-09-20: "long
term" means the holding period, not month-ends). Every morning the
universe is scored on the last completed bar and the top KEEP_MULT*N
names are recorded with their rank. Read with the engine's slot rule that
gives: enter when a name ranks inside the top N and a slot is free, keep
it while it stays inside the top 2N, sell when it falls out — exactly
agent/books/long_term.daily_composite, which is what the backtest ran.

Recording the ranked list daily (rather than a held set) keeps the shadow
record stateless: holdings are reconstructed by replaying the engine on
the recorded lists, so the live record and the backtest use one code path.

Shorting was allowed but not favoured, so momentum stays a component of
the composite and there is no long-short book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market, fundamentals
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR, TOP_N
from hedge_fund.features.panel import PanelStore

SIGNAL = "composite_long"
KEEP_MULT = 2


def should_score(last_bar: pd.Timestamp, last_recorded: pd.Timestamp | None) -> bool:
    """Once per completed bar: skip if this bar's list is already recorded (reruns are idempotent)."""
    return last_recorded is None or last_recorded.normalize() != last_bar.normalize()


def day_scores(store: PanelStore, market: Market, day: pd.Timestamp) -> pd.DataFrame:
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
    """The day's ranked list: top KEEP_MULT*N. Ranks <= N are entry candidates, the rest are keep-only."""
    fs = day_scores(store, market, day)
    if fs.empty:
        return []
    out = []
    for rank, (ticker, row) in enumerate(fs.head(KEEP_MULT * top_n).iterrows(), 1):
        out.append({"ticker": ticker, "signal_name": SIGNAL, "side": "L", "rank": rank,
                    "value": float(row["composite"]), "instrument": "stock",
                    "limit_ref": float(market.close.at[day, ticker]),
                    "spread_pct": market.spread_pct(ticker, day),
                    "gate_passed": rank <= top_n,                   # False = keep-only zone (N < rank <= 2N)
                    "gate_reason": f"v{row['value']:+.2f} q{row['quality']:+.2f} "
                                                        f"m{row['momentum']:+.2f} lv{row['lowvol']:+.2f}",
                    "expected_net_pct": None, "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None})
    return out
