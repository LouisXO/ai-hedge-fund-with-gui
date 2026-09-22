"""Event lines for the short-term book.

An event line turns a public record (a filing, a print, an announcement)
into dated, directional entries with a fixed holding period. Every line
answers the same three questions and nothing else:

  events(store, start, end)  -> DataFrame[date, ticker, side, strength, detail]
                                 date = the day the information became public
                                 (filing date, not transaction date); the book
                                 trades at the NEXT open
  hold_days                  -> sessions to hold; the engine exits at that open
  max_slots                  -> cap on concurrent positions from this line, so
                                 one busy filing week cannot fill the book

The book is the union of the lines that passed validation, run through the
same engine (agent/books/engine.py) with the same cost model. A ticker is
in at most one line at a time (first come, first served), and every line
carries its own pre-registered rules and version, so adding or retiring a
line never touches another.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agent.books.data import Market
from hedge_fund.features.panel import PanelStore

EVENT_COLS = ["date", "ticker", "side", "strength", "detail"]


@dataclass(frozen=True)
class LineSpec:
    name: str
    version: str
    hold_days: int
    max_slots: int
    adv_floor: float
    adv_ceiling: float
    hypothesis: str


class EventLine:
    spec: LineSpec

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        raise NotImplementedError

    def targets(self, market: Market, ev: pd.DataFrame, start: str, end: str) -> dict[pd.Timestamp, list[str]]:
        """Engine input: per event date, the long candidates in priority order, tradable that day."""
        tradable = market.tradable(self.spec.adv_floor, self.spec.adv_ceiling)
        ev = ev[(ev["side"] == "L") & ev["ticker"].isin(tradable.columns)]
        ev = ev[(ev["date"] >= pd.Timestamp(start)) & (ev["date"] <= pd.Timestamp(end))]
        out: dict[pd.Timestamp, list[str]] = {}
        for d, g in ev.groupby("date"):
            if d not in tradable.index:
                continue
            ok = g[[bool(tradable.at[d, t]) for t in g["ticker"]]]
            if not ok.empty:
                out[d] = ok.sort_values("strength", ascending=False)["ticker"].drop_duplicates().tolist()
        return out


def empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=EVENT_COLS)
