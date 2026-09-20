"""Agent tables inside optradar.db — signals, shadow picks, realized returns.

Satellite-script pattern, as bin/congress.py does: this module owns its own
DDL against the same DuckDB file, and never touches optradar's tables. The
connection is held read-write for a second or two at the end of a run, with
a retry, because a DuckDB writer locks the file for everyone else.

Shadow mode: picks are recorded with status='shadow' and no paper_ledger
row is opened. Nothing has passed validation (S3), so the agent earns its
scoreboard on stock-level returns first. `open_agent_positions` exists for
the day a signal does pass; it writes source='agent' rows shaped exactly
like optradar's own, so mark()/scoreboard() pick them up unchanged.
"""
from __future__ import annotations

import json
import time

import duckdb
import pandas as pd

OPTRADAR_DB = "/Users/louis/optradar/optradar.db"

DDL = [
    """CREATE TABLE IF NOT EXISTS agent_runs (
        run_id VARCHAR PRIMARY KEY, as_of DATE, run_ts TIMESTAMP, git_sha VARCHAR, mode VARCHAR,
        universe_n INT, n_signals INT, n_validated INT, vix DOUBLE, regime VARCHAR,
        n_picks INT, n_opened INT, status VARCHAR, error VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS agent_signals (
        as_of DATE, ticker VARCHAR, signal_name VARCHAR, signal_version VARCHAR, value DOUBLE,
        components VARCHAR, abstained BOOLEAN, validated BOOLEAN, run_id VARCHAR,
        PRIMARY KEY (as_of, ticker, signal_name))""",
    """CREATE TABLE IF NOT EXISTS agent_picks (
        as_of DATE, ticker VARCHAR, signal_name VARCHAR, side VARCHAR, rank INT, value DOUBLE,
        iv DOUBLE, rv20 DOUBLE, rv60 DOUBLE, breakeven_pct DOUBLE, gate_passed BOOLEAN, gate_reason VARCHAR,
        status VARCHAR, ledger_id VARCHAR, run_id VARCHAR, limit_ref DOUBLE, spread_pct DOUBLE,
        expected_net_pct DOUBLE, instrument VARCHAR,
        PRIMARY KEY (as_of, ticker, signal_name, side))""",
    """CREATE TABLE IF NOT EXISTS agent_returns (
        as_of DATE, ticker VARCHAR, entry_px DOUBLE, ret1 DOUBLE, ret5 DOUBLE, ret10 DOUBLE, ret20 DOUBLE,
        spy_ret5 DOUBLE, spy_ret10 DOUBLE, source VARCHAR, filled_at TIMESTAMP,
        PRIMARY KEY (as_of, ticker))""",
]


def connect(path: str = OPTRADAR_DB, read_only: bool = False, tries: int = 6, wait: float = 10.0):
    """DuckDB holds an exclusive lock per writer; a manual rerun may overlap the 08:41 job."""
    for attempt in range(tries):
        try:
            return duckdb.connect(path, read_only=read_only)
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == tries - 1:
                raise
            time.sleep(wait)
    raise RuntimeError("unreachable")


# columns added after the first deployment; DuckDB has no migration tool
_ADD_COLUMNS = [("agent_picks", "limit_ref", "DOUBLE"), ("agent_picks", "spread_pct", "DOUBLE"),
                ("agent_picks", "expected_net_pct", "DOUBLE"), ("agent_picks", "instrument", "VARCHAR")]


def ensure_schema(con) -> None:
    for stmt in DDL:
        con.execute(stmt)
    for table, col, typ in _ADD_COLUMNS:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            pass                      # already present


def _insert(con, table: str, df: pd.DataFrame) -> int:
    """Insert by column NAME: callers build dicts, and dict order is not schema order."""
    if df.empty:
        return 0
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    missing = [c for c in cols if c not in df.columns]
    for c in missing:
        df[c] = None
    con.register("_in", df[cols])
    con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM _in")
    con.unregister("_in")
    return len(df)


def write_run(con, row: dict) -> None:
    _insert(con, "agent_runs", pd.DataFrame([row]))


def write_signals(con, as_of, run_id: str, frames: dict[str, pd.DataFrame], validated: set[str]) -> int:
    """frames: {signal_name: DataFrame(index=ticker, columns=[value, *components])}."""
    rows = []
    for name, df in frames.items():
        for ticker, r in df.iterrows():
            comp = {k: (None if pd.isna(v) else float(v)) for k, v in r.items() if k != "value"}
            rows.append({"as_of": as_of, "ticker": ticker, "signal_name": name, "signal_version": "1",
                         "value": None if pd.isna(r["value"]) else float(r["value"]),
                         "components": json.dumps(comp), "abstained": bool(pd.isna(r["value"])),
                         "validated": name in validated, "run_id": run_id})
    return _insert(con, "agent_signals", pd.DataFrame(rows))


def write_picks(con, rows: list[dict]) -> int:
    return _insert(con, "agent_picks", pd.DataFrame(rows))


def fill_returns(con, panel_close: pd.DataFrame, panel_open: pd.DataFrame, spy: pd.Series) -> int:
    """Entry at the open after the signal date; fill 1/5/10/20-day returns once available."""
    todo = con.execute("""SELECT DISTINCT s.as_of, s.ticker FROM agent_signals s
                          LEFT JOIN agent_returns r ON r.as_of = s.as_of AND r.ticker = s.ticker
                          WHERE r.ticker IS NULL OR r.ret20 IS NULL""").df()
    if todo.empty:
        return 0
    idx = panel_close.index
    rows = []
    for as_of, ticker in todo.itertuples(index=False):
        d = pd.Timestamp(as_of)
        if d not in idx or ticker not in panel_close.columns:
            continue
        i = idx.get_loc(d)
        if i + 1 >= len(idx):
            continue
        entry = panel_open.iat[i + 1, panel_open.columns.get_loc(ticker)]
        if not pd.notna(entry) or entry <= 0:
            continue
        col = panel_close.columns.get_loc(ticker)

        def ret(h):
            j = i + h
            if j >= len(idx):
                return None
            v = panel_close.iat[j, col]
            return None if pd.isna(v) else float(v / entry - 1) * 100

        def sret(h):
            j = i + h
            if j >= len(idx) or idx[i + 1] not in spy.index:
                return None
            base = spy.iloc[i + 1]
            return None if pd.isna(spy.iloc[j]) or pd.isna(base) else float(spy.iloc[j] / base - 1) * 100

        rows.append({"as_of": d.date(), "ticker": ticker, "entry_px": float(entry),
                     "ret1": ret(1), "ret5": ret(5), "ret10": ret(10), "ret20": ret(20),
                     "spy_ret5": sret(5), "spy_ret10": sret(10), "source": "yfinance",
                     "filled_at": pd.Timestamp.now()})
    return _insert(con, "agent_returns", pd.DataFrame(rows))


def live_summary(con, horizon: str = "ret10") -> dict:
    """Realized stock-level scoreboard of the agent's shadow picks."""
    q = f"""SELECT p.side, count(*) n, avg(r.{horizon}) avg_ret,
                   avg(CASE WHEN (p.side IN ('C','L') AND r.{horizon}>0) OR (p.side='P' AND r.{horizon}<0)
                            THEN 1.0 ELSE 0.0 END) dir_hit
            FROM agent_picks p JOIN agent_returns r ON r.as_of=p.as_of AND r.ticker=p.ticker
            WHERE r.{horizon} IS NOT NULL GROUP BY 1"""
    rows = con.execute(q).df().to_dict("records")
    n_sig = con.execute("SELECT count(*) FROM agent_signals").fetchone()[0]
    days = con.execute("SELECT count(DISTINCT as_of) FROM agent_signals").fetchone()[0]
    return {"by_side": rows, "n_signal_rows": int(n_sig), "n_days": int(days), "horizon": horizon}
