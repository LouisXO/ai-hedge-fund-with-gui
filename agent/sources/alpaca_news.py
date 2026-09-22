"""Alpaca News (Benzinga wire, free tier) → panel.news_items / news_symbols.

Why a second news source next to Alpha Vantage: AV is 25 requests/day and
its history for this account starts 2026-03; Alpaca serves symbol-tagged
headlines back to at least 2017 with no meaningful quota (200 req/min),
which is what an event line like "large move with / without news" needs to
be backtested rather than merely collected.

Stored in ~/.hedge-fund/agent/news.db (separate file, so the long backfill
never blocks panel.db readers): id, created_at (UTC), updated_at, source,
headline, url, symbols.
No article bodies (include_content=false) — the line only needs "was there
news about this name in this window".

Usage:
  python -m agent.sources.alpaca_news backfill [--start 2017-01-01] [--end 2026-09-22]
  python -m agent.sources.alpaca_news update [--days 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from agent.sources.price_probe import keys_from_env
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

URL = "https://data.alpaca.markets/v1beta1/news"
NEWS_DB = AGENT_DIR / "news.db"      # its own file: a 6-hour backfill must not hold panel.db's write lock
LIMIT = 50
DDL = [
    """CREATE TABLE IF NOT EXISTS news_items (
        id BIGINT PRIMARY KEY, created_at TIMESTAMP, updated_at TIMESTAMP, source VARCHAR,
        headline VARCHAR, url VARCHAR, n_symbols INT, fetched_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS news_symbols (id BIGINT, symbol VARCHAR, created_at TIMESTAMP,
        PRIMARY KEY (id, symbol))""",
    """CREATE TABLE IF NOT EXISTS news_fetch_days (day DATE PRIMARY KEY, n_items INT, fetched_at TIMESTAMP)""",
]


def _get(params: dict, key: str, secret: str) -> dict:
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15)
                continue
            if attempt == 5:
                raise
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return {}


def fetch_day(day: dt.date, key: str, secret: str) -> list[dict]:
    """Every item created on `day` (UTC), all symbols, paginated."""
    out, token = [], None
    start = f"{day.isoformat()}T00:00:00Z"
    end = f"{(day + dt.timedelta(days=1)).isoformat()}T00:00:00Z"
    while True:
        p = {"start": start, "end": end, "limit": LIMIT, "sort": "asc", "include_content": "false",
             "exclude_contentless": "false"}
        if token:
            p["page_token"] = token
        d = _get(p, key, secret)
        out.extend(d.get("news") or [])
        token = d.get("next_page_token")
        if not token:
            return out


def upsert(store: PanelStore, items: list[dict]) -> int:
    if not items:
        return 0
    now = pd.Timestamp.now()
    rows = [{"id": int(x["id"]), "created_at": pd.Timestamp(x["created_at"]).tz_convert(None),
             "updated_at": pd.Timestamp(x["updated_at"]).tz_convert(None) if x.get("updated_at") else None,
             "source": x.get("source"), "headline": (x.get("headline") or "")[:500], "url": (x.get("url") or "")[:500],
             "n_symbols": len(x.get("symbols") or []), "fetched_at": now} for x in items]
    syms = [{"id": int(x["id"]), "symbol": s, "created_at": pd.Timestamp(x["created_at"]).tz_convert(None)}
            for x in items for s in (x.get("symbols") or [])]
    df = pd.DataFrame(rows).drop_duplicates("id")
    store.con.register("_n", df)
    store.con.execute("INSERT OR REPLACE INTO news_items SELECT id, created_at, updated_at, source, headline, url, n_symbols, fetched_at FROM _n")
    store.con.unregister("_n")
    if syms:
        ds = pd.DataFrame(syms).drop_duplicates(["id", "symbol"])
        store.con.register("_s", ds)
        store.con.execute("INSERT OR REPLACE INTO news_symbols SELECT id, symbol, created_at FROM _s")
        store.con.unregister("_s")
    return len(df)


def backfill(store: PanelStore, start: dt.date, end: dt.date, redo: bool = False) -> dict:
    key, secret = keys_from_env("alpaca")
    for stmt in DDL:
        store.con.execute(stmt)
    done = {r[0] for r in store.con.execute("SELECT day FROM news_fetch_days").fetchall()} if not redo else set()
    stats = {"days": 0, "items": 0}
    day = start
    while day <= end:
        if day not in done:
            items = fetch_day(day, key, secret)
            n = upsert(store, items)
            store.con.execute("INSERT OR REPLACE INTO news_fetch_days VALUES (?, ?, ?)", [day, n, pd.Timestamp.now()])
            stats["days"] += 1
            stats["items"] += n
            if stats["days"] % 20 == 0:
                print(f"  {day}: {stats['items']} items over {stats['days']} days", flush=True)
        day += dt.timedelta(days=1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update"])
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    today = dt.date.today()
    if args.cmd == "backfill":
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end) if args.end else today
        redo = False
    else:
        start, end, redo = today - dt.timedelta(days=args.days), today, True
    with PanelStore(NEWS_DB) as store:
        print(backfill(store, start, end, redo=redo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
