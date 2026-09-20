"""Alpha Vantage LISTING_STATUS → panel.listing_status (free, includes delisted).

Complements agent/sources/sec_form4.py's issuer_seen: Form 4 filings tell
us a ticker was alive in a quarter, this gives the exact ipoDate and
delistingDate per symbol plus the exchange and asset type. Both are free
and neither needs the premium tier.

Measured 2026-09-20: the daily-bar endpoint DOES serve delisted symbols
(NUAN returns bars through its 2022-03-04 delisting), but `outputsize=full`
is premium — the free tier caps at the last 100 bars, which is useless for
history. So this module gives the universe; the prices still need a paid
source (see docs/AGENT_PLAN.md §9).

Usage: python -m agent.sources.av_listing refresh
"""
from __future__ import annotations

import argparse
import csv
import io
import time
import urllib.parse
import urllib.request

import pandas as pd

from agent.sources.av_news import URL, api_key
from hedge_fund.features.panel import PanelStore


def fetch(state: str, date: str | None = None, tries: int = 4, wait: float = 20.0) -> pd.DataFrame:
    params = {"function": "LISTING_STATUS", "apikey": api_key(), "state": state}
    if date:
        params["date"] = date
    text = ""
    for attempt in range(tries):
        # the free tier answers a bare "{}" when it throttles, not an error code
        req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "optradar-agent"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode()
        if text.startswith("symbol,"):
            break
        time.sleep(wait)
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "symbol" not in rows[0]:
        raise RuntimeError(text[:300])
    df = pd.DataFrame(rows)
    return pd.DataFrame({"symbol": df["symbol"].str.upper(), "name": df["name"],
                         "exchange": df["exchange"], "asset_type": df["assetType"],
                         "ipo_date": pd.to_datetime(df["ipoDate"], errors="coerce").dt.date,
                         "delisting_date": pd.to_datetime(df["delistingDate"], errors="coerce").dt.date,
                         "status": df["status"], "fetched_at": pd.Timestamp.now()})


def refresh(store: PanelStore) -> dict:
    out = {}
    for state in ("active", "delisted"):
        df = fetch(state)
        out[state] = store.insert("listing_status", df)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh"])
    ap.parse_args()
    with PanelStore() as store:
        print(refresh(store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
