"""Today's Form 4 filings from EDGAR's daily index → panel.insider_tx.

The quarterly bulk data sets (agent/sources/sec_form4.py) lag by months,
which is fine for research and useless for trading. EDGAR publishes a
daily index of every filing; this pulls the Form 4s from it and parses the
XML, so the live signal sees a filing the morning after it lands.

Only open-market purchases are kept (transactionCode P with a positive
price), matching the research definition in S11/S13.

SEC asks for 10 requests/second at most and a contact in the User-Agent
(SEC_USER_AGENT in ~/.hedge-fund/.env).

Usage: python -m agent.sources.sec_daily_form4 [--days 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

IDX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
PAUSE = 0.12                      # ~8 requests/second, inside SEC's limit


def _get(url: str, ua: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def index_form4(day: dt.date, ua: str) -> list[str]:
    """Paths of the Form 4 filings indexed for that day."""
    text = _get(IDX.format(y=day.year, q=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d")), ua)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        if not line.startswith("4 ") and not line.startswith("4/A "):
            continue
        parts = line.split()
        if parts and parts[-1].endswith(".txt"):
            out.append(parts[-1])
    return out


def _xml_url(path: str) -> str:
    """edgar/data/CIK/0001234567-26-000123.txt -> the filing's index directory."""
    acc = path.rsplit("/", 1)[-1].replace(".txt", "")
    return f"https://www.sec.gov/Archives/{path.rsplit('/', 1)[0]}/{acc.replace('-', '')}/"


def parse_filing(path: str, ua: str) -> list[dict]:
    """Fetch a filing's XML and return its open-market purchase rows."""
    listing = _get(_xml_url(path), ua)
    if not listing:
        return []
    m = re.findall(r'href="([^"]+\.xml)"', listing)
    doc = next((u for u in m if "xslF345" not in u), None)
    if not doc:
        return []
    raw = _get(f"https://www.sec.gov{doc}" if doc.startswith("/") else doc, ua)
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


def crawl(store: PanelStore, days: int = 3, limit_filings: int | None = None) -> dict:
    ua = user_agent()
    today = dt.date.today()
    stats = {"days": 0, "filings": 0, "rows": 0}
    frames = []
    for back in range(days):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        paths = index_form4(day, ua)
        time.sleep(PAUSE)
        if not paths:
            continue
        stats["days"] += 1
        for p in paths[:limit_filings]:
            rows = parse_filing(p, ua)
            time.sleep(PAUSE)
            stats["filings"] += 1
            if rows:
                df = pd.DataFrame(rows)
                df["filing_date"] = day
                df["source"] = "edgar_daily"
                df["fetched_at"] = pd.Timestamp.now()
                frames.append(df.drop(columns=["period_of_report"]))
    if frames:
        stats["rows"] = store.insert("insider_tx", pd.concat(frames, ignore_index=True))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--limit-filings", type=int, default=None, help="cap per day, for testing")
    args = ap.parse_args()
    with PanelStore() as store:
        print(crawl(store, args.days, args.limit_filings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
