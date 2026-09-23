"""8-K filings that announce a share repurchase program → panel.buyback_8k.

Source: EDGAR full-text search (efts.sec.gov), form 8-K, phrase "repurchase
program" (covers "share repurchase program" and "stock repurchase program").
Every hit is a document (the 8-K body or an EX-99.1 press release); rows are
one per accession with the filing's Item list, which is what separates a
standalone authorization (Item 8.01 / 7.01) from a buyback line buried in an
earnings release (Item 2.02). The event line decides which to trade on.

The ticker EFTS shows is the CURRENT one; the point-in-time ticker comes from
panel.issuer_seen via sec_13d.pit_ticker when available.

Usage:
  python -m agent.sources.sec_8k_buyback backfill [--start 2017-01-01]
  python -m agent.sources.sec_8k_buyback update [--days 5]
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import pandas as pd

from agent.sources.sec_13d import _NAME, _get_json, pit_ticker
from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

PAGE = 100
PAUSE = 0.15
QUERY = '"repurchase program"'

DDL = """CREATE TABLE IF NOT EXISTS buyback_8k (
    accession VARCHAR PRIMARY KEY, filed DATE, items VARCHAR, has_earnings BOOLEAN, file_types VARCHAR,
    cik VARCHAR, name VARCHAR, ticker_display VARCHAR, ticker VARCHAR, fetched_at TIMESTAMP)"""


def parse_hit(hit: dict) -> dict | None:
    src = hit["_source"]
    names = src.get("display_names") or []
    m = _NAME.match(names[0]) if names else None
    if not m:
        return None
    items = [str(i) for i in (src.get("items") or [])]
    return {"accession": hit["_id"].split(":")[0], "filed": src.get("file_date"), "items": ",".join(items),
            "has_earnings": "2.02" in items, "file_types": src.get("file_type") or "",
            "cik": m.group("cik"), "name": m.group("name").strip(), "ticker_display": m.group("ticker")}


def search(start: dt.date, end: dt.date, ua: str) -> list[dict]:
    out, frm = [], 0
    while True:
        d = _get_json({"q": QUERY, "forms": "8-K", "dateRange": "custom", "startdt": start.isoformat(),
                       "enddt": end.isoformat(), "from": frm}, ua)
        if not d:
            break
        hits = d.get("hits", {}).get("hits", [])
        for h in hits:
            row = parse_hit(h)
            if row:
                out.append(row)
        total = d["hits"]["total"]["value"]
        frm += len(hits)
        if not hits or frm >= total or frm >= 10_000:
            break
        time.sleep(PAUSE)
    return out


def _upsert(store: PanelStore, rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    # several documents per accession (8-K body + exhibits): one row, union of file types
    df = (df.groupby("accession", as_index=False)
            .agg(filed=("filed", "first"), items=("items", "first"), has_earnings=("has_earnings", "max"),
                 file_types=("file_types", lambda s: ",".join(sorted(set(s)))), cik=("cik", "first"),
                 name=("name", "first"), ticker_display=("ticker_display", "first")))
    df = df.rename(columns={"cik": "subject_cik"})
    df["ticker"] = pit_ticker(store, df)
    df = df.rename(columns={"subject_cik": "cik"})
    df["fetched_at"] = pd.Timestamp.now()
    store.con.register("_in", df)
    store.con.execute("INSERT OR REPLACE INTO buyback_8k SELECT accession, filed, items, has_earnings, file_types, "
                      "cik, name, ticker_display, ticker, fetched_at FROM _in")
    store.con.unregister("_in")
    return len(df)


def load(store: PanelStore, start: dt.date, end: dt.date, quiet: bool = False, workers: int = 4) -> dict:
    """Monthly windows (well under EFTS's 10,000-hit cap), fetched in parallel, inserted per window."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ua = user_agent()
    store.con.execute(DDL)
    windows = []
    cur = start
    while cur <= end:
        nxt = min((pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date(), end)
        windows.append((cur, nxt))
        cur = nxt + dt.timedelta(days=1)
    stats = {"rows": 0, "windows": len(windows)}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(lambda w: (w, search(w[0], w[1], ua)), w) for w in windows]):
            w, rows = fut.result()
            n = _upsert(store, rows)
            stats["rows"] += n
            if not quiet:
                print(f"  {w[0]} → {w[1]}: {n} filings", flush=True)
    stats["with_ticker"] = store.con.execute("SELECT count(*) FROM buyback_8k WHERE ticker IS NOT NULL").fetchone()[0]
    stats["standalone"] = store.con.execute("SELECT count(*) FROM buyback_8k WHERE ticker IS NOT NULL AND NOT has_earnings").fetchone()[0]
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update"])
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    today = dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.cmd == "backfill" else today - dt.timedelta(days=args.days)
    with PanelStore() as store:
        print(load(store, start, today, quiet=args.cmd == "update"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
