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
OVERRIDE_DAYS = 120        # shares_override reaches rows filed this many days before its as_of, none earlier or after


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
    # S47b 1 (replaces S47 补充 5's 400 days from the newest filing): only where the count is missing or
    # flagged (shares_flag not empty), and only on rows filed within OVERRIDE_DAYS before the override's
    # as_of — none after it, none older. A backtest day can still see a count up to OVERRIDE_DAYS newer
    # than itself on those rows; every other row, and every unflagged count, is as built.
    try:
        ov = store.con.execute("SELECT ticker, shares, as_of FROM shares_override").df()
        ov = ov.drop_duplicates("ticker", keep="last").set_index("ticker")
        m, asof = df["ticker"].map(ov["shares"]), pd.to_datetime(df["ticker"].map(ov["as_of"]))
        flagged = df["shares_flag"].fillna("").ne("") if "shares_flag" in df else False
        use = (m.notna() & (df["shares"].isna() | flagged)
               & (df["filed"] <= asof) & (df["filed"] >= asof - pd.Timedelta(days=OVERRIDE_DAYS)))
        df.loc[use, "shares"] = m[use]
    except Exception:
        pass
    return df


def insider_flows(store: PanelStore, start: str) -> pd.DataFrame:
    """(filing_date, ticker) -> n_buyers, buy_usd, sell_usd. Filing date is the only date used.

    A purchase (code P) counts, as a buyer and in dollars, only if its price lies within
    [0.8 x low, 1.25 x high] of the bar of its transaction date, or of the nearest earlier session
    when that day has none; with no bar at all it does not count (S47 addendum item 13: NCT filed
    a buy at $0.40 on 2026-09-25 with the stock at $4.43; a foreign issuer's Form 4 is priced in
    its own currency). The bar is never later than the filing date, so nothing after it is used.
    """
    df = store.con.execute("""
        WITH tx AS (
            SELECT *, least(coalesce(trans_date, filing_date), filing_date) AS px_day
            FROM insider_tx WHERE filing_date >= ? AND acq_disp IN ('A','D')),
        priced AS (
            SELECT tx.*, tx.price BETWEEN 0.8 * b.low AND 1.25 * b.high AS sane    -- NULL without a bar
            FROM tx ASOF LEFT JOIN (SELECT ticker, trade_date, low, high FROM bars
                                    WHERE low IS NOT NULL AND high IS NOT NULL) b
              ON tx.ticker = b.ticker AND tx.px_day >= b.trade_date)
        SELECT filing_date, ticker,
               count(DISTINCT CASE WHEN trans_code='P' AND sane THEN owner_name END) AS n_buyers,
               sum(CASE WHEN trans_code='P' AND sane AND value_usd <= ? THEN value_usd ELSE 0 END) AS buy_usd,
               sum(CASE WHEN trans_code='S' AND value_usd <= ? THEN value_usd ELSE 0 END) AS sell_usd
        FROM priced GROUP BY 1, 2""", [start, MAX_TX_USD, MAX_TX_USD]).df()
    df["date"] = pd.to_datetime(df["filing_date"])
    return df
