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

Rows are keyed by (symbol, status, ipo_date): one row per listing, so a ticker
used by two companies keeps both, and a refresh adds to the history instead
of replacing it (hedge_fund/features/panel.py, write_listing).

Usage: python -m agent.sources.av_listing refresh
       python -m agent.sources.av_listing migrate     # once: (symbol, status) key -> (symbol, status, ipo_date)
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


def _get(params: dict) -> str:
    """The only network call in this module; tests pass their own."""
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode({**params, 'apikey': api_key()})}",
                                 headers={"User-Agent": "optradar-agent"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode()


def fetch(state: str, date: str | None = None, tries: int = 4, wait: float = 20.0, get=_get) -> pd.DataFrame:
    params = {"function": "LISTING_STATUS", "state": state}
    if date:
        params["date"] = date
    text = ""
    for attempt in range(tries):
        # the free tier answers a bare "{}" when it throttles, not an error code
        text = get(params)
        if text.startswith("symbol,"):
            break
        if attempt < tries - 1:
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


def download(**kw) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Both lists, before anything is written: a failed download leaves the table as it was."""
    return fetch("active", **kw), fetch("delisted", **kw)


def refresh(store: PanelStore, **kw) -> dict:
    return store.write_listing(*download(**kw))


def migrate(store: PanelStore) -> dict:
    """Once per database: the key change, then the Active rows that were already stale are closed with their
    start kept (PanelStore.close_stale_listings). Running it again changes nothing."""
    out = store.migrate_listing_key()
    store.con.execute("BEGIN")
    try:
        out.update(store.close_stale_listings())
        store.con.execute("COMMIT")
    except Exception:
        store.con.execute("ROLLBACK")
        raise
    out["rows_now"] = store.con.execute("SELECT count(*) FROM listing_status").fetchone()[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh", "migrate"])
    args = ap.parse_args()
    if args.cmd == "migrate":
        with PanelStore() as store:
            print(migrate(store))
        return 0
    # download first: a throttled vendor can take minutes, and an open PanelStore locks panel.db for everyone
    lists = download()
    with PanelStore() as store:
        print(store.write_listing(*lists))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
