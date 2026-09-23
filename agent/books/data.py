"""Shared inputs for the books: prices, tradable mask, spreads, insider flows.

All frames are wide (date x ticker). `tradable[t, d]` is the only universe
definition: listed that day (point-in-time) AND 20-day dollar volume inside
the book's [floor, ceiling]. Nothing else is allowed to select names.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from agent.s11_insider_wide import listed_mask
from hedge_fund.features.panel import PanelStore

BUCKETS = [-np.inf, 3e6, 2e7, 1e8, np.inf]
LABELS = ["micro", "small", "mid", "large"]
MAX_TX_USD = 50e6          # a single open-market insider trade above this is a parsing error, not a signal


@dataclass
class Market:
    adj: pd.DataFrame          # adjusted close (total return)
    adj_open: pd.DataFrame     # open scaled by the same factor
    close: pd.DataFrame        # raw close (for dollar volume)
    adv20: pd.DataFrame        # 20-day mean dollar volume
    listed: pd.DataFrame       # point-in-time listing mask
    spy: pd.Series             # SPY adjusted close
    spread: dict               # (bucket, year) -> median quoted spread %
    iwm: pd.Series | None = None   # IWM adjusted close (size factor)

    def tradable(self, adv_floor: float, adv_ceiling: float) -> pd.DataFrame:
        return self.listed & (self.adv20 >= adv_floor) & (self.adv20 <= adv_ceiling) & self.adj.notna()

    def bucket(self, ticker: str, day: pd.Timestamp) -> str:
        v = self.adv20.at[day, ticker]
        if pd.isna(v):
            return "small"
        return LABELS[int(np.searchsorted(BUCKETS[1:], v, side="right"))]

    def spread_pct(self, ticker: str, day: pd.Timestamp) -> float:
        key = (self.bucket(ticker, day), day.year)
        if key in self.spread:
            return self.spread[key]
        same_bucket = [v for (b, y), v in self.spread.items() if b == key[0]]
        return float(np.median(same_bucket)) if same_bucket else 0.5


def load_market(store: PanelStore, start: str, lookback_days: int = 400) -> Market:
    lb = (pd.Timestamp(start) - pd.Timedelta(days=lookback_days)).date().isoformat()
    close, adj = store.bars_wide("close", start=lb), store.bars_wide("adj_close", start=lb)
    opn, vol = store.bars_wide("open", start=lb), store.bars_wide("volume", start=lb)
    adj_open = opn * (adj / close)
    adv20 = (close * vol).rolling(20).mean()
    listed = listed_mask(store, adj.index, list(adj.columns))
    spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()
    grid = store.con.execute("SELECT bucket, year, median_spread_pct FROM spread_grid").df()
    spread = {(r.bucket, int(r.year)): float(r.median_spread_pct) for r in grid.itertuples()}
    try:
        iwm = store.index_series("IWM", "adj_close").reindex(adj.index).ffill()
    except Exception:
        iwm = None
    return Market(adj, adj_open, close, adv20, listed, spy, spread, iwm)


def fundamentals(store: PanelStore) -> pd.DataFrame | None:
    """panel.fundamentals_pit, built by agent.books.fundamentals.factor_inputs; None if absent."""
    try:
        df = store.con.execute("SELECT * FROM fundamentals_pit").df()
    except Exception:
        return None
    df["filed"] = pd.to_datetime(df["filed"])
    # shares_override: a few names report every share count per class (V, BRK.B, STZ, ERIE), which the
    # XBRL feed cannot see; their current count from yfinance stands in (not point-in-time — a share
    # count moves a few % a year, the price is what moves the ratio). Audit S28, 2026-09-22.
    df.loc[df["shares"] <= 1000, "shares"] = np.nan             # 0 / 1 / negative counts are XBRL noise (FOX, HOOD, EL...)
    try:
        ov = store.con.execute("SELECT ticker, shares FROM shares_override").df().set_index("ticker")["shares"]
        m = df["ticker"].map(ov)
        df["shares"] = m.where(m.notna(), df["shares"])
    except Exception:
        pass
    return df


def insider_flows(store: PanelStore, start: str) -> pd.DataFrame:
    """(filing_date, ticker) -> n_buyers, buy_usd, sell_usd. Filing date is the only date used."""
    df = store.con.execute("""
        SELECT filing_date, ticker,
               count(DISTINCT CASE WHEN trans_code='P' THEN owner_name END) AS n_buyers,
               sum(CASE WHEN trans_code='P' AND value_usd <= ? THEN value_usd ELSE 0 END) AS buy_usd,
               sum(CASE WHEN trans_code='S' AND value_usd <= ? THEN value_usd ELSE 0 END) AS sell_usd
        FROM insider_tx WHERE filing_date >= ? AND acq_disp IN ('A','D')
        GROUP BY 1, 2""", [MAX_TX_USD, MAX_TX_USD, start]).df()
    df["date"] = pd.to_datetime(df["filing_date"])
    return df
