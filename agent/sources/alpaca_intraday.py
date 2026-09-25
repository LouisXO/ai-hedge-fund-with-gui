"""Intraday bars from Alpaca (free tier, SIP history older than 15 minutes) → ~/.hedge-fund/agent/intraday.db.

  bars30   30-minute bars, regular session only (09:30–15:30 ET bar starts), split-adjusted
  bars5    5-minute bars, regular session only, for the option names (S42's realized variance)

Alpaca's intraday bars include pre/after-hours; they are dropped here. Multi-symbol requests,
paginated; a load_log row per (table, ticker) makes reruns incremental. Rate limit on the free
tier is 200 requests/minute, so a full 30-minute history for ~3,000 names (2019 →) takes a few hours.

Usage:
  python -m agent.sources.alpaca_intraday --table bars30 --symbols RKLB,SPY --start 2025-09-01
  python -m agent.sources.alpaca_intraday --table bars30 --universe --start 2019-09-01
  python -m agent.sources.alpaca_intraday --table bars5 --options --start 2023-12-01
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import duckdb
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

INTRADAY_DB = AGENT_DIR / "intraday.db"
URL = "https://data.alpaca.markets/v2/stocks/bars"
TF = {"bars30": "30Min", "bars5": "5Min"}
DDL = {t: f"CREATE TABLE IF NOT EXISTS {t} (ticker VARCHAR, ts TIMESTAMP, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, v DOUBLE, n INT, PRIMARY KEY (ticker, ts))" for t in TF}
DDL_LOG = "CREATE TABLE IF NOT EXISTS load_log (tbl VARCHAR, ticker VARCHAR, through DATE, rows INT, PRIMARY KEY (tbl, ticker))"
BATCH = 25


def _headers() -> dict:
    env = dict(l.strip().split("=", 1) for l in open(AGENT_DIR.parent / ".env") if "=" in l and not l.startswith("#"))
    return {"APCA-API-KEY-ID": env["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET"]}


def fetch(symbols: list[str], timeframe: str, start: dt.date, end: dt.date, headers: dict) -> pd.DataFrame:
    end_ts = min(dt.datetime.combine(end, dt.time(23, 59), dt.timezone.utc), dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20))
    frames, token = [], None
    while True:
        q = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": f"{start}T00:00:00Z", "end": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "limit": 10000, "feed": "sip", "adjustment": "split", "sort": "asc"}
        if token:
            q["page_token"] = token
        for attempt in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(URL + "?" + urllib.parse.urlencode(q), headers=headers), timeout=90) as r:
                    d = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
        for sym, bars in (d.get("bars") or {}).items():
            if bars:
                f = pd.DataFrame(bars)
                f["ticker"] = sym
                frames.append(f)
        token = d.get("next_page_token")
        time.sleep(0.32)
        if not token:
            break
    if not frames:
        return pd.DataFrame(columns=["ticker", "ts", "o", "h", "l", "c", "v", "n"])
    df = pd.concat(frames, ignore_index=True)
    et = pd.to_datetime(df["t"]).dt.tz_convert("America/New_York")
    hm = et.dt.hour * 60 + et.dt.minute
    df = df[(hm >= 9 * 60 + 30) & (hm < 16 * 60)].copy()
    df["ts"] = et[df.index].dt.tz_localize(None)
    return df[["ticker", "ts", "o", "h", "l", "c", "v", "n"]]


def load(symbols: list[str], table: str, start: dt.date, end: dt.date | None = None, quiet: bool = False) -> dict:
    end = end or dt.date.today()
    headers = _headers()
    con = duckdb.connect(str(INTRADAY_DB))
    con.execute(DDL[table])
    con.execute(DDL_LOG)
    done = dict(con.execute("SELECT ticker, through FROM load_log WHERE tbl = ?", [table]).fetchall())
    todo = []
    for s in symbols:
        thr = done.get(s)
        s_start = start if thr is None else max(start, thr + dt.timedelta(days=1))
        if s_start <= end:
            todo.append((s, s_start))
    stats = {"table": table, "requested": len(symbols), "todo": len(todo), "rows": 0, "batches": 0}
    t0 = time.time()
    # group names that share the same start date so one request covers a batch
    for s_start in sorted({d for _, d in todo}):
        names = [s for s, d in todo if d == s_start]
        for i in range(0, len(names), BATCH):
            chunk = names[i:i + BATCH]
            try:
                df = fetch(chunk, TF[table], s_start, end, headers)
            except Exception as exc:
                print(f"  batch failed ({chunk[0]}…): {str(exc)[:80]}")
                continue
            if len(df):
                con.register("_b", df)
                con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM _b")
                con.unregister("_b")
            counts = df.groupby("ticker").size().to_dict() if len(df) else {}
            for s in chunk:
                con.execute("INSERT OR REPLACE INTO load_log VALUES (?, ?, ?, ?)", [table, s, end, int(counts.get(s, 0))])
            stats["rows"] += int(len(df))
            stats["batches"] += 1
            if not quiet and stats["batches"] % 10 == 0:
                print(f"  {table}: {stats['batches']} batches, {stats['rows']:,} rows [{time.time() - t0:.0f}s]", flush=True)
    con.close()
    stats["seconds"] = round(time.time() - t0)
    return stats


def universe() -> list[str]:
    with PanelStore(read_only=True) as store:
        return [r[0] for r in store.con.execute("""
            WITH w AS (SELECT ticker, avg(close * volume) adv FROM bars WHERE trade_date >= current_date - 30 GROUP BY 1)
            SELECT ticker FROM w WHERE adv >= 5e6 ORDER BY adv DESC""").fetchall()]


def option_names() -> list[str]:
    from agent.sources.alpaca_options import OPTIONS_DB
    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    out = [r[0] for r in con.execute("SELECT DISTINCT underlying FROM opt_contracts ORDER BY 1").fetchall()]
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=list(TF), required=True)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--options", action="store_true")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    syms = args.symbols.split(",") if args.symbols else universe() if args.universe else option_names() if args.options else []
    if "SPY" not in syms:
        syms = ["SPY"] + syms
    print(load(syms, args.table, dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end) if args.end else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
