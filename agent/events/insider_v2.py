"""Insider line v2 candidates: who is buying, and is it routine?

Cohen, Malloy & Pomorski (2012, "Decoding inside information"): insiders who
trade in the same calendar month year after year are "routine" and carry no
information; the abnormal returns of insider trading come almost entirely
from the "opportunistic" remainder. Seyhun (1986, 1998): officers' purchases
are more informative than directors' and 10% owners'. Lakonishok & Lee
(2001): purchases after a price decline are the informative ones.

Each variant is the v1 line (cluster >= 2 or >= $250k, ADV $3M–$100M, hold 5)
with ONE extra condition, pre-registered 2026-09-22 before any backtest:

  opp        count only opportunistic buyers: an owner is routine for a ticker if
             they bought in the same calendar month in >= 2 of the 3 prior years
             (the panel starts 2015, so this is decidable from 2017 on)
  officer    at least one buyer is an officer (relationship contains 'Officer')
  ceo        at least one buyer whose title contains CEO / Chief Executive / President / CFO
  contrarian the name's 20-day return before the filing is below -5%
  opp_ceo    opp AND ceo

Same holding period and universe as v1 so any difference is the condition.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import MAX_TX_USD, Market, load_market
from agent.books.short_term import ADV_CEILING, ADV_FLOOR, BIG_USD, HOLD_DAYS, MAX_POSITIONS
from agent.events.base import EVENT_COLS, EventLine, LineSpec
from hedge_fund.features.panel import PanelStore

CEO_RX = r"CEO|Chief Executive|President|CFO|Chief Financial"


def purchases(store: PanelStore, start: str) -> pd.DataFrame:
    """One row per open-market purchase with owner role flags and the routine label."""
    df = store.con.execute("""
        SELECT filing_date, ticker, owner_name, relationship, officer_title, value_usd
        FROM insider_tx WHERE trans_code = 'P' AND acq_disp = 'A' AND value_usd > 0 AND value_usd <= ?
          AND filing_date >= '2015-01-01'""", [MAX_TX_USD]).df()
    df["date"] = pd.to_datetime(df["filing_date"])
    df["is_officer"] = df["relationship"].fillna("").str.contains("Officer")
    df["is_ceo"] = df["officer_title"].fillna("").str.contains(CEO_RX, case=False, regex=True)
    # routine: same (owner, ticker) bought in this calendar month in >= 2 of the 3 prior years
    ym = df[["owner_name", "ticker"]].assign(y=df["date"].dt.year, m=df["date"].dt.month).drop_duplicates()
    key = set(zip(ym["owner_name"], ym["ticker"], ym["y"], ym["m"]))
    y, m = df["date"].dt.year.to_numpy(), df["date"].dt.month.to_numpy()
    df["routine"] = [sum((o, t, yy - k, mm) in key for k in (1, 2, 3)) >= 2
                     for o, t, yy, mm in zip(df["owner_name"], df["ticker"], y, m)]
    return df[df["date"] >= pd.Timestamp(start)]


def aggregate(p: pd.DataFrame, opp_only: bool) -> pd.DataFrame:
    if opp_only:
        p = p[~p["routine"]]
    g = p.groupby(["date", "ticker"]).agg(n_buyers=("owner_name", "nunique"), buy_usd=("value_usd", "sum"),
                                          any_officer=("is_officer", "max"), any_ceo=("is_ceo", "max")).reset_index()
    return g[(g["n_buyers"] >= 2) | (g["buy_usd"] >= BIG_USD)]


class InsiderV2(EventLine):
    def __init__(self, variant: str):
        assert variant in ("opp", "officer", "ceo", "contrarian", "opp_ceo")
        self.variant = variant
        self.spec = LineSpec(name=f"insider_{variant}", version="2", hold_days=HOLD_DAYS, max_slots=MAX_POSITIONS,
                             adv_floor=ADV_FLOOR, adv_ceiling=ADV_CEILING,
                             hypothesis=f"v1 insider rule restricted by '{variant}' keeps the informative purchases")

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        p = purchases(store, start)
        g = aggregate(p, opp_only=self.variant in ("opp", "opp_ceo"))
        if self.variant in ("officer",):
            g = g[g["any_officer"]]
        if self.variant in ("ceo", "opp_ceo"):
            g = g[g["any_ceo"]]
        if self.variant == "contrarian":
            market = load_market(store, start)
            r20 = (market.adj / market.adj.shift(20) - 1) * 100
            prior = [float(r20.at[d, t]) if (d in r20.index and t in r20.columns) else np.nan for d, t in zip(g["date"], g["ticker"])]
            g = g[np.array(prior) < -5.0]
        g = g[(g["date"] >= pd.Timestamp(start)) & (g["date"] <= pd.Timestamp(end))]
        out = pd.DataFrame({"date": g["date"], "ticker": g["ticker"], "side": "L", "strength": g["buy_usd"].astype(float),
                            "detail": g["n_buyers"].map(lambda n: "cluster" if n >= 2 else "big_usd")})
        return out[EVENT_COLS].reset_index(drop=True)
