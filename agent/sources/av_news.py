"""Alpha Vantage NEWS_SENTIMENT → panel.news_sentiment, point-in-time from today on.

Two measured facts shape this collector:
- `tickers=A,B` returns only articles mentioning ALL of them (verified
  2026-09-19: AAPL alone 23 articles, RKLB alone 3, "RKLB,AAPL" zero), so
  a per-universe call is impossible. We pull untickered with limit=1000
  and filter each article's own ticker_sentiment locally.
- The historical feed is not point-in-time: summaries and scores are
  regenerated, so a backfill cannot be validated. Only what we crawl
  forward counts, which is why this starts now.

Free tier is 25 requests/day and bin/congress.py already takes 22 every
Saturday, so the quota ledger is shared by file and this refuses to run
the Saturday budget away.

Usage: python -m agent.sources.av_news [--hours 24] [--max-calls 2]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import ssl
import urllib.parse
import urllib.request

import pandas as pd

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:                                    # system CAs are fine on Homebrew python
    _CTX = None

from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

ENV_PATH = "/Users/louis/optradar/.env"
QUOTA_PATH = AGENT_DIR / "av_quota.json"
DAILY_LIMIT = 25
RESERVED_FOR_CONGRESS = 22       # bin/congress.py, Saturdays
URL = "https://www.alphavantage.co/query"


def api_key() -> str:
    for line in open(ENV_PATH):
        if line.startswith("ALPHAVANTAGE_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ALPHAVANTAGE_KEY not in optradar/.env")


def _quota() -> dict:
    today = dt.date.today().isoformat()
    if QUOTA_PATH.exists():
        q = json.loads(QUOTA_PATH.read_text())
        if q.get("date") == today:
            return q
    return {"date": today, "used": 0}


def _spend(n: int) -> None:
    q = _quota()
    q["used"] += n
    QUOTA_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_PATH.write_text(json.dumps(q))


def budget_left() -> int:
    q = _quota()
    reserve = RESERVED_FOR_CONGRESS if dt.date.today().weekday() == 5 else 0
    return max(DAILY_LIMIT - reserve - q["used"], 0)


def fetch(time_from: dt.datetime, time_to: dt.datetime, limit: int = 1000) -> dict:
    params = {"function": "NEWS_SENTIMENT", "apikey": api_key(), "limit": str(limit), "sort": "LATEST",
              "time_from": time_from.strftime("%Y%m%dT%H%M"), "time_to": time_to.strftime("%Y%m%dT%H%M")}
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "optradar-agent"})
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as resp:
        body = json.loads(resp.read().decode())
    _spend(1)
    if "feed" not in body:
        raise RuntimeError(str(body)[:300])
    return body


def rows_from(feed: list[dict], universe: set[str]) -> list[dict]:
    now = pd.Timestamp.now()
    out = []
    for art in feed:
        ts = pd.to_datetime(art["time_published"], format="%Y%m%dT%H%M%S", errors="coerce")
        aid = art.get("url", "")[:400]
        for ts_row in art.get("ticker_sentiment", []):
            t = ts_row.get("ticker", "")
            if universe and t not in universe:
                continue
            out.append({"article_id": aid, "time_published": ts, "ticker": t,
                        "relevance": float(ts_row.get("relevance_score", 0) or 0),
                        "sentiment": float(ts_row.get("ticker_sentiment_score", 0) or 0),
                        "overall_sentiment": float(art.get("overall_sentiment_score", 0) or 0),
                        "source_domain": art.get("source_domain"), "fetched_at": now})
    return out


def crawl(store: PanelStore, hours: int = 24, max_calls: int = 2) -> dict:
    left = min(budget_left(), max_calls)
    if left <= 0:
        return {"skipped": "no AV quota left today", "used_today": _quota()["used"]}
    members = {t for _, m in store.membership_changes() for t in m}
    to = dt.datetime.now()
    frm = to - dt.timedelta(hours=hours)
    total_rows, articles, truncated = 0, 0, False
    for _ in range(left):
        body = fetch(frm, to)
        feed = body["feed"]
        articles += len(feed)
        truncated = len(feed) >= 1000
        total_rows += store.insert("news_sentiment", pd.DataFrame(rows_from(feed, members)))
        store.insert("news_fetch_log", pd.DataFrame([{"window_from": frm, "window_to": to,
                                                      "n_articles": len(feed), "n_rows": total_rows,
                                                      "truncated": truncated, "fetched_at": pd.Timestamp.now()}]))
        if not truncated:
            break
        to = pd.to_datetime(feed[-1]["time_published"], format="%Y%m%dT%H%M%S").to_pydatetime()
    return {"articles": articles, "rows": total_rows, "truncated": truncated,
            "quota_used_today": _quota()["used"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--max-calls", type=int, default=2)
    args = ap.parse_args()
    with PanelStore() as store:
        print(crawl(store, args.hours, args.max_calls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
