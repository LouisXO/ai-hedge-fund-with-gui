"""Today's Form 4 filings from EDGAR's daily index → panel.insider_tx.

The quarterly bulk data sets (agent/sources/sec_form4.py) lag by months,
which is fine for research and useless for trading. EDGAR publishes a
daily index of every filing; this pulls the Form 4s from it and parses the
XML, so the live signal sees a filing the morning after it lands.

Only open-market purchases are kept (transactionCode P with a positive
price), matching the research definition in S11/S13.

SEC asks for 10 requests/second at most and a contact in the User-Agent
(SEC_USER_AGENT in ~/.hedge-fund/.env).

Two indexes:
  daily index  form.YYYYMMDD.idx — complete, but published after our
               after-close run, so a filing on D reached the panel on D+1
               and was traded at D+2's open (one day later than the backtest);
  --realtime   EDGAR full-text search (efts.sec.gov) lists a Form 4 within
               minutes of acceptance, so the 16:10 PT run sees D's filings
               and trades them at D+1's open, as the backtest assumed.
               S23 (2026-09-22): most of the insider edge is in the first
               session after the filing, so this lag is worth more than any
               refinement of the rule.

Locking (S48): a run can take hours when the Mac sleeps between requests, and DuckDB lets no other
process open panel.db while one holds the write lock. So a run reads the known accession numbers
with a short read-only connection, then downloads and parses with no connection open, and opens
the write connection only to write the rows.

Run log (S48): every run writes one row to panel.fetch_runs — ok / partial / failed with the counts
and the first errors — so "nothing was filed" (ok, 0 filings) and "the index could not be fetched"
(failed) are told apart. The table is shared with the other fetch jobs (sec_13d, av_news); it lives
here because the insider book trades on this one. A failed run exits 1.

Usage: python -m agent.sources.sec_daily_form4 [--days 3] [--realtime]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import PANEL_DB

IDX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
PAUSE = 0.12                      # ~8 requests/second, inside SEC's limit
FAIL_SHARE = 0.5                  # more than this share of the filings unfetchable = the run failed, not partial

RUNS_DDL = """CREATE TABLE IF NOT EXISTS fetch_runs (
    source VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP, status VARCHAR,
    n_requests INT, n_failed INT, n_items INT, n_rows INT, reason VARCHAR,
    PRIMARY KEY (source, started_at))"""
RUN_COLS = ["source", "started_at", "finished_at", "status", "n_requests", "n_failed", "n_items", "n_rows", "reason"]


def record_run(con, run: dict) -> None:
    """One row in panel.fetch_runs, written on the same connection as the data it describes.

    status  ok       every request answered (n_items = 0 then means nothing was filed)
            partial  the list was fetched, some items were not (they are retried next run)
            failed   the list itself could not be fetched, or most items could not
            skipped  the job chose not to ask (e.g. no API quota left)
    n_failed counts failed requests; reason keeps the first errors.
    """
    con.execute(RUNS_DDL)
    con.execute(f"INSERT OR REPLACE INTO fetch_runs ({', '.join(RUN_COLS)}) VALUES ({', '.join('?' * len(RUN_COLS))})",
                [run.get(c) for c in RUN_COLS])


class Http:
    """GET with the SEC User-Agent that counts what it could not fetch.

    A 404 is an answer when the caller says so (a daily index that is not published yet, a holiday);
    anything else that is not a 200 — a timeout, a refused connection, SEC's 403 throttle — is a failure.
    """

    def __init__(self, ua: str, opener=None, pause: float = PAUSE):
        self.ua, self.opener, self.pause = ua, opener or urllib.request.urlopen, pause
        self.n_requests = self.n_failed = 0
        self.errors: list[str] = []

    def get(self, url: str, missing_ok: bool = False) -> str | None:
        self.n_requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        try:
            with self.opener(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            err = f"HTTP {exc.code}"
        except Exception as exc:
            err = f"{type(exc).__name__} {str(exc)[:80]}"
        self.n_failed += 1
        if len(self.errors) < 3:
            self.errors.append(f"{err} ({url.split('?')[0][-80:]})")
        return None

    def sleep(self) -> None:
        if self.pause:
            time.sleep(self.pause)


def index_form4(day: dt.date, http: Http) -> list[str] | None:
    """Paths of the Form 4 filings indexed for that day; [] when no index exists (not yet published,
    holiday); None when the index could not be fetched."""
    before = http.n_failed
    text = http.get(IDX.format(y=day.year, q=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d")), missing_ok=True)
    if text is None:
        return None if http.n_failed > before else []
    out = []
    for line in text.splitlines():
        if not line.startswith("4 ") and not line.startswith("4/A "):
            continue
        parts = line.split()
        if parts and parts[-1].endswith(".txt"):
            out.append(parts[-1])
    return out


def efts_form4_paths(start: dt.date, end: dt.date, http: Http) -> tuple[list[tuple[str, dt.date]], bool]:
    """((filing path, file_date) for every Form 4 accepted in [start, end], complete?) from full-text search.

    complete is False when a page could not be fetched or read: the list may then be short, which
    must not be mistaken for a quiet day.
    """
    out, frm, complete = [], 0, True
    while True:
        q = urllib.parse.urlencode({"q": '"4"', "forms": "4", "dateRange": "custom", "startdt": start.isoformat(),
                                    "enddt": end.isoformat(), "from": frm})
        txt = http.get(f"https://efts.sec.gov/LATEST/search-index?{q}")
        if txt is None:
            complete = False
            break
        try:
            d = json.loads(txt)
            hits = d["hits"]["hits"]
            total = d["hits"]["total"]["value"]
        except (ValueError, KeyError, TypeError) as exc:
            http.n_failed += 1
            http.errors.append(f"efts answer unreadable: {str(exc)[:60]} {txt[:80]!r}")
            complete = False
            break
        for h in hits:
            src = h["_source"]
            if src.get("form") != "4" or not src.get("ciks"):
                continue
            acc = h["_id"].split(":")[0]
            out.append((f"edgar/data/{int(src['ciks'][0])}/{acc}.txt", dt.date.fromisoformat(src["file_date"])))
        frm += len(hits)
        if not hits or frm >= total or frm >= 10_000:
            break
        http.sleep()
    return list(dict.fromkeys(out)), complete


def _xml_url(path: str) -> str:
    """edgar/data/CIK/0001234567-26-000123.txt -> the filing's index directory."""
    acc = path.rsplit("/", 1)[-1].replace(".txt", "")
    return f"https://www.sec.gov/Archives/{path.rsplit('/', 1)[0]}/{acc.replace('-', '')}/"


def parse_filing(path: str, http: Http) -> list[dict]:
    """Fetch a filing's XML and return its open-market purchase rows (a failed request is counted by http)."""
    listing = http.get(_xml_url(path))
    if not listing:
        return []
    m = re.findall(r'href="([^"]+\.xml)"', listing)
    doc = next((u for u in m if "xslF345" not in u), None)
    if not doc:
        return []
    raw = http.get(f"https://www.sec.gov{doc}" if doc.startswith("/") else doc)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    def text(node, *path_parts):
        cur = node
        for p in path_parts:
            cur = cur.find(p) if cur is not None else None
        return (cur.text or "").strip() if cur is not None and cur.text else ""

    ticker = text(root, "issuer", "issuerTradingSymbol").upper()
    cik = text(root, "issuer", "issuerCik")
    owner = text(root, "reportingOwner", "reportingOwnerId", "rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    roles = ",".join(t.tag for t in rel if (t.text or "").strip() in ("1", "true")) if rel is not None else ""
    title = text(root, "reportingOwner", "reportingOwnerRelationship", "officerTitle")
    filed = text(root, "periodOfReport")
    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if text(tx, "transactionCoding", "transactionCode") != "P":
            continue
        amounts = tx.find("transactionAmounts")
        if amounts is None:
            continue
        shares = text(amounts, "transactionShares", "value")
        price = text(amounts, "transactionPricePerShare", "value")
        disp = text(amounts, "transactionAcquiredDisposedCode", "value")
        if disp != "A" or not shares or not price:
            continue
        try:
            sh, pr = float(shares), float(price)
        except ValueError:
            continue
        if pr <= 0 or sh <= 0 or not ticker:
            continue
        rows.append({"accession": path.rsplit("/", 1)[-1].replace(".txt", ""), "ticker": ticker,
                     "issuer_cik": cik, "trans_date": pd.to_datetime(text(tx, "transactionDate", "value"),
                                                                     errors="coerce").date(),
                     "owner_name": owner, "relationship": roles, "officer_title": title,
                     "trans_code": "P", "acq_disp": "A", "shares": sh, "price": pr,
                     "value_usd": sh * pr, "shares_after": None, "period_of_report": filed})
    return rows


def known_accessions(path: Path | str, since: dt.date) -> set[str]:
    """Accessions already in insider_tx, over a short read-only connection (released before any download)."""
    if not Path(path).exists():
        return set()
    with PanelStore(path, read_only=True) as store:
        return {r[0] for r in store.con.execute("SELECT DISTINCT accession FROM insider_tx WHERE filing_date >= ?",
                                                [since]).fetchall()}


def fetch(days: int, limit_filings: int | None, realtime: bool, known: set[str], http: Http,
          today: dt.date) -> tuple[pd.DataFrame, dict]:
    """Download and parse; no database connection is open here. Returns (rows, stats with the run status)."""
    stats = {"days": 0, "filings": 0, "rows": 0, "skipped_known": 0}
    frames, list_failed, filings_failed = [], [], 0
    if realtime:
        pairs, complete = efts_form4_paths(today - dt.timedelta(days=days - 1), today, http)
        if not complete:
            list_failed.append("efts search")
        by_day: dict[dt.date, list[str]] = {}
        for p, d in pairs:
            by_day.setdefault(d, []).append(p)
    for back in range(days):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        if realtime:
            paths = by_day.get(day, [])
        else:
            paths = index_form4(day, http)
            http.sleep()
            if paths is None:
                list_failed.append(f"daily index {day}")
                continue
        if not paths:
            continue
        stats["days"] += 1
        for p in paths[:limit_filings]:
            if p.rsplit("/", 1)[-1].replace(".txt", "") in known:
                stats["skipped_known"] += 1
                continue
            before = http.n_failed
            rows = parse_filing(p, http)
            http.sleep()
            stats["filings"] += 1
            filings_failed += http.n_failed > before
            if rows:
                df = pd.DataFrame(rows)
                df["filing_date"] = day
                df["source"] = "edgar_realtime" if realtime else "edgar_daily"
                df["fetched_at"] = pd.Timestamp.now()
                frames.append(df.drop(columns=["period_of_report"]))
    if list_failed:
        status = "failed"
    elif stats["filings"] and filings_failed > FAIL_SHARE * stats["filings"]:
        status = "failed"
    else:
        status = "partial" if filings_failed else "ok"
    reasons = ([f"could not list: {', '.join(list_failed)}"] if list_failed else []) + \
              ([f"{filings_failed} of {stats['filings']} filings not fetched"] if filings_failed else []) + http.errors
    stats.update({"status": status, "failed_requests": http.n_failed, "filings_not_fetched": filings_failed,
                  "reason": "; ".join(reasons)[:500] or None})
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), stats


def crawl(days: int = 3, limit_filings: int | None = None, realtime: bool = False, path: Path | str = PANEL_DB,
          http: Http | None = None, today: dt.date | None = None) -> dict:
    """Read known accessions (read-only, seconds), fetch (no connection), write rows + the run row (seconds)."""
    started = pd.Timestamp.now()
    today = today or dt.date.today()
    http = http or Http(user_agent())
    known = known_accessions(path, today - dt.timedelta(days=days + 3))
    rows, stats = fetch(days, limit_filings, realtime, known, http, today)
    with PanelStore(path) as store:
        stats["rows"] = store.insert("insider_tx", rows) if not rows.empty else 0
        record_run(store.con, {"source": "form4_realtime" if realtime else "form4_daily", "started_at": started,
                               "finished_at": pd.Timestamp.now(), "status": stats["status"],
                               "n_requests": http.n_requests, "n_failed": http.n_failed,
                               "n_items": stats["filings"] + stats["skipped_known"], "n_rows": stats["rows"],
                               "reason": stats["reason"]})
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--limit-filings", type=int, default=None, help="cap per day, for testing")
    ap.add_argument("--realtime", action="store_true", help="use full-text search (same-day) instead of the daily index")
    args = ap.parse_args()
    # start marker: the 06:00 job logs to collect.log with no wrapper script; agent.health reads this line
    print(f"=== form4 {'realtime' if args.realtime else 'daily'} {time.strftime('%a %b %d %H:%M:%S %Z %Y')} ===", flush=True)
    stats = crawl(args.days, args.limit_filings, args.realtime)
    print(stats)
    return 1 if stats["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
