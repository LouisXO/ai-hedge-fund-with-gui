"""Alpaca daily bars → panel.bars, including delisted names (free tier).

Measured 2026-09-20 on 150 non-S&P names listed in 2016q1: NYSE 98%,
NASDAQ 85%, AMEX 80% have that year's bars, versus 44% from yfinance,
which deletes a company's history when it delists. That gap is what makes
a small/mid-cap study possible at all (docs/AGENT_PLAN.md §9).

Free-plan notes that matter:
- historical SIP (full-market) bars ARE served; only recent timestamps
  (< 15 minutes) need a subscription. IEX has no useful pre-2018 history.
- `adjustment=all` gives split+dividend adjusted prices; we store raw
  close in `close` and adjusted in `adj_close` by asking twice, so the
  panel keeps its convention (RV from raw OHLC, returns from adj_close).
- Up to ~100 symbols per request, paginated with next_page_token.

Universe: exchange-listed names from panel.listing_status (NYSE, NASDAQ,
AMEX and variants), restricted to those seen in panel.issuer_seen, so OTC
shells stay out.

Usage:
  python -m agent.sources.alpaca_bars backfill [--start 2015-01-01] [--limit 500]
  python -m agent.sources.alpaca_bars update [--days 7] [--extra AAPL,MSFT]   # after the close, whole universe
  python -m agent.sources.alpaca_bars coverage
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.parse

import pandas as pd

from agent.sources.price_probe import _get, keys_from_env
from hedge_fund.features.panel import PanelStore

BASE = "https://data.alpaca.markets/v2/stocks/bars"
EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "NYSE MKT", "NYSE ARCA", "BATS")
BATCH = 100
SOURCE = "alpaca"


def universe(store: PanelStore, limit: int | None = None) -> list[str]:
    q = """
        SELECT DISTINCT l.symbol FROM listing_status l
        JOIN (SELECT DISTINCT ticker FROM issuer_seen) i ON i.ticker = l.symbol
        WHERE l.exchange IN ('NYSE','NASDAQ','AMEX','NYSE MKT','NYSE ARCA','BATS')
          AND l.asset_type = 'Stock'
        ORDER BY l.symbol"""
    if limit:
        q += f" LIMIT {limit}"
    return store.con.execute(q).df()["symbol"].tolist()


def fetch_batch(symbols: list[str], start: str, end: str, key: str, secret: str,
                adjustment: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    token = None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                  "limit": 10000, "feed": "sip", "adjustment": adjustment}
        if token:
            params["page_token"] = token
        code, body = _get(f"{BASE}?{urllib.parse.urlencode(params)}", headers)
        if code == 429:                      # free tier is 200 requests/min
            time.sleep(20)
            continue
        if code != 200:
            return out
        payload = json.loads(body)
        for sym, bars in (payload.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = payload.get("next_page_token")
        if not token:
            return out


def backfill(store: PanelStore, start: str, end: str, limit: int | None = None,
             pause: float = 0.35, symbols: list[str] | None = None, quiet: bool = False) -> dict:
    key, secret = keys_from_env("alpaca")
    syms = symbols if symbols is not None else universe(store, limit)
    stats = {"symbols": len(syms), "rows": 0, "with_data": 0}
    now = pd.Timestamp.now()
    for i in range(0, len(syms), BATCH):
        batch = syms[i:i + BATCH]
        raw = fetch_batch(batch, start, end, key, secret, "raw")
        adj = fetch_batch(batch, start, end, key, secret, "all")
        frames = []
        for sym, bars in raw.items():
            if not bars:
                continue
            df = pd.DataFrame(bars)
            df["trade_date"] = pd.to_datetime(df["t"]).dt.date
            a = pd.DataFrame(adj.get(sym) or [])
            if not a.empty:
                a["trade_date"] = pd.to_datetime(a["t"]).dt.date
                df = df.merge(a[["trade_date", "c"]].rename(columns={"c": "adj_close"}),
                              on="trade_date", how="left")
            else:
                df["adj_close"] = df["c"]
            df.loc[df["adj_close"] <= 0, "adj_close"] = None            # Alpaca sometimes returns 0 (audit 2026-09-22)
            frames.append(pd.DataFrame({"ticker": sym, "trade_date": df["trade_date"], "open": df["o"],
                                        "high": df["h"], "low": df["l"], "close": df["c"],
                                        "adj_close": df["adj_close"], "volume": df["v"],
                                        "source": SOURCE, "fetched_at": now}))
            stats["with_data"] += 1
        if frames:
            stats["rows"] += store.upsert_bars(pd.concat(frames, ignore_index=True))
        if not quiet:
            print(f"  [{min(i + BATCH, len(syms))}/{len(syms)}] rows {stats['rows']} "
                  f"symbols with data {stats['with_data']}", flush=True)
        time.sleep(pause)
    return stats


def update(store: PanelStore, days: int = 7, extra: list[str] | None = None) -> dict:
    """Incremental: the last `days` calendar days for the whole listed universe (+ `extra`).

    Today's daily bar is served once the session has closed (the free tier only
    withholds the last 15 minutes of intraday data), so an after-close run gets
    the completed bar the books need. ~80 requests, under a minute.
    """
    syms = sorted(set(universe(store)) | set(extra or []))
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    # the free tier 403s when the query's END bound is inside the last 15 minutes; a bound
    # 16 minutes back still returns today's bar in full once the session has closed
    end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = backfill(store, start, end, symbols=syms, pause=0.2, quiet=True)
    stats["last_bar"] = store.con.execute("SELECT max(trade_date) FROM bars WHERE source = ?", [SOURCE]).fetchone()[0]
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update", "coverage"])
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--extra", default="", help="comma-separated symbols to include (e.g. current positions)")
    args = ap.parse_args()
    end = args.end or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    with PanelStore() as store:
        if args.cmd == "backfill":
            print(backfill(store, args.start, end, args.limit))
        elif args.cmd == "update":
            print(update(store, args.days, [s for s in args.extra.split(",") if s]))
        else:
            print(store.con.execute("""SELECT source, count(*) AS n_rows, count(DISTINCT ticker) AS n_tickers,
                                              min(trade_date) AS first, max(trade_date) AS last
                                       FROM bars GROUP BY 1""").df().to_dict("records"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
