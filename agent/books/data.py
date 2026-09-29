"""Shared inputs for the books: prices, tradable mask, spreads, insider flows.

All frames are wide (date x ticker). `tradable[t, d]` is the only universe
definition: listed that day (point-in-time) AND 20-day dollar volume inside
the book's [floor, ceiling]. Nothing else is allowed to select names.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from agent.books import segments
from agent.s11_insider_wide import listed_mask
from hedge_fund.features.panel import PanelStore

BUCKETS = [-np.inf, 3e6, 2e7, 1e8, np.inf]
LABELS = ["micro", "small", "mid", "large"]
MAX_TX_USD = 50e6          # a single open-market insider trade above this is a parsing error, not a signal
OVERRIDE_DAYS = 120        # a share-count override reaches rows filed up to this many days before its as_of, none earlier or after
SPLIT_MOVE = 1.4           # adj_close/close moving more than this between a row's filing and the override's as_of: a split between
OVERRIDE_TABLES = ("shares_override", "shares_override_log")   # the current row per ticker, and every row it ever had
# Multi-class companies whose moomoo count is only the listed classes while the others carry the same
# economics (S47b, 2026-09-29): their moomoo rows are ignored here and they are not fetched; XBRL's total stands.
MOOMOO_KEEP_OURS = {
    "CWEN": "moomoo 121.2M = listed classes A + C; B and D (held by the sponsor, same economics) make the XBRL total 205.3M",
}


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
    # S47b item 2: a reused ticker's bars before its current listing are another security's ('SE@2007')
    splits = segments.load_splits(store)
    f = segments.split_bars({"close": close, "adj": adj, "open": opn, "vol": vol}, splits)
    close, adj, opn, vol = f["close"], f["adj"], f["open"], f["vol"]
    adj_open = opn * (adj / close)
    adv20 = (close * vol).rolling(20).mean()
    listed = listed_mask(store, adj.index, list(adj.columns), splits=splits)
    pseudo = [c for c in listed.columns if segments.is_pseudo(c)]
    if len(listed) and pseudo and listed.iloc[-1][pseudo].any():     # live lists are built on the last bar
        raise RuntimeError(f"pseudo tickers listed on {listed.index[-1].date()}: "
                           f"{listed.iloc[-1][pseudo][lambda s: s].index.tolist()}")
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
    df = segments.split_fundamentals(df, segments.load_splits(store))   # S47b item 2: filed before a reused ticker's listing
    df["filed"] = pd.to_datetime(df["filed"])
    # shares_override: a few names report every share count per class (V, BRK.B, STZ, ERIE), which the
    # XBRL feed cannot see; their current count from yfinance stands in (not point-in-time — a share
    # count moves a few % a year, the price is what moves the ratio). Audit S28, 2026-09-22.
    df.loc[df["shares"] <= 1000, "shares"] = np.nan             # 0 / 1 / negative counts are XBRL noise (FOX, HOOD, EL...)
    # S47b 1 (replaces S47 补充 5's 400 days from the newest filing): only where the count is missing or
    # flagged (shares_flag not empty), and only from an override whose as_of is 0..OVERRIDE_DAYS after the
    # row's filing — see apply_share_overrides. Every other row, and every unflagged count, is as built.
    try:
        df = apply_share_overrides(store, df)
    except Exception:
        pass
    return df


def _tables(store: PanelStore) -> set[str]:
    # looked up, not tried: a failed query would abort the caller's open transaction
    return set(store.con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])


def share_overrides(store: PanelStore) -> pd.DataFrame:
    """ticker, as_of, shares of every override: shares_override (the current row per ticker) and
    shares_override_log (every row agent.sources.moomoo_shares wrote, and the rows it found there). On the
    same (ticker, as_of) the shares_override row wins: a row fixed by hand there is what stands."""
    have = _tables(store)
    keep = ", ".join(f"'{t}'" for t in MOOMOO_KEEP_OURS) or "''"

    def rows(t: str) -> pd.DataFrame:
        cols = set(store.con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", [t]).df()["column_name"])
        where = f" WHERE NOT (source = 'moomoo_snapshot' AND ticker IN ({keep}))" if "source" in cols else ""
        return store.con.execute(f"SELECT ticker, CAST(as_of AS DATE) AS as_of, shares FROM {t}{where}").df()
    parts = [rows(t) for t in OVERRIDE_TABLES if t in have]
    cols = ["ticker", "as_of", "shares"]
    ov = pd.concat(parts, ignore_index=True)[cols] if parts else pd.DataFrame(columns=cols)
    ov["as_of"] = pd.to_datetime(ov["as_of"]).astype("datetime64[ns]")
    ov["shares"] = pd.to_numeric(ov["shares"], errors="coerce")
    return ov.dropna(subset=["as_of", "shares"]).drop_duplicates(["ticker", "as_of"], keep="first")


def apply_share_overrides(store: PanelStore, df: pd.DataFrame) -> pd.DataFrame:
    """Fundamentals rows whose count is missing or flagged take the EARLIEST override with as_of in
    [filed, filed + OVERRIDE_DAYS] (S47b 1); shares_asof becomes that as_of.

    Point in time: a backtest day on such a row sees a count at most OVERRIDE_DAYS newer than the row's
    filing; with the daily fetch the first snapshot after a filing is a day or two after it. The first
    snapshots (2026-09-22 yfinance, 2026-09-29 onward moomoo) reach back to rows filed from 2026-05-25 /
    2026-06-01; earlier rows, and rows with no override in their window (a 20-F filed in April, V's row
    of 2026-04-29), stay as built. Earliest, not latest: once a row has taken an override it keeps it as
    later snapshots arrive (a 20-F row is used for 200 days, not 120), and a list recomputed later — the
    drift check, the evaluation's same-window backtest — sees the count the live list saw.

    Splits: close is not split-adjusted, so a count taken after a split, on a row filed before it, gives
    close x count off by the split ratio on the days before the split (a reverse split makes the name look
    that many times cheaper). Where adj_close/close on the last bar at or before the as_of differs from
    the one at or before the filing by more than SPLIT_MOVE either way, the row is left as built
    (CTNT: 1:150 reverse split on 2026-09-28, row filed 2026-08-13)."""
    ov = share_overrides(store)
    flagged = (df["shares_flag"].fillna("").ne("") if "shares_flag" in df
               else pd.Series(False, index=df.index))
    need = df.index[(df["shares"].isna() | flagged) & df["ticker"].isin(set(ov["ticker"]))]
    if not len(need):
        return df
    left = (df.loc[need, ["ticker", "filed"]].assign(filed=lambda x: x["filed"].astype("datetime64[ns]"))
            .rename_axis("row").reset_index().sort_values("filed"))
    hit = pd.merge_asof(left, ov[["ticker", "as_of", "shares"]].sort_values("as_of"), left_on="filed",
                        right_on="as_of", by="ticker", direction="forward",
                        tolerance=pd.Timedelta(days=OVERRIDE_DAYS)).dropna(subset=["shares"])
    move = _factor_move(store, hit)
    hit = hit[~((move > SPLIT_MOVE) | (move < 1 / SPLIT_MOVE))]
    df.loc[hit["row"].to_numpy(), "shares"] = hit["shares"].to_numpy()
    if "shares_asof" in df:
        df["shares_asof"] = pd.to_datetime(df["shares_asof"])
        df.loc[hit["row"].to_numpy(), "shares_asof"] = hit["as_of"].to_numpy()
    return df


def _factor_move(store: PanelStore, hit: pd.DataFrame) -> pd.Series:
    """Per row of hit: adj_close/close on the last bar at or before filed over the same at or before as_of.
    NaN where either bar is missing (then nothing is skipped: without a bar the name is not scored)."""
    out = pd.Series(np.nan, index=hit.index)
    if hit.empty or "bars" not in _tables(store):
        return out
    start = (hit["filed"].min() - pd.Timedelta(days=10)).date().isoformat()
    bars = store.con.execute(
        "SELECT ticker, CAST(trade_date AS DATE) AS d, adj_close / close AS k FROM bars "
        "WHERE trade_date >= CAST(? AS DATE) AND close > 0 AND adj_close > 0 "
        "AND list_contains(?, ticker)", [start, sorted(hit["ticker"].unique())]).df()
    if bars.empty:
        return out
    bars = bars.assign(d=pd.to_datetime(bars["d"]).astype("datetime64[ns]")).sort_values("d")

    def k_at(col):
        x = hit[["ticker", col]].rename_axis("i").reset_index().sort_values(col)
        got = pd.merge_asof(x, bars, left_on=col, right_on="d", by="ticker", direction="backward")
        return got.set_index("i")["k"].reindex(hit.index)
    return k_at("filed") / k_at("as_of")


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
    return segments.split_events(df, segments.load_splits(store))   # S47b item 2: the old security's filings, its pseudo ticker
