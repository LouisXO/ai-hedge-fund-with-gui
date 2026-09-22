"""Event line: initial Schedule 13D (a holder crossing 5% with intent to influence).

Hypothesis (Brav, Jiang, Partnoy & Thomas 2008; Bebchuk, Brav & Jiang 2015):
the filing day carries a positive abnormal return and a drift over the
following weeks, and the effect has not decayed the way most anomalies did.
We trade the filing date at the next open, so the pre-filing run-up (which
is most of the headline +7%) is NOT ours; the question the backtest answers
is whether anything is left after the market has seen the filing.

Rules (v1, pre-registered before the backtest ran, 2026-09-22):
  trigger   initial 13D (never an amendment) whose subject has a listed ticker
  strength  1 + number of filers (a group filing is a stronger signal)
  universe  ADV $3M–$500M: the announcement effect is concentrated in small/mid caps
  hold      10 sessions (the literature's drift is weeks, not days; 5 and 20 are
            registered variants for the report, 10 is the one that would go live)
"""
from __future__ import annotations

import pandas as pd

from agent.events.base import EVENT_COLS, EventLine, LineSpec
from hedge_fund.features.panel import PanelStore


class Schedule13D(EventLine):
    spec = LineSpec(name="sch13d", version="1", hold_days=10, max_slots=10, adv_floor=3e6, adv_ceiling=5e8,
                    hypothesis="an initial 13D filing is followed by positive abnormal returns over 1-4 weeks")

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        df = store.con.execute("""
            SELECT filed AS date, ticker, filers FROM sch13d
            WHERE NOT is_amendment AND ticker IS NOT NULL AND filed BETWEEN ? AND ?""", [start, end]).df()
        if df.empty:
            return pd.DataFrame(columns=EVENT_COLS)
        df["date"] = pd.to_datetime(df["date"])
        df["side"] = "L"
        df["strength"] = 1.0 + df["filers"].fillna("").map(lambda s: len([x for x in s.split(";") if x.strip()]))
        df["detail"] = df["filers"].fillna("").str.slice(0, 60)
        # one event per (date, ticker): several filers filing the same day is one event, strength = max
        df = df.sort_values("strength", ascending=False).drop_duplicates(["date", "ticker"])
        return df[EVENT_COLS].reset_index(drop=True)
