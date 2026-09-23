"""Event line: an 8-K announcing a share repurchase program.

Hypothesis (Ikenberry, Lakonishok & Vermaelen 1995; Peyer & Vermaelen 2009;
Manconi, Peyer & Vermaelen 2019 for the post-2000 sample): open-market
repurchase authorizations carry a ~+2-3% announcement return and a drift
over the following months, strongest in small caps and in value names. We
trade the filing date at the next open, so the announcement-day return is
mostly not ours; the question is whether any drift is left over 20 sessions.

Rules (v1, pre-registered before the backtest ran, 2026-09-23):
  trigger   an 8-K whose text contains "repurchase program" (agent/sources/sec_8k_buyback.py)
  cells     clean  = the filing has no Item 2.02 (not an earnings release): the buyback is
                     the news. This is the cell that would go live.
            all    = every such 8-K, earnings releases included (confounded; report only)
  strength  1.0 (authorization sizes are not parsed; equal priority, then ADV as tiebreak
            is not applied — insertion order = filing order)
  universe  ADV $3M–$1B, listed that day (the literature's effect is in small/mid caps)
  hold      20 sessions goes live if it passes; 5 and 40 are report variants
"""
from __future__ import annotations

import pandas as pd

from agent.events.base import EVENT_COLS, EventLine, LineSpec
from hedge_fund.features.panel import PanelStore


class Buyback8K(EventLine):
    def __init__(self, cell: str = "clean"):
        assert cell in ("clean", "all")
        self.cell = cell
        self.spec = LineSpec(name=f"buyback_{cell}", version="1", hold_days=20, max_slots=20, adv_floor=3e6, adv_ceiling=1e9,
                             hypothesis={"clean": "a standalone 8-K repurchase authorization is followed by positive abnormal returns over 1-2 months",
                                         "all": "report only: every 8-K mentioning a repurchase program, earnings releases included"}[cell])

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        df = store.con.execute("""
            SELECT filed AS date, ticker, items, has_earnings FROM buyback_8k
            WHERE ticker IS NOT NULL AND filed BETWEEN ? AND ?""", [start, end]).df()
        if self.cell == "clean":
            df = df[~df["has_earnings"]]
        if df.empty:
            return pd.DataFrame(columns=EVENT_COLS)
        df["date"] = pd.to_datetime(df["date"])
        df["side"] = "L"
        df["strength"] = 1.0
        df["detail"] = "items " + df["items"].fillna("")
        df = df.drop_duplicates(["date", "ticker"])
        return df[EVENT_COLS].reset_index(drop=True)
