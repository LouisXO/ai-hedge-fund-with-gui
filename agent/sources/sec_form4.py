"""SEC Form 4 insider transactions → panel.insider_tx (free, point-in-time).

The quarterly "Insider Transactions Data Sets" are the as-filed record:
SUBMISSION.tsv carries FILING_DATE and ISSUERTRADINGSYMBOL, NONDERIV_TRANS
the transactions, REPORTINGOWNER the roles. Filing date is what a signal
may condition on — the transaction date is often days earlier and was not
public then.

Only open-market purchases and sales are kept: TRANS_CODE 'P'/'S' with a
positive price. Grants, option exercises and tax withholding (codes A, M,
F, and price 0) are noise for this purpose — they dominate row counts and
carry no view.

Note the SEC moved the newest quarter to a different directory, so both
are tried. Requests need a contact in the User-Agent (SEC_USER_AGENT in
~/.hedge-fund/.env) or EDGAR answers 403.

Usage:
  python -m agent.sources.sec_form4 load --from 2015q1 [--to 2026q2]
  python -m agent.sources.sec_form4 status
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import time
import urllib.request
import zipfile

import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import USER_DIR

ENV_PATH = USER_DIR / ".env"
URLS = ("https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
        "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{q}_form345.zip")
KEEP_CODES = ("P", "S")


def user_agent() -> str:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("SEC_USER_AGENT="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("set SEC_USER_AGENT=<name> <email> in ~/.hedge-fund/.env (EDGAR 403s without it)")


def quarters(first: str, last: str) -> list[str]:
    fy, fq = int(first[:4]), int(first[-1])
    ly, lq = int(last[:4]), int(last[-1])
    out = []
    while (fy, fq) <= (ly, lq):
        out.append(f"{fy}q{fq}")
        fy, fq = (fy + 1, 1) if fq == 4 else (fy, fq + 1)
    return out


def download(q: str, ua: str) -> zipfile.ZipFile | None:
    for url in URLS:
        req = urllib.request.Request(url.format(q=q), headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return zipfile.ZipFile(io.BytesIO(resp.read()))
        except Exception:
            continue
    return None


def parse(zf: zipfile.ZipFile, universe: set[str]) -> pd.DataFrame:
    def tsv(name, cols):
        with zf.open(name) as f:
            return pd.read_csv(f, sep="\t", usecols=cols, dtype=str, on_bad_lines="skip")

    sub = tsv("SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "ISSUERCIK", "ISSUERTRADINGSYMBOL",
                                 "DOCUMENT_TYPE"])
    sub = sub[sub["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    sub["ticker"] = sub["ISSUERTRADINGSYMBOL"].str.upper().str.strip()
    sub = sub[sub["ticker"].isin(universe)]
    if sub.empty:
        return pd.DataFrame()
    trans = tsv("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                                       "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
                                       "SHRS_OWND_FOLWNG_TRANS"])
    trans = trans[trans["TRANS_CODE"].isin(KEEP_CODES)]
    own = tsv("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
                                     "RPTOWNER_TITLE"])
    own = own.drop_duplicates("ACCESSION_NUMBER")          # one row per filing is enough for roles
    df = trans.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    if df.empty:
        return pd.DataFrame()
    num = lambda c: pd.to_numeric(df[c].str.replace(",", "", regex=False), errors="coerce")  # noqa: E731
    out = pd.DataFrame({
        "accession": df["ACCESSION_NUMBER"], "ticker": df["ticker"], "issuer_cik": df["ISSUERCIK"],
        "filing_date": pd.to_datetime(df["FILING_DATE"], errors="coerce").dt.date,
        "trans_date": pd.to_datetime(df["TRANS_DATE"], errors="coerce").dt.date,
        "owner_name": df["RPTOWNERNAME"], "relationship": df["RPTOWNER_RELATIONSHIP"],
        "officer_title": df["RPTOWNER_TITLE"], "trans_code": df["TRANS_CODE"],
        "acq_disp": df["TRANS_ACQUIRED_DISP_CD"], "shares": num("TRANS_SHARES"),
        "price": num("TRANS_PRICEPERSHARE"), "shares_after": num("SHRS_OWND_FOLWNG_TRANS"),
        "source": "sec_form345", "fetched_at": pd.Timestamp.now()})
    out["value_usd"] = out["shares"] * out["price"]
    # trans_date is part of the primary key, so a row without it cannot be stored
    out = out[(out["price"] > 0) & (out["shares"] > 0) & out["filing_date"].notna() & out["trans_date"].notna()]
    return out.drop_duplicates(subset=["accession", "ticker", "trans_date", "trans_code", "shares", "price"])


def load(store: PanelStore, first: str, last: str, pause: float = 0.5, force: bool = False) -> dict:
    ua = user_agent()
    universe = {t for _, m in store.membership_changes() for t in m}
    done = {r[0] for r in store.con.execute("SELECT quarter FROM insider_load_log").fetchall()} if not force else set()
    stats = {"quarters": 0, "rows": 0, "missing": []}
    for q in quarters(first, last):
        if q in done:
            continue
        zf = download(q, ua)
        if zf is None:
            stats["missing"].append(q)
            continue
        df = parse(zf, universe)
        n = store.insert("insider_tx", df) if not df.empty else 0
        store.insert("insider_load_log", pd.DataFrame([{"quarter": q, "n_rows": int(n),
                                                        "n_tickers": int(df["ticker"].nunique()) if n else 0,
                                                        "loaded_at": pd.Timestamp.now()}]))
        stats["quarters"] += 1
        stats["rows"] += n
        print(f"  {q}: {n} rows", flush=True)
        time.sleep(pause)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["load", "status"])
    ap.add_argument("--from", dest="first", default="2015q1")
    ap.add_argument("--to", dest="last", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    today = dt.date.today()
    last = args.last or f"{today.year}q{(today.month - 1) // 3 + 1}"
    with PanelStore() as store:
        if args.cmd == "load":
            print(load(store, args.first, last, force=args.force))
        else:
            print(store.con.execute("""SELECT count(*) AS n_rows, count(DISTINCT ticker) AS n_tickers,
                                              min(filing_date) AS first_filing, max(filing_date) AS last_filing,
                                              sum(CASE WHEN trans_code='P' THEN 1 ELSE 0 END) AS n_buys
                                       FROM insider_tx""").df().to_dict("records"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
