"""Alpha Vantage EARNINGS_ESTIMATES → ~/.hedge-fund/agent/estimates.db (free tier, shared 25/day quota).

One call returns a company's full quarterly/annual estimate history (MU: back to 2017): for each
fiscal period, the final consensus EPS and revenue, the consensus 7/30/60/90 days earlier, and
the count of upward/downward revisions over the trailing 7 and 30 days. That is a free,
quarter-snapshot history of estimate revisions — the large-cap signal S37 found missing.

Point-in-time caveat: the snapshot for a past quarter is the consensus shortly before that
quarter's report; "90 days ago" is relative to that snapshot. A backtest may use the revision
(final vs 90 days earlier) only from the report date on, never before.

Quota: shares av_quota.json with av_news (about 2 calls/day) and bin/congress.py (22 on
Saturdays). This takes at most `--max` calls and always leaves 3 for the news job.
Order: tickers never fetched, largest market cap first; then refreshes of the oldest fetch.

Usage: python -m agent.sources.av_estimates [--max 20] [--symbols MU,AAPL]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.parse
import urllib.request

import duckdb
import numpy as np
import pandas as pd

from agent.sources.av_news import URL, _CTX, _spend, api_key, budget_left
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

EST_DB = AGENT_DIR / "estimates.db"
KEEP_FOR_NEWS = 3
DDL = """CREATE TABLE IF NOT EXISTS av_estimates (
    ticker VARCHAR, fiscal_date DATE, horizon VARCHAR, eps_avg DOUBLE, eps_high DOUBLE, eps_low DOUBLE, n_analysts DOUBLE,
    eps_7d DOUBLE, eps_30d DOUBLE, eps_60d DOUBLE, eps_90d DOUBLE, up_7d DOUBLE, down_7d DOUBLE, up_30d DOUBLE, down_30d DOUBLE,
    rev_avg DOUBLE, rev_n_analysts DOUBLE, fetched_at TIMESTAMP, PRIMARY KEY (ticker, fiscal_date, horizon))"""
DDL_LOG = "CREATE TABLE IF NOT EXISTS av_estimates_log (ticker VARCHAR PRIMARY KEY, fetched_at TIMESTAMP, n_rows INT, status VARCHAR)"


def _f(x):
    try:
        return float(x) if x not in (None, "", "None") else np.nan
    except (TypeError, ValueError):
        return np.nan


def fetch_one(symbol: str) -> list[dict]:
    q = urllib.parse.urlencode({"function": "EARNINGS_ESTIMATES", "symbol": symbol, "apikey": api_key()})
    with urllib.request.urlopen(urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": "optradar-agent"}), timeout=60, context=_CTX) as r:
        d = json.load(r)
    if "estimates" not in d:
        raise RuntimeError(str(d)[:160])
    out = []
    for e in d["estimates"]:
        out.append({"ticker": symbol, "fiscal_date": e.get("date"), "horizon": e.get("horizon"),
                    "eps_avg": _f(e.get("eps_estimate_average")), "eps_high": _f(e.get("eps_estimate_high")),
                    "eps_low": _f(e.get("eps_estimate_low")), "n_analysts": _f(e.get("eps_estimate_analyst_count")),
                    "eps_7d": _f(e.get("eps_estimate_average_7_days_ago")), "eps_30d": _f(e.get("eps_estimate_average_30_days_ago")),
                    "eps_60d": _f(e.get("eps_estimate_average_60_days_ago")), "eps_90d": _f(e.get("eps_estimate_average_90_days_ago")),
                    "up_7d": _f(e.get("eps_estimate_revision_up_trailing_7_days")), "down_7d": _f(e.get("eps_estimate_revision_down_trailing_7_days")),
                    "up_30d": _f(e.get("eps_estimate_revision_up_trailing_30_days")), "down_30d": _f(e.get("eps_estimate_revision_down_trailing_30_days")),
                    "rev_avg": _f(e.get("revenue_estimate_average")), "rev_n_analysts": _f(e.get("revenue_estimate_analyst_count"))})
    return out


def queue(con) -> list[str]:
    """Never-fetched names by today's market cap (largest first), then the stalest fetches (> 80 days)."""
    with PanelStore(read_only=True) as store:
        rows = store.con.execute("""
            WITH px AS (SELECT ticker, close FROM bars WHERE trade_date = (SELECT max(trade_date) FROM bars)),
                 f AS (SELECT ticker, shares, row_number() OVER (PARTITION BY ticker ORDER BY filed DESC) rn FROM fundamentals_pit)
            SELECT px.ticker, px.close * f.shares AS mcap FROM px JOIN f ON f.ticker = px.ticker AND f.rn = 1
            WHERE f.shares > 1000 ORDER BY mcap DESC""").fetchall()
    done = dict(con.execute("SELECT ticker, fetched_at FROM av_estimates_log WHERE status = 'ok'").fetchall())
    fresh = [t for t, m in rows if m and m >= 3e8 and t not in done]
    stale_cut = pd.Timestamp.now() - pd.Timedelta(days=80)
    stale = sorted((t for t, ts in done.items() if pd.Timestamp(ts) < stale_cut), key=lambda t: done[t])
    return fresh + stale


def run(max_calls: int, symbols: list[str] | None = None) -> dict:
    con = duckdb.connect(str(EST_DB))
    con.execute(DDL)
    con.execute(DDL_LOG)
    todo = symbols or queue(con)
    allowed = min(max_calls, max(budget_left() - KEEP_FOR_NEWS, 0))
    stats = {"allowed": allowed, "queue": len(todo), "ok": 0, "failed": 0, "rows": 0}
    for sym in todo[:allowed]:
        try:
            rows = fetch_one(sym)
            _spend(1)
            if rows:
                df = pd.DataFrame(rows)
                df["fiscal_date"] = pd.to_datetime(df["fiscal_date"]).dt.date
                df["fetched_at"] = pd.Timestamp.now()
                con.register("_e", df)
                con.execute("INSERT OR REPLACE INTO av_estimates SELECT * FROM _e")
                con.unregister("_e")
            con.execute("INSERT OR REPLACE INTO av_estimates_log VALUES (?, ?, ?, 'ok')", [sym, pd.Timestamp.now(), len(rows)])
            stats["ok"] += 1
            stats["rows"] += len(rows)
        except Exception as exc:
            _spend(1)
            msg = str(exc).replace(api_key(), "***")[:80]                 # AV echoes the key in its quota message; never store it
            con.execute("INSERT OR REPLACE INTO av_estimates_log VALUES (?, ?, 0, ?)", [sym, pd.Timestamp.now(), f"error: {msg}"])
            stats["failed"] += 1
            if "rate limit" in str(exc).lower() or "premium" in str(exc).lower() or "25 requests" in str(exc):
                break
        time.sleep(1.2)
    stats["done_total"] = con.execute("SELECT count(*) FROM av_estimates_log WHERE status = 'ok'").fetchone()[0]
    con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--symbols", default=None)
    args = ap.parse_args()
    print(run(args.max, args.symbols.split(",") if args.symbols else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
