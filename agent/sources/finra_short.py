"""FINRA consolidated short interest (exchange-listed + OTC), twice a month → panel short_interest.

Public API, no key: https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest
(history from 2020-04). Positions are as of the settlement date and published about a week
later, so the point-in-time date used by any signal is `public_date` = settlement date + 9
business days (FINRA's dissemination schedule is 7 business days; +2 of margin).

Separate DuckDB file (~/.hedge-fund/agent/short.db) so loads never lock panel.db.

Usage:
  python -m agent.sources.finra_short backfill [--start 2020-04-01]
  python -m agent.sources.finra_short update            # last 3 months
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request

import duckdb
import pandas as pd

from hedge_fund.paths import AGENT_DIR

SHORT_DB = AGENT_DIR / "short.db"
URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
PAGE = 5000
DDL = """CREATE TABLE IF NOT EXISTS short_interest (ticker VARCHAR, settlement DATE, public_date DATE, short_qty DOUBLE,
         prev_short_qty DOUBLE, adv DOUBLE, days_to_cover DOUBLE, market VARCHAR, PRIMARY KEY (ticker, settlement))"""


def _post(body: dict) -> list[dict]:
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt.strip() else []
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("FINRA API failed")


def fetch_range(start: dt.date, end: dt.date) -> pd.DataFrame:
    rows, offset = [], 0
    while True:
        batch = _post({"limit": PAGE, "offset": offset,
                       "fields": ["symbolCode", "settlementDate", "currentShortPositionQuantity", "previousShortPositionQuantity",
                                  "averageDailyVolumeQuantity", "daysToCoverQuantity", "marketClassCode"],
                       "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start.isoformat(), "endDate": end.isoformat()}]})
        rows += batch
        if len(batch) < PAGE:
            break
        offset += PAGE
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"symbolCode": "ticker", "settlementDate": "settlement", "currentShortPositionQuantity": "short_qty",
                            "previousShortPositionQuantity": "prev_short_qty", "averageDailyVolumeQuantity": "adv",
                            "daysToCoverQuantity": "days_to_cover", "marketClassCode": "market"})
    df["settlement"] = pd.to_datetime(df["settlement"]).dt.date
    df["public_date"] = [(pd.Timestamp(s) + pd.offsets.BDay(9)).date() for s in df["settlement"]]
    return df[["ticker", "settlement", "public_date", "short_qty", "prev_short_qty", "adv", "days_to_cover", "market"]].drop_duplicates(["ticker", "settlement"])


def load(start: dt.date, end: dt.date) -> int:
    con = duckdb.connect(str(SHORT_DB))
    con.execute(DDL)
    n, cur = 0, start
    while cur <= end:
        nxt = min((pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date(), end)
        df = fetch_range(cur, nxt)
        if not df.empty:
            con.register("_s", df)
            con.execute("INSERT OR REPLACE INTO short_interest SELECT * FROM _s")
            con.unregister("_s")
            n += len(df)
        print(f"  {cur} → {nxt}: {len(df)} rows", flush=True)
        cur = nxt + dt.timedelta(days=1)
    con.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update"])
    ap.add_argument("--start", default="2020-04-01")
    args = ap.parse_args()
    today = dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.cmd == "backfill" else today - dt.timedelta(days=90)
    print(load(start, today), "rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
