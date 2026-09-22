"""Event line: open-market insider buying (Form 4, code P), the S11–S13 rules unchanged.

Trigger on the FILING date: >= 2 distinct buyers that day, or >= $250k bought.
Universe: 20-day dollar volume $3M–$100M (micro spreads eat the edge, large
caps have none). Hold 5 sessions. This is agent/books/short_term.py behind the
EventLine interface; the numbers are the same so the backtest is the same.
"""
from __future__ import annotations

import pandas as pd

from agent.books.data import insider_flows
from agent.books.short_term import ADV_CEILING, ADV_FLOOR, BIG_USD, HOLD_DAYS, MAX_POSITIONS
from agent.events.base import EVENT_COLS, EventLine, LineSpec
from hedge_fund.features.panel import PanelStore


class InsiderBuys(EventLine):
    spec = LineSpec(name="insider_buy", version="1", hold_days=HOLD_DAYS, max_slots=MAX_POSITIONS,
                    adv_floor=ADV_FLOOR, adv_ceiling=ADV_CEILING,
                    hypothesis="clustered or large open-market insider purchases are followed by a 1-5 day drift")

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        f = insider_flows(store, start)
        f = f[(f["n_buyers"] >= 2) | (f["buy_usd"] >= BIG_USD)]
        out = pd.DataFrame({"date": pd.to_datetime(f["date"]), "ticker": f["ticker"], "side": "L",
                            "strength": f["buy_usd"].astype(float),
                            "detail": f["n_buyers"].map(lambda n: "cluster" if n >= 2 else "big_usd")})
        return out[EVENT_COLS].reset_index(drop=True)
