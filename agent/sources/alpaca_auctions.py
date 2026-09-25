"""Opening / closing auction prints from Alpaca's historical auctions endpoint → ~/.hedge-fund/agent/auctions.db.

`/v2/stocks/auctions` (feed=sip) is free for data older than 15 minutes (verified 2026-09-24).
Each day has opening prints `o` and closing prints `c`. The primary listing exchange's cross is the
print with condition "O" (opening) / "6" (closing); "Q" / "M" are per-venue official open / close
markers and can be a 1-share print on another venue (2026-09-24: MU's NYSE Arca "Q" was 1 share
while the Nasdaq cross was 151,103), so they are not used. We keep the cross price and size per
(ticker, day), which answers two
execution questions the paper simulator cannot:
  - what a real market-on-open order would have paid (the cross price), and
  - how large our order is relative to the auction (participation = our $ / cross $).

Separate DuckDB file so a long backfill never locks panel.db.

Usage: python -m agent.sources.alpaca_auctions --symbols EDRY,ATEX --start 2025-09-01 [--end 2026-08-31]
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

from hedge_fund.paths import AGENT_DIR

AUCTIONS_DB = AGENT_DIR / "auctions.db"
URL = "https://data.alpaca.markets/v2/stocks/auctions"
DDL = """CREATE TABLE IF NOT EXISTS auctions (ticker VARCHAR, day DATE, open_px DOUBLE, open_size DOUBLE,
         close_px DOUBLE, close_size DOUBLE, fetched_at TIMESTAMP, PRIMARY KEY (ticker, day))"""


def _headers() -> dict:
    env = dict(l.strip().split("=", 1) for l in open(AGENT_DIR.parent / ".env") if "=" in l and not l.startswith("#"))
    return {"APCA-API-KEY-ID": env["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET"]}


def fetch(symbols: list[str], start: dt.date, end: dt.date, headers: dict) -> list[dict]:
    """All auction days for `symbols` in [start, end]; the end time is clipped to 20 minutes ago (free plan: SIP >= 15 min old)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    end_ts = min(dt.datetime.combine(end, dt.time(23, 0), dt.timezone.utc), cutoff)
    rows, token = [], None
    while True:
        q = {"symbols": ",".join(symbols), "start": f"{start}T00:00:00Z", "end": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "feed": "sip", "limit": 10000}
        if token:
            q["page_token"] = token
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(URL + "?" + urllib.parse.urlencode(q), headers=headers), timeout=60) as r:
                    d = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        for sym, days in (d.get("auctions") or {}).items():
            for day in days:
                op = sorted((x for x in (day.get("o") or []) if x.get("c") == "O"), key=lambda x: -(x.get("s") or 0))   # the primary cross print
                cl = sorted((x for x in (day.get("c") or []) if x.get("c") == "6"), key=lambda x: -(x.get("s") or 0))
                rows.append({"ticker": sym, "day": day["d"],
                             "open_px": op[0]["p"] if op else None, "open_size": op[0].get("s") if op else None,
                             "close_px": cl[0]["p"] if cl else None, "close_size": cl[0].get("s") if cl else None})
        token = d.get("next_page_token")
        if not token:
            return rows


def load(symbols: list[str], start: dt.date, end: dt.date, batch: int = 40) -> int:
    headers = _headers()
    con = duckdb.connect(str(AUCTIONS_DB))
    con.execute(DDL)
    n = 0
    for i in range(0, len(symbols), batch):
        rows = fetch(symbols[i:i + batch], start, end, headers)
        if rows:
            df = pd.DataFrame(rows)
            df["day"] = pd.to_datetime(df["day"]).dt.date
            df["fetched_at"] = pd.Timestamp.now()
            con.register("_a", df)
            con.execute("INSERT OR REPLACE INTO auctions SELECT ticker, day, open_px, open_size, close_px, close_size, fetched_at FROM _a")
            con.unregister("_a")
            n += len(df)
        time.sleep(0.3)
    con.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    print(load(args.symbols.split(","), dt.date.fromisoformat(args.start), end), "rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
