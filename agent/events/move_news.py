"""Event line: a large one-day move, split by whether there was news about the name.

Tetlock (2010, "Does public financial news resolve asymmetric information?"):
large moves WITHOUT accompanying news are liquidity shocks and reverse;
large moves WITH news are information and continue. The "technical repair"
trade has an evidence base only in this conditional form.

Two long sub-lines from one screen (shorting is not traded, so the other
two cells — no-news up, news down — are recorded as factors only):
  move_nonews_down   abnormal return <= -threshold, no news  -> long (reversal)
  move_news_up       abnormal return >= +threshold, with news -> long (continuation)

Definitions (v1, pre-registered 2026-09-22, before any backtest ran):
  move       close-to-close return minus SPY that day, and |abn| >= max(5%, 3 x
             the stock's trailing 60-day daily sd)
  news       >= 1 Alpaca/Benzinga item tagged with the symbol, created between the
             previous session's 16:00 ET and this session's 16:00 ET, with at most
             3 tagged symbols (roundups tag dozens) and a headline that is not a
             movers list (regex below). Benzinga's own "X shares are trading
             higher" posts DO count as news: they usually state the reason.
  universe   ADV $3M–$1B, listed that day
  hold       5 sessions goes live if it passes; 2 and 10 are report variants
  strength   |abn| / sd (bigger surprise first when slots are scarce)
"""
from __future__ import annotations

import re

import duckdb
import numpy as np
import pandas as pd

from agent.books.data import Market, load_market
from agent.events.base import EVENT_COLS, EventLine, LineSpec
from agent.sources.alpaca_news import NEWS_DB
from hedge_fund.features.panel import PanelStore

ROUNDUP = re.compile(r"movers|top gainers|top losers|biggest|stocks moving|mid-day|midday|premarket|pre-market|"
                     r"after-hours|afterhours|watchlist|earnings scheduled|analyst ratings|option alert|unusual options|"
                     r"52-week|list of|stocks to watch|market clubhouse|benzinga", re.I)
MAX_SYMBOLS = 3
SIGMA_MULT = 3.0
MIN_ABS_PCT = 5.0
SD_WINDOW = 60


def news_days(start: str, end: str) -> pd.DataFrame:
    """(session_date, symbol) pairs with qualifying news in (prev close 16:00 ET, close 16:00 ET]."""
    con = duckdb.connect(str(NEWS_DB), read_only=True)
    try:
        df = con.execute("""
            SELECT s.symbol, i.created_at, i.headline FROM news_symbols s JOIN news_items i USING (id)
            WHERE i.n_symbols <= ? AND i.created_at >= ? AND i.created_at < ?""",
                         [MAX_SYMBOLS, pd.Timestamp(start) - pd.Timedelta(days=3), pd.Timestamp(end) + pd.Timedelta(days=2)]).df()
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame(columns=["date", "symbol"])
    df = df[~df["headline"].fillna("").str.contains(ROUNDUP)]
    df["date"] = assign_session(df["created_at"])
    return df[["date", "symbol"]].drop_duplicates()


def assign_session(created_utc: pd.Series) -> pd.Series:
    """UTC timestamps -> the session date whose close-to-close window contains them.

    An item at 16:00:01 ET on Monday belongs to Tuesday's session (it can only move
    Tuesday's close-to-close return); one at 15:59 ET belongs to Monday's.
    Weekend items roll to the next weekday session's window."""
    ts = pd.to_datetime(created_utc)
    et = (ts.dt.tz_convert("America/New_York") if ts.dt.tz is not None else ts.dt.tz_localize("UTC").dt.tz_convert("America/New_York"))
    shifted = (et - pd.Timedelta(hours=16)).dt.normalize() + pd.Timedelta(days=1)
    day = shifted.dt.tz_localize(None)
    return day + pd.to_timedelta(((7 - day.dt.weekday) % 7).where(day.dt.weekday >= 5, 0), unit="D")


def screen(market: Market, start: str, end: str, adv_floor: float, adv_ceiling: float) -> pd.DataFrame:
    """Every (date, ticker) with a large abnormal move; columns abn, sd, z."""
    ret = market.adj.pct_change() * 100
    abn = ret.sub(market.spy.pct_change() * 100, axis=0)
    sd = ret.shift(1).rolling(SD_WINDOW, min_periods=40).std()
    tradable = market.tradable(adv_floor, adv_ceiling)
    z = (abn / sd).where(tradable)
    big = (z.abs() >= SIGMA_MULT) & (abn.abs() >= MIN_ABS_PCT)
    big = big.loc[start:end]
    st = big.stack()
    st = st[st]
    out = pd.DataFrame({"date": st.index.get_level_values(0), "ticker": st.index.get_level_values(1)})
    out["abn"] = [abn.at[d, t] for d, t in zip(out["date"], out["ticker"])]
    out["z"] = [z.at[d, t] for d, t in zip(out["date"], out["ticker"])]
    return out


class MoveNews(EventLine):
    """cell: 'nonews_down' (long the reversal) or 'news_up' (long the continuation)."""

    def __init__(self, cell: str):
        assert cell in ("nonews_down", "news_up", "nonews_up", "news_down")
        self.cell = cell
        self.spec = LineSpec(name=f"move_{cell}", version="1", hold_days=5, max_slots=20, adv_floor=3e6, adv_ceiling=1e9,
                             hypothesis={"nonews_down": "a large drop with no news is a liquidity shock and reverses",
                                         "news_up": "a large rise on news is information and continues",
                                         "nonews_up": "factor only: a large rise with no news reverses",
                                         "news_down": "factor only: a large drop on news continues"}[cell])

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        market = load_market(store, start)
        sc = screen(market, start, end, self.spec.adv_floor, self.spec.adv_ceiling)
        if sc.empty:
            return pd.DataFrame(columns=EVENT_COLS)
        nd = news_days(start, end)
        key = set(zip(nd["date"], nd["symbol"]))
        sc["has_news"] = [(d, t) in key for d, t in zip(sc["date"], sc["ticker"])]
        want_news = self.cell.startswith("news")
        want_down = self.cell.endswith("down")
        sel = sc[(sc["has_news"] == want_news) & ((sc["abn"] < 0) == want_down)]
        side = "L" if self.cell in ("nonews_down", "news_up") else "S"
        out = pd.DataFrame({"date": sel["date"], "ticker": sel["ticker"], "side": side,
                            "strength": sel["z"].abs().astype(float),
                            "detail": [f"abn{a:+.1f}% {'news' if n else 'no-news'}" for a, n in zip(sel["abn"], sel["has_news"])]})
        return out[EVENT_COLS].reset_index(drop=True)
