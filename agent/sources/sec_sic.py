"""SIC codes for every company in the fundamentals panel, from EDGAR's submissions API.

One request per CIK (data.sec.gov/submissions/CIK##########.json, ~8/s
with 4 threads, ~15 min for 7,900 companies). Written to a parquet file
rather than panel.db so it can run while backtests hold read-only
connections open. Industry groups (Fama-French 12) are derived in
agent/books/industry.py.

Usage: python -m agent.sources.sec_sic [--limit N]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

OUT = AGENT_DIR / "company_sic.csv"


def fetch(cik: int, ua: str) -> dict | None:
    req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": ua})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            return {"cik": cik, "sic": int(d["sic"]) if d.get("sic") else None, "sic_desc": d.get("sicDescription"),
                    "name": d.get("name"), "tickers": ",".join(d.get("tickers") or []),
                    "exchanges": ",".join(x for x in (d.get("exchanges") or []) if x), "state": d.get("stateOfIncorporation")}
        except Exception as exc:
            if "404" in str(exc):
                return {"cik": cik, "sic": None, "sic_desc": None, "name": None, "tickers": "", "exchanges": "", "state": None}
            time.sleep(2 * (attempt + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    ua = user_agent()
    with PanelStore(read_only=True) as store:
        ciks = [int(r[0]) for r in store.con.execute("SELECT DISTINCT cik FROM fundamentals_pit ORDER BY 1").fetchall()]
    if args.limit:
        ciks = ciks[:args.limit]
    done = pd.read_csv(OUT) if OUT.exists() else pd.DataFrame(columns=["cik"])
    todo = [c for c in ciks if c not in set(done["cik"])]
    print(f"{len(ciks)} companies, {len(todo)} to fetch", flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, r in enumerate(ex.map(lambda c: (time.sleep(0.45), fetch(c, ua))[1], todo), 1):
            if r:
                rows.append(r)
            if i % 500 == 0:
                pd.concat([done, pd.DataFrame(rows)], ignore_index=True).to_csv(OUT, index=False)
                print(f"  {i}/{len(todo)}", flush=True)
    out = pd.concat([done, pd.DataFrame(rows)], ignore_index=True).drop_duplicates("cik")
    out.to_csv(OUT, index=False)
    print({"rows": len(out), "with_sic": int(out["sic"].notna().sum())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
