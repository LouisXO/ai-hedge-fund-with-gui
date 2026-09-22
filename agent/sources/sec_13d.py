"""Initial Schedule 13D filings (activist / >5% stakes with intent) → panel.sch13d.

Source: EDGAR full-text search (efts.sec.gov), which is the only free index
that (a) separates the initial filing from amendments after the Dec-2024
switch to XML "SCHEDULE 13D" (the full-index truncates form types to 12
chars, so 13D and 13D/A collapse), and (b) names the subject company with
its CIK and ticker and the filers separately.

Only the INITIAL filing is an event (Brav, Jiang, Partnoy & Thomas 2008:
the announcement effect and the drift are on the first 13D). Amendments
are recorded with is_amendment=TRUE for later use and never traded on.

The ticker EFTS shows is the CURRENT one; the point-in-time ticker comes
from panel.issuer_seen (Form 4 issuer ↔ CIK by quarter) when available.

Usage:
  python -m agent.sources.sec_13d backfill [--start 2015-01-01]
  python -m agent.sources.sec_13d update [--days 5]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

EFTS = "https://efts.sec.gov/LATEST/search-index"
PAGE = 100
PAUSE = 0.15
FORMS = ("SC 13D", "SCHEDULE 13D")

DDL = """CREATE TABLE IF NOT EXISTS sch13d (
    accession VARCHAR PRIMARY KEY, filed DATE, form VARCHAR, is_amendment BOOLEAN,
    subject_cik VARCHAR, subject_name VARCHAR, ticker_display VARCHAR, ticker VARCHAR,
    filers VARCHAR, fetched_at TIMESTAMP)"""

_NAME = re.compile(r"^(?P<name>.*?)\s+(?:\((?P<ticker>[A-Z0-9.\-]{1,10})\)\s+)?\(CIK (?P<cik>\d{10})\)\s*$")


def _get_json(params: dict, ua: str) -> dict | None:
    url = EFTS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def parse_hit(hit: dict) -> dict | None:
    src = hit["_source"]
    names = src.get("display_names") or []
    if not names:
        return None
    parsed = [(_NAME.match(n), n) for n in names]
    subj = parsed[0][0]
    if not subj:
        return None
    filers = [m.group("name") if m else raw for m, raw in parsed[1:]]
    form = src.get("form", "")
    return {"accession": hit["_id"].split(":")[0], "filed": src.get("file_date"), "form": form,
            "is_amendment": form.endswith("/A"), "subject_cik": subj.group("cik"),
            "subject_name": subj.group("name").strip(), "ticker_display": subj.group("ticker"),
            "filers": "; ".join(filers)[:500]}


def search(form: str, start: dt.date, end: dt.date, ua: str) -> list[dict]:
    out, frm = [], 0
    while True:
        d = _get_json({"q": '"13D"', "forms": form, "dateRange": "custom", "startdt": start.isoformat(),
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


def pit_ticker(store: PanelStore, df: pd.DataFrame) -> pd.Series:
    """issuer_seen ticker for (cik, quarter of filing); fall back to EFTS's current ticker."""
    seen = store.con.execute("SELECT cik, quarter, ticker FROM issuer_seen").df()
    seen["cik"] = seen["cik"].str.zfill(10)
    key = seen.set_index(["cik", "quarter"])["ticker"].to_dict()
    latest = seen.sort_values("quarter").drop_duplicates("cik", keep="last").set_index("cik")["ticker"].to_dict()
    q = pd.to_datetime(df["filed"]).dt.year.astype(str) + "q" + pd.to_datetime(df["filed"]).dt.quarter.astype(str)
    out = []
    for cik, qq, disp in zip(df["subject_cik"], q, df["ticker_display"]):
        out.append(key.get((cik, qq)) or latest.get(cik) or disp)
    return pd.Series(out, index=df.index)


def _upsert(store: PanelStore, rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows).drop_duplicates("accession")
    df["ticker"] = pit_ticker(store, df)
    df["fetched_at"] = pd.Timestamp.now()
    store.con.register("_in", df)
    store.con.execute("INSERT OR REPLACE INTO sch13d SELECT accession, filed, form, is_amendment, subject_cik, "
                      "subject_name, ticker_display, ticker, filers, fetched_at FROM _in")
    store.con.unregister("_in")
    return len(df)


def load(store: PanelStore, start: dt.date, end: dt.date, quiet: bool = False, workers: int = 4) -> dict:
    """Quarter windows (under EFTS's 10,000-hit cap), fetched in parallel, inserted per quarter."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ua = user_agent()
    store.con.execute(DDL)
    windows = []
    cur = start
    while cur <= end:
        nxt = min((pd.Timestamp(cur) + pd.offsets.QuarterEnd(0)).date(), end)
        windows.append((cur, nxt))
        cur = nxt + dt.timedelta(days=1)

    def one(w):
        rows = []
        for form in FORMS:
            rows += search(form, w[0], w[1], ua)
        return w, rows

    stats = {"rows": 0, "initial": 0, "with_ticker": 0}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(one, w) for w in windows]):
            w, rows = fut.result()
            n = _upsert(store, rows)
            stats["rows"] += n
            stats["initial"] += sum(1 for r in rows if not r["is_amendment"])
            if not quiet:
                print(f"  {w[0]} → {w[1]}: {n} rows", flush=True)
    stats["with_ticker"] = store.con.execute("SELECT count(*) FROM sch13d WHERE ticker IS NOT NULL AND NOT is_amendment").fetchone()[0]
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update"])
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    today = dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.cmd == "backfill" else today - dt.timedelta(days=args.days)
    with PanelStore() as store:
        print(load(store, start, today, quiet=args.cmd == "update"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
