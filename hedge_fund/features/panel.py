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
