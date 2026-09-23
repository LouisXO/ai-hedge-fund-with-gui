"""Retail attention without Reddit's API: ApeWisdom (aggregated subreddit mentions) and Stocktwits.

Reddit closed self-service Data API access in Nov 2025 (approval only, personal
scripts rejected), so mention counts come from ApeWisdom's free aggregate feed
(24h mentions and rank across the trading subreddits) and Stocktwits' public
symbol streams (watchers, and the bullish/bearish tags on the last messages).
Both are read without credentials and stored daily in ~/.hedge-fund/agent/
retail.db so the series exists if it is ever worth testing. Until then it is
a risk flag on the watchlist, not a signal: the literature has retail
attention predicting a day or two of continuation and then reversal.

Usage: python -m agent.sources.retail_heat [--symbols RKLB,NEOV]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.request

import duckdb
import pandas as pd

from hedge_fund.paths import AGENT_DIR

RETAIL_DB = AGENT_DIR / "retail.db"
UA = "optradar-research/0.1 (contact: louis.leng@outlook.com)"
DDL = [
    """CREATE TABLE IF NOT EXISTS apewisdom_daily (day DATE, ticker VARCHAR, rank INT, mentions INT, mentions_24h_ago INT,
        upvotes INT, PRIMARY KEY (day, ticker))""",
    """CREATE TABLE IF NOT EXISTS stocktwits_daily (day DATE, ticker VARCHAR, watchers INT, n_msgs INT, bullish INT, bearish INT,
        PRIMARY KEY (day, ticker))""",
]


def _get(url: str, ua: str = UA) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=30) as r:
        return json.load(r)


def apewisdom(pages: int = 3) -> pd.DataFrame:
    rows = []
    for p in range(1, pages + 1):
        try:
            d = _get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{p}")
        except Exception:
            break
        for r in d.get("results", []):
            rows.append({"ticker": r["ticker"], "rank": int(r.get("rank") or 0), "mentions": int(r.get("mentions") or 0),
                         "mentions_24h_ago": int(r.get("mentions_24h_ago") or 0), "upvotes": int(r.get("upvotes") or 0)})
    return pd.DataFrame(rows)


def stocktwits(symbol: str) -> dict | None:
    try:
        d = _get(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json?limit=30", ua="Mozilla/5.0")
    except Exception:
        return None
    msgs = d.get("messages", [])
    tags = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in msgs]
    return {"watchers": int((d.get("symbol") or {}).get("watchlist_count") or 0), "n_msgs": len(msgs),
            "bullish": sum(1 for t in tags if t == "Bullish"), "bearish": sum(1 for t in tags if t == "Bearish")}


def snapshot(symbols: list[str]) -> dict:
    """Store today's ApeWisdom top pages and Stocktwits stats for `symbols`; return the watchlist view."""
    today = dt.date.today()
    con = duckdb.connect(str(RETAIL_DB))
    for s in DDL:
        con.execute(s)
    ape = apewisdom()
    if not ape.empty:
        con.register("_a", ape.assign(day=today))
        con.execute("INSERT OR REPLACE INTO apewisdom_daily SELECT day, ticker, rank, mentions, mentions_24h_ago, upvotes FROM _a")
        con.unregister("_a")
    out = {}
    for sym in symbols:
        st = stocktwits(sym)
        if st:
            con.execute("INSERT OR REPLACE INTO stocktwits_daily VALUES (?, ?, ?, ?, ?, ?)", [today, sym, st["watchers"], st["n_msgs"], st["bullish"], st["bearish"]])
        a = ape[ape["ticker"] == sym].iloc[0].to_dict() if not ape.empty and (ape["ticker"] == sym).any() else None
        out[sym] = {"apewisdom": a, "stocktwits": st}
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="RKLB,NEOV")
    args = ap.parse_args()
    for sym, v in snapshot(args.symbols.split(",")).items():
        a, s = v["apewisdom"], v["stocktwits"]
        print(f"{sym:6s} reddit: {'rank %s, %s mentions/24h (prev %s)' % (a['rank'], a['mentions'], a['mentions_24h_ago']) if a else 'not in top pages'}"
              f" | stocktwits: {'%s watchers, last %s msgs %s bull / %s bear' % (s['watchers'], s['n_msgs'], s['bullish'], s['bearish']) if s else 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
