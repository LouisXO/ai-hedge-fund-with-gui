"""SEC filings by form type via EDGAR full-text search → ~/.hedge-fund/agent/filings.db.

Forms loaded:
  424B5   prospectus supplement: a share offering has been priced (the dilution happens now)
  S-3     shelf registration (the right to sell shares later); S-3/A, S-3ASR included
  144     notice of a planned insider sale (electronic filing mandatory only since 2023-04, so
          earlier years are nearly empty — tests using 144 start 2023-06)

One row per accession with the subject company (issuer) CIK and ticker as EFTS shows them;
the point-in-time ticker comes from panel.issuer_seen when available (same helper as 13D).
Monthly windows keep every query under EFTS's 10,000-hit cap.

Usage:
  python -m agent.sources.sec_forms backfill --forms 424B5,S-3 --start 2016-06-01
  python -m agent.sources.sec_forms backfill --forms 144 --start 2023-04-01
  python -m agent.sources.sec_forms update [--days 7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import duckdb
import pandas as pd

from agent.sources.sec_13d import _NAME, _get_json, pit_ticker
from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

FILINGS_DB = AGENT_DIR / "filings.db"
EFTS = "https://efts.sec.gov/LATEST/search-index"
FORM_GROUPS = {"424B5": ["424B5"], "S-3": ["S-3", "S-3/A", "S-3ASR"], "144": ["144"]}
DDL = """CREATE TABLE IF NOT EXISTS sec_forms (accession VARCHAR, form VARCHAR, filed DATE, cik VARCHAR, name VARCHAR,
         ticker_display VARCHAR, ticker VARCHAR, filer VARCHAR, fetched_at TIMESTAMP, PRIMARY KEY (accession, form))"""


def search(form: str, start: dt.date, end: dt.date, ua: str) -> list[dict]:
    out, frm = [], 0
    while True:
        d = _get_json({"forms": form, "dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat(), "from": frm}, ua)
        if not d:
            break
        hits = d.get("hits", {}).get("hits", [])
        for h in hits:
            src = h["_source"]
            names = src.get("display_names") or []
            m = _NAME.match(names[0]) if names else None
            if not m:
                continue
            out.append({"accession": h["_id"].split(":")[0], "form": src.get("form", form), "filed": src.get("file_date"),
                        "subject_cik": m.group("cik"), "name": m.group("name").strip(), "ticker_display": m.group("ticker"),
                        "filer": "; ".join(names[1:])[:200]})
        total = d["hits"]["total"]["value"]
        frm += len(hits)
        if not hits or frm >= total or frm >= 10_000:
            break
        time.sleep(0.12)
    return out


def load(groups: list[str], start: dt.date, end: dt.date, quiet: bool = False) -> int:
    ua = user_agent()
    rows: list[dict] = []
    cur = start
    while cur <= end:
        nxt = min((pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date(), end)
        for g in groups:
            for form in FORM_GROUPS[g]:
                part = search(form, cur, nxt, ua)
                rows += part
                if not quiet:
                    print(f"  {form} {cur} → {nxt}: {len(part)}", flush=True)
        cur = nxt + dt.timedelta(days=1)
    if not rows:
        return 0
    df = pd.DataFrame(rows).drop_duplicates(["accession", "form"])
    with PanelStore(read_only=True) as store:
        df["ticker"] = pit_ticker(store, df)
    df = df.rename(columns={"subject_cik": "cik"})
    df["fetched_at"] = pd.Timestamp.now()
    con = duckdb.connect(str(FILINGS_DB))
    con.execute(DDL)
    con.register("_f", df)
    con.execute("INSERT OR REPLACE INTO sec_forms SELECT accession, form, filed, cik, name, ticker_display, ticker, filer, fetched_at FROM _f")
    con.unregister("_f")
    con.close()
    return len(df)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update"])
    ap.add_argument("--forms", default="424B5,S-3,144")
    ap.add_argument("--start", default="2016-06-01")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    today = dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.cmd == "backfill" else today - dt.timedelta(days=args.days)
    print(load(args.forms.split(","), start, today, quiet=args.cmd == "update"), "filings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
