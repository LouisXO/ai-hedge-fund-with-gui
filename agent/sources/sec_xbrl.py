"""Point-in-time fundamentals from SEC XBRL companyfacts → panel.xbrl_facts.

Free, and the only free source that is point-in-time: every fact carries the
`filed` date of the filing it came from, so a factor built on day D can use
exactly what was public on D. (The frames API is one call per tag-period
for all companies, but it lacks `filed`, so it is not used.)

One request per CIK (~8,100 for names in the price panel), each answering
with the company's entire fact history; only the tags below are kept,
which turns ~15 GB of JSON into a few hundred MB of rows.

Tags (us-gaap unless noted) — the inputs to the standard factor set:
  NetIncomeLoss, Revenues / RevenueFromContract..., GrossProfit,
  CostOfRevenue, OperatingIncomeLoss, NetCashProvidedByUsedInOperatingActivities
  (flows: quarterly 10-Q values and annual 10-K values, both kept);
  Assets, StockholdersEquity, LongTermDebtNoncurrent,
  CommonStockSharesOutstanding, dei:EntityCommonStockSharesOutstanding (instants).

Usage: python -m agent.sources.sec_xbrl load [--since 2014-01-01] [--limit N]
       python -m agent.sources.sec_xbrl status
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TAGS = {
    "us-gaap": ["NetIncomeLoss", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "GrossProfit", "CostOfRevenue", "CostOfGoodsAndServicesSold",
                "OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities", "Assets",
                "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                "LongTermDebtNoncurrent", "LongTermDebt", "CommonStockSharesOutstanding",
                "WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
    "dei": ["EntityCommonStockSharesOutstanding"],
}
DDL = [
    """CREATE TABLE IF NOT EXISTS xbrl_facts (
        cik INT, ns VARCHAR, tag VARCHAR, unit VARCHAR, period_start DATE, period_end DATE, is_instant BOOLEAN,
        val DOUBLE,
        accn VARCHAR, fy INT, fp VARCHAR, form VARCHAR, filed DATE, frame VARCHAR,
        PRIMARY KEY (cik, ns, tag, period_start, period_end, accn))""",
    """CREATE TABLE IF NOT EXISTS xbrl_load_log (
        cik INT PRIMARY KEY, status VARCHAR, n_rows INT, fetched_at TIMESTAMP)""",
]
PAUSE = 0.11


def fetch(cik: int, ua: str) -> dict | None:
    req = urllib.request.Request(URL.format(cik=cik), headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                body = gzip.decompress(body)
            return json.loads(body)
    except Exception:
        return None


def rows_from(cik: int, doc: dict, since: str) -> list[dict]:
    out = []
    facts = doc.get("facts") or {}
    for ns, tags in TAGS.items():
        for tag in tags:
            units = (facts.get(ns) or {}).get(tag, {}).get("units") or {}
            for unit, items in units.items():
                if unit not in ("USD", "shares"):
                    continue
                for it in items:
                    if it.get("filed", "") < since or it.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"):
                        continue
                    # balance-sheet facts are instants: no start. Store start = end so the key is
                    # never null, and flag them so nobody treats them as a flow.
                    out.append({"cik": cik, "ns": ns, "tag": tag, "unit": unit,
                                "period_start": it.get("start") or it["end"], "period_end": it["end"],
                                "is_instant": "start" not in it, "val": it["val"],
                                "accn": it["accn"], "fy": it.get("fy"), "fp": it.get("fp"), "form": it["form"],
                                "filed": it["filed"], "frame": it.get("frame")})
    return out


def load(store: PanelStore, since: str, limit: int | None = None, retry_failed: bool = False,
         workers: int = 4) -> dict:
    ua = user_agent()
    for stmt in DDL:
        store.con.execute(stmt)
    ciks = store.con.execute("""
        SELECT DISTINCT CAST(i.cik AS INT) AS cik FROM issuer_seen i
        WHERE i.ticker IN (SELECT DISTINCT ticker FROM bars) ORDER BY 1""").df()["cik"].tolist()
    done_q = "SELECT cik FROM xbrl_load_log" + ("" if retry_failed else "")
    done = {r[0] for r in store.con.execute(done_q).fetchall()}
    if retry_failed:
        done = {r[0] for r in store.con.execute("SELECT cik FROM xbrl_load_log WHERE status='ok'").fetchall()}
    todo = [c for c in ciks if c not in done][:limit]
    stats = {"todo": len(todo), "ok": 0, "missing": 0, "rows": 0}
    batch, log = [], []
    # download-bound (~1.8 s per company single-threaded); a few threads stay well
    # inside SEC's 10 requests/second while cutting the wall clock by 4x
    from concurrent.futures import ThreadPoolExecutor

    def _fetch(cik):
        time.sleep(PAUSE)
        return cik, fetch(cik, ua)

    pool = ThreadPoolExecutor(max_workers=workers)
    for i, (cik, doc) in enumerate(pool.map(_fetch, todo), 1):
        if doc is None:
            log.append({"cik": cik, "status": "missing", "n_rows": 0, "fetched_at": pd.Timestamp.now()})
            stats["missing"] += 1
        else:
            rows = rows_from(cik, doc, since)
            batch.extend(rows)
            log.append({"cik": cik, "status": "ok", "n_rows": len(rows), "fetched_at": pd.Timestamp.now()})
            stats["ok"] += 1
        if i % 100 == 0 or i == len(todo):
            if batch:
                df = pd.DataFrame(batch)
                for c in ("period_start", "period_end", "filed"):
                    df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
                df = df.dropna(subset=["period_end", "filed"])
                # a malformed start date coerces to NaT; the key must not be null
                df["period_start"] = df["period_start"].where(df["period_start"].notna(), df["period_end"])
                df = df.drop_duplicates(subset=["cik", "ns", "tag", "period_start", "period_end", "accn"])
                stats["rows"] += store.insert("xbrl_facts", df)
                batch = []
            store.insert("xbrl_load_log", pd.DataFrame(log))
            log = []
            print(f"  [{i}/{len(todo)}] ok {stats['ok']} missing {stats['missing']} rows {stats['rows']}", flush=True)
    pool.shutdown()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["load", "status"])
    ap.add_argument("--since", default="2014-01-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()
    with PanelStore() as store:
        if args.cmd == "load":
            print(load(store, args.since, args.limit, args.retry_failed))
        else:
            for stmt in DDL:
                store.con.execute(stmt)
            print(store.con.execute("""SELECT status, count(*) AS n FROM xbrl_load_log GROUP BY 1""").fetchall())
            print(store.con.execute("""SELECT tag, count(DISTINCT cik) AS companies, count(*) AS n
                                       FROM xbrl_facts GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
