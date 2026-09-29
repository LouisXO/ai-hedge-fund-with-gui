"""PanelStore — the research panel (daily bars + point-in-time index membership).

One DuckDB file (~/.hedge-fund/agent/panel.db), written only by the agent's
own jobs, so it never contends with optradar.db's writer lock.

Price conventions:
- bars.close/open/high/low are split-adjusted (Yahoo's raw columns);
  bars.adj_close is split+dividend adjusted. Returns and price signals use
  adj_close; ranges/realized vol use OHLC. adj_open = open * adj_close/close.
- Every row records `source` and `fetched_at` (provenance).

Membership is stored as the dated change rows of the source file: the
members on day D are those listed on the latest eff_date <= D.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from hedge_fund.paths import PANEL_DB

# One row per listing of a symbol, not per symbol: a ticker can be used by two companies in turn (SNDK
# 1995-2016 and 2025-), and the vendor can carry an Active and a Delisted row for the same company (OKE).
# ipo_date is part of the key so that every such segment is kept. Until 2026-09-28 the key was
# (symbol, status); PanelStore.migrate_listing_key converts a table created before that.
LISTING_KEY = ["symbol", "status", "ipo_date"]
LISTING_DDL = """CREATE TABLE IF NOT EXISTS {name} (
        symbol VARCHAR, name VARCHAR, exchange VARCHAR, asset_type VARCHAR,
        ipo_date DATE, delisting_date DATE, status VARCHAR, fetched_at TIMESTAMP,
        PRIMARY KEY (symbol, status, ipo_date))"""
LISTING_COLS = ["symbol", "name", "exchange", "asset_type", "ipo_date", "delisting_date", "status", "fetched_at"]
NO_IPO_DATE = "1900-01-01"          # a key column cannot be NULL; the listing mask reads this as "from the first bar"
MIN_ACTIVE_SHARE = 0.9              # a fresh active list smaller than this share of the last one is a broken download

DDL = [
    """CREATE TABLE IF NOT EXISTS bars (
        ticker VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, adj_close DOUBLE, volume DOUBLE, source VARCHAR,
        fetched_at TIMESTAMP, PRIMARY KEY (ticker, trade_date))""",
    """CREATE TABLE IF NOT EXISTS index_daily (
        symbol VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, adj_close DOUBLE, source VARCHAR, fetched_at TIMESTAMP,
        PRIMARY KEY (symbol, trade_date))""",
    """CREATE TABLE IF NOT EXISTS membership (
        index_name VARCHAR, eff_date DATE, ticker VARCHAR,
        PRIMARY KEY (index_name, eff_date, ticker))""",
    """CREATE TABLE IF NOT EXISTS chain_daily (
        snap_date DATE, ticker VARCHAR, expiry DATE, strike DOUBLE, "right" VARCHAR,
        bid DOUBLE, ask DOUBLE, last DOUBLE, volume DOUBLE, oi DOUBLE, iv DOUBLE,
        delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE, spot DOUBLE, dte INT,
        snap_ts TIMESTAMP, source VARCHAR,
        PRIMARY KEY (snap_date, ticker, expiry, strike, "right"))""",
    """CREATE TABLE IF NOT EXISTS insider_tx (
        accession VARCHAR, ticker VARCHAR, issuer_cik VARCHAR, filing_date DATE, trans_date DATE,
        owner_name VARCHAR, relationship VARCHAR, officer_title VARCHAR, trans_code VARCHAR,
        acq_disp VARCHAR, shares DOUBLE, price DOUBLE, value_usd DOUBLE, shares_after DOUBLE,
        source VARCHAR, fetched_at TIMESTAMP,
        PRIMARY KEY (accession, ticker, trans_date, trans_code, shares, price))""",
    LISTING_DDL.format(name="listing_status"),
    """CREATE TABLE IF NOT EXISTS issuer_seen (
        ticker VARCHAR, cik VARCHAR, quarter VARCHAR, n_filings INT, first_filing DATE, last_filing DATE,
        PRIMARY KEY (ticker, cik, quarter))""",
    """CREATE TABLE IF NOT EXISTS insider_load_log (
        quarter VARCHAR PRIMARY KEY, n_rows INT, n_tickers INT, loaded_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS news_sentiment (
        article_id VARCHAR, time_published TIMESTAMP, ticker VARCHAR, relevance DOUBLE,
        sentiment DOUBLE, overall_sentiment DOUBLE, source_domain VARCHAR, fetched_at TIMESTAMP,
        PRIMARY KEY (article_id, ticker))""",
    """CREATE TABLE IF NOT EXISTS news_fetch_log (
        window_from TIMESTAMP, window_to TIMESTAMP, n_articles INT, n_rows INT, truncated BOOLEAN,
        fetched_at TIMESTAMP, PRIMARY KEY (window_from, window_to))""",
    """CREATE TABLE IF NOT EXISTS fetch_log (
        ticker VARCHAR, data_ticker VARCHAR, source VARCHAR, status VARCHAR,
        n_rows INT, first_date DATE, last_date DATE, note VARCHAR,
        fetched_at TIMESTAMP, PRIMARY KEY (ticker, source))""",
]

BAR_COLS = ["open", "high", "low", "close", "adj_close", "volume"]


class PanelStore:
    def __init__(self, path: Path | str = PANEL_DB, read_only: bool = False) -> None:
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            for stmt in DDL:
                self.con.execute(stmt)

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- writes ---------------------------------------------------------

    def upsert_bars(self, df: pd.DataFrame) -> int:
        """df columns: ticker, trade_date, open..volume, source, fetched_at."""
        if df.empty:
            return 0
        self.con.register("_bars_in", df)
        self.con.execute("INSERT OR REPLACE INTO bars SELECT ticker, trade_date, open, high, low, close, "
                         "adj_close, volume, source, fetched_at FROM _bars_in")
        self.con.unregister("_bars_in")
        return len(df)

    def upsert_index(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self.con.register("_idx_in", df)
        self.con.execute("INSERT OR REPLACE INTO index_daily SELECT symbol, trade_date, open, high, low, close, "
                         "adj_close, source, fetched_at FROM _idx_in")
        self.con.unregister("_idx_in")
        return len(df)

    def replace_membership(self, index_name: str, rows: list[tuple[str, list[str]]]) -> int:
        """rows: [(eff_date 'YYYY-MM-DD', [tickers])] — the full change history."""
        flat = pd.DataFrame([(index_name, d, t) for d, ts in rows for t in ts],
                            columns=["index_name", "eff_date", "ticker"]).drop_duplicates()
        flat["eff_date"] = pd.to_datetime(flat["eff_date"]).dt.date
        self.con.execute("DELETE FROM membership WHERE index_name = ?", [index_name])
        self.con.register("_mem_in", flat)
        self.con.execute("INSERT INTO membership SELECT * FROM _mem_in")
        self.con.unregister("_mem_in")
        return len(flat)

    def insert(self, table: str, df: pd.DataFrame) -> int:
        """Generic INSERT OR REPLACE, aligning by column name."""
        if df.empty:
            return 0
        cols = [r[0] for r in self.con.execute(f"DESCRIBE {table}").fetchall()]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        self.con.register("_gen_in", df[cols])
        self.con.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM _gen_in')
        self.con.unregister("_gen_in")
        return len(df)

    def _listing_count(self, name: str) -> int:
        return self.con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]

    def _listing_key(self) -> list[str]:
        row = self.con.execute("""SELECT constraint_column_names FROM duckdb_constraints()
                                  WHERE table_name = 'listing_status' AND constraint_type = 'PRIMARY KEY'""").fetchone()
        return list(row[0]) if row else []

    def migrate_listing_key(self) -> dict:
        """listing_status keyed by (symbol, status) -> keyed by (symbol, status, ipo_date), in place.

        DuckDB cannot alter a primary key, so: create the new table, copy, compare the row counts, drop the
        old one, rename — all in one transaction, so a failure at any step leaves the old table untouched.
        Safe to run again: a table that already has the new key is left alone.
        """
        n = self._listing_count("listing_status")
        if self._listing_key() == LISTING_KEY:
            return {"migrated": False, "rows_before": n, "rows_after": n}
        self.con.execute("BEGIN")
        try:
            self.con.execute("DROP TABLE IF EXISTS listing_status_new")        # left by nothing we commit; be sure
            self.con.execute(LISTING_DDL.format(name="listing_status_new"))
            self.con.execute(f"""INSERT INTO listing_status_new
                                 SELECT * REPLACE (coalesce(ipo_date, DATE '{NO_IPO_DATE}') AS ipo_date)
                                 FROM (SELECT {', '.join(LISTING_COLS)} FROM listing_status)""")
            after = self._listing_count("listing_status_new")
            if after != n:
                raise RuntimeError(f"listing_status migration: row count {n} -> {after}, nothing changed")
            self.con.execute("DROP TABLE listing_status")
            self.con.execute("ALTER TABLE listing_status_new RENAME TO listing_status")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        if self._listing_key() != LISTING_KEY or self._listing_count("listing_status") != n:
            raise RuntimeError("listing_status migration: the table after the swap is not the one that was copied")
        return {"migrated": True, "rows_before": n, "rows_after": n}

    def close_stale_listings(self, fresh_at=None) -> dict:
        """Active rows the vendor's active list no longer carries (fetched before `fresh_at`, default the
        latest Active fetch).

        If a Delisted row of the same symbol ends on or after the row's ipo_date, the vendor has said when
        that listing ended: the Active row is removed and the Delisted row carries the interval. Without
        one the row stays as it is (still open-ended) and is reported, because a name the vendor merely
        dropped from its list may be alive and held.
        """
        if fresh_at is None:
            fresh_at = self.con.execute("SELECT max(fetched_at) FROM listing_status WHERE status = 'Active'").fetchone()[0]
        stale = self.con.execute("""
            SELECT a.symbol, a.ipo_date,
                   EXISTS (SELECT 1 FROM listing_status d WHERE d.symbol = a.symbol AND d.status <> 'Active'
                           AND d.delisting_date >= a.ipo_date) AS ended
            FROM listing_status a WHERE a.status = 'Active' AND a.fetched_at < ? ORDER BY 1, 2""", [fresh_at]).df()
        ended = stale[stale["ended"]]
        for sym, ipo in zip(ended["symbol"], ended["ipo_date"]):
            self.con.execute("DELETE FROM listing_status WHERE symbol = ? AND status = 'Active' AND ipo_date = ?",
                             [sym, pd.Timestamp(ipo).date()])
        return {"active_closed": sorted(set(ended["symbol"])),
                "active_missing": sorted(set(stale.loc[~stale["ended"], "symbol"]))}

    def write_listing(self, active: pd.DataFrame, delisted: pd.DataFrame) -> dict:
        """One refresh from the vendor's two lists. Adds to a symbol's history, never replaces it.

        Everything happens in one transaction: the table as it was is copied to listing_status_prev (the
        audit compares the two), the rows are upserted by (symbol, status, ipo_date), and Active rows that
        left the active list are closed (close_stale_listings). A fresh active list much shorter than the
        last one is refused: with the mask a union of intervals, the active list is what keeps a name in
        the universe, and a truncated download must not be read as 2,000 delistings.
        """
        self.migrate_listing_key()
        df = pd.concat([active, delisted], ignore_index=True)
        for c in LISTING_COLS:
            if c not in df.columns:
                df[c] = None
        df = df[LISTING_COLS].copy()
        df["ipo_date"] = pd.to_datetime(df["ipo_date"]).fillna(pd.Timestamp(NO_IPO_DATE)).dt.date
        # the same listing twice in one download: keep the one that ends last (INSERT OR REPLACE would keep the first)
        df = (df.assign(_end=pd.to_datetime(df["delisting_date"])).sort_values("_end", na_position="last", kind="stable")
                .drop_duplicates(LISTING_KEY, keep="last").drop(columns="_end").sort_values(LISTING_KEY))
        had = self.con.execute("""SELECT count(*) FROM listing_status WHERE status = 'Active' AND fetched_at =
                                  (SELECT max(fetched_at) FROM listing_status WHERE status = 'Active')""").fetchone()[0]
        n_active = int((df["status"] == "Active").sum())
        if n_active < MIN_ACTIVE_SHARE * had:
            raise RuntimeError(f"listing refresh refused: the active list has {n_active} rows, the last one had {had}")
        before = self._listing_count("listing_status")
        self.con.execute("BEGIN")
        try:
            self.con.execute("CREATE OR REPLACE TABLE listing_status_prev AS SELECT * FROM listing_status")
            self.con.register("_ls_in", df)
            new = self.con.execute("SELECT count(*) FROM _ls_in i ANTI JOIN listing_status l USING (symbol, status, ipo_date)").fetchone()[0]
            self.con.execute(f"INSERT OR REPLACE INTO listing_status SELECT {', '.join(LISTING_COLS)} FROM _ls_in")
            self.con.unregister("_ls_in")
            if self._listing_count("listing_status") != before + new:       # a refresh only adds rows or updates them
                raise RuntimeError(f"listing refresh: {before} rows + {new} new != {self._listing_count('listing_status')}")
            out = self.close_stale_listings(df.loc[df["status"] == "Active", "fetched_at"].max())
            after = self._listing_count("listing_status")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return {"active": n_active, "delisted": int(len(df) - n_active), "rows_before": before, "rows_after": after,
                "new_segments": int(new), **out}

    def log_fetch(self, rows: list[dict]) -> None:
        if not rows:
            return
        df = pd.DataFrame(rows)
        self.con.register("_log_in", df)
        self.con.execute("INSERT OR REPLACE INTO fetch_log SELECT ticker, data_ticker, source, status, n_rows, "
                         "first_date, last_date, note, fetched_at FROM _log_in")
        self.con.unregister("_log_in")

    # -- reads ----------------------------------------------------------

    def membership_changes(self, index_name: str = "sp500") -> list[tuple[pd.Timestamp, frozenset[str]]]:
        df = self.con.execute("SELECT eff_date, ticker FROM membership WHERE index_name = ? ORDER BY eff_date",
                              [index_name]).df()
        return [(pd.Timestamp(d), frozenset(g["ticker"])) for d, g in df.groupby("eff_date")]

    def membership_mask(self, dates: pd.DatetimeIndex, tickers: list[str],
                        index_name: str = "sp500") -> pd.DataFrame:
        """Boolean date x ticker frame: True where the ticker was a member that day."""
        changes = self.membership_changes(index_name)
        eff = pd.DatetimeIndex([d for d, _ in changes])
        pos = eff.searchsorted(dates, side="right") - 1
        col = {t: i for i, t in enumerate(tickers)}
        out = pd.DataFrame(False, index=dates, columns=tickers)
        arr = out.to_numpy()
        for row, p in enumerate(pos):
            if p < 0:
                continue
            for t in changes[p][1]:
                j = col.get(t)
                if j is not None:
                    arr[row, j] = True
        return pd.DataFrame(arr, index=dates, columns=tickers)

    def bars_wide(self, field: str = "adj_close", start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
        if field not in BAR_COLS:
            raise ValueError(f"unknown field {field}")
        q = f"SELECT trade_date, ticker, {field} AS v FROM bars WHERE 1=1"
        params: list = []
        if start:
            q += " AND trade_date >= ?"
            params.append(start)
        if end:
            q += " AND trade_date <= ?"
            params.append(end)
        df = self.con.execute(q, params).df()
        wide = df.pivot(index="trade_date", columns="ticker", values="v")
        wide.index = pd.to_datetime(wide.index)
        return wide.sort_index()

    def index_series(self, symbol: str, field: str = "adj_close") -> pd.Series:
        df = self.con.execute(f"SELECT trade_date, {field} FROM index_daily WHERE symbol = ? ORDER BY trade_date",
                              [symbol]).df()
        return pd.Series(df[field].to_numpy(), index=pd.to_datetime(df["trade_date"]), name=symbol)
