"""Daily point-in-time archive of data that has no free history of its own → ~/.hedge-fund/agent/archive.db.

Run after the close (agent/bin/postclose.sh). Everything here is kept as "what we could see that
day", so a test a year from now has no look-ahead.

  iv_hv        moomoo get_option_underlying_his_volatility: daily ATM IV and HV per underlying.
               The endpoint returns ~251 days, so each name is fetched once a week (a fifth of the
               universe per weekday, plus the focus list daily) and the history stays complete.
  (moomoo research endpoints allow 60 calls per 30 seconds; every call is paced.)
  consensus    moomoo get_research_analyst_consensus: target high / mean / low, rating mix, count —
               a daily snapshot for the 600 most-traded names plus the focus list.
  ratings      moomoo get_research_rating_summary: each broker's rating and target with its date
               (history only ~12 months deep, so archiving now is the only way to get more).
               Held names, watchlist, the long book's top 60 and the insider candidates only.
  borrow       Alpaca /v2/assets: shortable / easy_to_borrow / marginable for every active US equity.

moomoo is read-only here; no trading context is opened.

Usage: python -m agent.sources.daily_archive [--only iv_hv,consensus,ratings,borrow]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.request

import duckdb
import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

ARCHIVE_DB = AGENT_DIR / "archive.db"
OPTRADAR_DB = "/Users/louis/optradar/optradar.db"
DDL = [
    "CREATE TABLE IF NOT EXISTS iv_hv (ticker VARCHAR, day DATE, iv DOUBLE, hv DOUBLE, px DOUBLE, fetched DATE, PRIMARY KEY (ticker, day))",
    """CREATE TABLE IF NOT EXISTS consensus (day DATE, ticker VARCHAR, tp_high DOUBLE, tp_avg DOUBLE, tp_low DOUBLE, rating VARCHAR,
       n_analysts INT, buy_pct DOUBLE, hold_pct DOUBLE, sell_pct DOUBLE, updated VARCHAR, PRIMARY KEY (day, ticker))""",
    """CREATE TABLE IF NOT EXISTS ratings (ticker VARCHAR, institution VARCHAR, rec_date DATE, rating VARCHAR, target DOUBLE,
       first_seen DATE, PRIMARY KEY (ticker, institution, rec_date))""",
    """CREATE TABLE IF NOT EXISTS borrow (day DATE, ticker VARCHAR, shortable BOOLEAN, easy_to_borrow BOOLEAN, marginable BOOLEAN,
       PRIMARY KEY (day, ticker))""",
]


def universe() -> list[str]:
    """Tradable names on the last bar: listed, ADV >= $5M (the books' floor)."""
    with PanelStore(read_only=True) as store:
        rows = store.con.execute("""
            WITH last AS (SELECT max(trade_date) d FROM bars),
                 w AS (SELECT ticker, avg(close * volume) adv FROM bars, last WHERE trade_date > last.d - INTERVAL 30 DAY GROUP BY 1)
            SELECT ticker FROM w WHERE adv >= 5e6 ORDER BY adv DESC""").fetchall()
    return [r[0] for r in rows]


def focus() -> list[str]:
    """Held names, watchlist, long book top 60, recent insider candidates."""
    import yaml
    names = set()
    try:
        con = duckdb.connect(OPTRADAR_DB, read_only=True)
        names |= {r[0] for r in con.execute("SELECT ticker FROM agent_lots WHERE status = 'open'").fetchall()}
        names |= {r[0] for r in con.execute("""SELECT ticker FROM agent_picks WHERE as_of >= current_date - 7""").fetchall()}
        names |= {r[0].replace("US.", "") for r in con.execute("SELECT DISTINCT underlying FROM acct_positions WHERE date = (SELECT max(date) FROM acct_positions)").fetchall() if r[0]}
        con.close()
    except Exception as exc:
        print(f"focus from ledger failed: {exc}")
    try:
        names |= set(yaml.safe_load(open("/Users/louis/hedge-fund/agent/watchlist.yaml"))["watch"])
    except Exception:
        pass
    return sorted(n for n in names if n and "." not in n)


PACE = 0.55          # moomoo research endpoints: at most 60 calls per 30 seconds


def _paced(fn, *a, **k):
    for attempt in range(3):
        r = fn(*a, **k)
        time.sleep(PACE)
        if r[0] == 0 or "频率太高" not in str(r[1]):
            return r
        time.sleep(31)
    return r


def moomoo_ctx():
    import moomoo as mm
    return mm.OpenQuoteContext(host="127.0.0.1", port=11111)


def archive_iv(con, q, names: list[str]) -> int:
    n = 0
    today = dt.date.today()
    for t in names:
        r = _paced(q.get_option_underlying_his_volatility, f"US.{t}")
        ret, d = r[0], r[1]                                        # returns (ret, data, page_key)
        if ret != 0 or d is None or len(d) == 0:
            continue
        df = pd.DataFrame({"ticker": t, "day": pd.to_datetime(d["time"]).dt.date, "iv": d["iv"].astype(float), "hv": d["hv"].astype(float),
                           "px": d["underlying_price"].astype(float), "fetched": today})
        con.register("_v", df)
        con.execute("INSERT OR REPLACE INTO iv_hv SELECT * FROM _v")
        con.unregister("_v")
        n += 1
    return n


def archive_consensus(con, q, names: list[str]) -> int:
    rows, today = [], dt.date.today()
    for t in names:
        r = _paced(q.get_research_analyst_consensus, f"US.{t}")
        ret, d = r[0], r[1]
        if ret != 0 or not isinstance(d, dict) or not d.get("total"):
            continue
        rows.append([today, t, d.get("highest"), d.get("average"), d.get("lowest"), d.get("rating"), d.get("total"),
                     d.get("buy"), d.get("hold"), d.get("sell"), d.get("update_time_str")])
    if rows:
        con.executemany("INSERT OR REPLACE INTO consensus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def archive_ratings(con, q, names: list[str]) -> int:
    today, n = dt.date.today(), 0
    for t in names:
        key = None
        for _ in range(10):
            r = _paced(q.get_research_rating_summary, f"US.{t}", num=10, next_key=key)
            ret, d = r[0], r[1]
            if ret != 0 or not isinstance(d, dict):
                break
            for inst in d.get("inst_rating_summary_list", []):
                name = (inst.get("institution_info") or {}).get("institution_en_name") or (inst.get("institution_info") or {}).get("institution_name")
                for it in inst.get("rating_item_list", []):
                    con.execute("INSERT OR IGNORE INTO ratings VALUES (?, ?, ?, ?, ?, ?)",
                                [t, name, it.get("recommendation_date_str"), it.get("rating"), it.get("target_price"), today])
                    n += 1
            key = d.get("next_key")
            if not key:
                break
    return n


def archive_borrow(con) -> int:
    env = dict(l.strip().split("=", 1) for l in open(AGENT_DIR.parent / ".env") if "=" in l and not l.startswith("#"))
    h = {"APCA-API-KEY-ID": env["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET"]}
    req = urllib.request.Request("https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity", headers=h)
    with urllib.request.urlopen(req, timeout=120) as r:
        assets = json.load(r)
    today = dt.date.today()
    df = pd.DataFrame([{"day": today, "ticker": a["symbol"], "shortable": bool(a.get("shortable")), "easy_to_borrow": bool(a.get("easy_to_borrow")),
                        "marginable": bool(a.get("marginable"))} for a in assets if a.get("tradable")])
    con.register("_b", df)
    con.execute("INSERT OR REPLACE INTO borrow SELECT * FROM _b")
    con.unregister("_b")
    return len(df)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="iv_hv,consensus,ratings,borrow")
    args = ap.parse_args()
    parts = set(args.only.split(","))
    con = duckdb.connect(str(ARCHIVE_DB))
    for s in DDL:
        con.execute(s)
    stats, t0 = {}, time.time()
    try:
        if parts & {"iv_hv", "consensus", "ratings"}:
            q = moomoo_ctx()
            try:
                uni = universe()
                fx = focus()
                if "iv_hv" in parts:
                    # each call returns ~251 days, so a fifth of the universe per weekday keeps every history complete;
                    # focus names every day. ~600 calls ≈ 6 minutes at the rate limit.
                    k = dt.date.today().weekday() % 5
                    todays = sorted(set(uni[k::5]) | set(fx))
                    have = {r[0] for r in con.execute("SELECT DISTINCT ticker FROM iv_hv").fetchall()}
                    todays += [t for t in uni if t not in have][:200]            # first weeks: also backfill 200 missing names a day
                    stats["iv_hv_names"] = archive_iv(con, q, sorted(set(todays)))
                if "consensus" in parts:
                    stats["consensus_names"] = archive_consensus(con, q, sorted(set(uni[:600]) | set(fx)))
                if "ratings" in parts:
                    stats["rating_rows_seen"] = archive_ratings(con, q, focus())
            finally:
                q.close()
        if "borrow" in parts:
            stats["borrow_names"] = archive_borrow(con)
    finally:
        con.close()
    stats["seconds"] = round(time.time() - t0)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
