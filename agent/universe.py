"""Point-in-time S&P 500 membership → panel.db, and the data-ticker map.

Source: github.com/fja05680/sp500 "S&P 500 Historical Components & Changes
(Updated).csv" — one row per index change since 1996, listing every member
on that date. Members on day D = the latest row dated <= D.

Why not today's large caps: selecting on today's size/popularity keeps the
names that went up, which inflates momentum IC in any backtest (see
docs/AGENT_PLAN.md v2, change #3).

Symbols in the membership file are the ones in use at the time. Yahoo keeps
history only under a company's current symbol, and old symbols get reused
by unrelated securities (FB, BBBY). So:
- ALIASES maps a pure rename (same surviving entity, verified: old symbol's
  membership ends the day the new one's starts) to the symbol Yahoo files
  the history under. Mergers are deliberately excluded because the new
  symbol's older history belongs to the other company.
- backfill keeps only bars inside each symbol's own membership window (plus
  lookback), so a reused symbol never lends another company's prices.

Usage: python -m agent.universe refresh
"""
from __future__ import annotations

import argparse
import csv
import io
import urllib.request
from dataclasses import dataclass

import pandas as pd

from hedge_fund.features.panel import PanelStore

SOURCE_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
              "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
INDEX = "sp500"

# member symbol -> symbol Yahoo holds the history under. Renames only.
# Excluded on purpose (merger, the new symbol's history is another company's
# or a different share class): RTN->RTX, PX->LIN, DWDP->DD, HCP/PEAK->DOC,
# DISCK->WBD, KORS->CPRI, CELG->BMY.
ALIASES = {
    "FB": "META", "ANTM": "ELV", "ABC": "COR", "RE": "EG", "FLT": "CPAY",
    "PKI": "RVTY", "WLTW": "WTW", "TMK": "GL", "CTL": "LUMN", "BLL": "BALL",
    "SYMC": "GEN", "NLOK": "GEN", "COG": "CTRA", "UTX": "RTX", "FI": "FISV",
    "VIAC": "PARA", "CBS": "PARA", "BBT": "TFC", "HRS": "LHX", "MMC": "MRSH",
}


def data_ticker(member: str) -> str:
    """Yahoo symbol for a membership symbol ('BRK.B' -> 'BRK-B')."""
    return ALIASES.get(member, member).replace(".", "-")


@dataclass(frozen=True)
class Interval:
    ticker: str
    start: pd.Timestamp
    end: pd.Timestamp | None  # None = still a member


def fetch_changes(url: str = SOURCE_URL) -> list[tuple[str, list[str]]]:
    with urllib.request.urlopen(url, timeout=60) as resp:
        text = resp.read().decode()
    rows = list(csv.reader(io.StringIO(text)))
    return [(r[0], sorted(set(r[1].split(",")))) for r in rows[1:] if r and r[0]]


def intervals(changes: list[tuple[pd.Timestamp, frozenset[str]]]) -> list[Interval]:
    """Membership spans per symbol (a symbol can come and go several times)."""
    out: list[Interval] = []
    open_since: dict[str, pd.Timestamp] = {}
    prev: frozenset[str] = frozenset()
    for d, members in changes:
        for t in members - prev:
            open_since[t] = d
        for t in prev - members:
            out.append(Interval(t, open_since.pop(t), d))
        prev = members
    out.extend(Interval(t, s, None) for t, s in open_since.items())
    return out


def refresh(store: PanelStore) -> dict:
    changes = fetch_changes()
    n = store.replace_membership(INDEX, changes)
    return {"rows": n, "changes": len(changes), "first": changes[0][0], "last": changes[-1][0],
            "n_last": len(changes[-1][1])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh"])
    args = ap.parse_args()
    with PanelStore() as store:
        if args.cmd == "refresh":
            print(refresh(store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
