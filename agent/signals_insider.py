"""The insider-buy candidate list, in the form the daily run and the ledger need.

Rules come straight from what S11-S13 measured, not from taste:
- open-market purchases only, dated by FILING date (S11);
- keep a name if the filings in the trailing window are either a cluster
  (>= 2 distinct insiders) or large (>= $250k), the two cuts that showed
  an edge (S11);
- ADV floor and ceiling: skip micro caps, where the 1.41% spread eats the
  0.60% gross edge, and skip large caps, where the edge is 0.07% (S13);
- entry is a LIMIT order, never a market order: at a full spread the edge
  is zero, at half it is +0.28% (t 2.57), passive +0.39% (t 3.60). So the
  candidate carries a limit reference price and the live quoted spread,
  and the ledger records what the fill would have cost.

Usage: python -m agent.signals_insider [--date YYYY-MM-DD] [--window 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import numpy as np
import pandas as pd

from agent.s13_real_spreads import BUCKETS, LABELS
from hedge_fund.features.panel import PanelStore

MIN_ADV = 3e6              # above micro: its spread is wider than the edge
MAX_ADV = 1e8              # below large: the edge there is 0.07%, i.e. nothing
BIG_USD = 250_000
HOLD_DAYS = 5
SIGNAL = "insider_buy"
VERSION = "1"


def candidates(store: PanelStore, as_of: pd.Timestamp, window: int = 3) -> pd.DataFrame:
    """Names with a qualifying Form 4 purchase filed in the last `window` days."""
    start = (as_of - pd.Timedelta(days=window)).date()
    ev = store.con.execute("""
        SELECT ticker, max(filing_date) AS last_filing, count(DISTINCT owner_name) AS n_buyers,
               sum(value_usd) AS buy_usd, max(officer_title) AS title
        FROM insider_tx
        WHERE trans_code = 'P' AND acq_disp = 'A' AND filing_date BETWEEN ? AND ?
        GROUP BY 1""", [start, as_of.date()]).df()
    if ev.empty:
        return ev
    ev = ev[(ev["n_buyers"] >= 2) | (ev["buy_usd"] >= BIG_USD)]

    close = store.bars_wide("close", start=(as_of - pd.Timedelta(days=120)).date().isoformat())
    vol = store.bars_wide("volume", start=(as_of - pd.Timedelta(days=120)).date().isoformat())
    adv = (close * vol).rolling(20).mean()
    day = close.index[close.index <= as_of][-1]
    ev = ev[ev["ticker"].isin(close.columns)]
    ev["adv20"] = [float(adv.at[day, t]) if t in adv.columns else np.nan for t in ev["ticker"]]
    ev["last_close"] = [float(close.at[day, t]) if t in close.columns else np.nan for t in ev["ticker"]]
    ev = ev.dropna(subset=["adv20", "last_close"])
    ev["bucket"] = pd.cut(ev["adv20"], BUCKETS, labels=LABELS)
    ev["reason"] = np.where(ev["adv20"] < MIN_ADV, "adv_below_floor",
                            np.where(ev["adv20"] > MAX_ADV, "adv_above_ceiling", ""))
    ev["eligible"] = ev["reason"] == ""
    ev["kind"] = np.where(ev["n_buyers"] >= 2, "cluster", "big_usd")
    ev["bar_date"] = day.date()
    return ev.sort_values(["eligible", "buy_usd"], ascending=[False, False])


def expected_edge(store: PanelStore, bucket: str, year: int, spread_fraction: float = 0.5) -> dict:
    """S13's measured numbers for this bucket, so the brief can show what is expected."""
    row = store.con.execute("""SELECT median_spread_pct FROM spread_grid WHERE bucket = ? AND year = ?""",
                            [bucket, year]).fetchone()
    spread = float(row[0]) if row else float("nan")
    gross = {"small": 0.378, "mid": 0.190, "micro": 0.598, "large": 0.073}.get(bucket, float("nan"))
    return {"gross_pct_5d": gross, "quoted_spread_pct": spread,
            "net_at_half_spread_pct": gross - spread_fraction * spread}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--window", type=int, default=3)
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        as_of = pd.Timestamp(args.date) if args.date else pd.Timestamp(dt.date.today())
        df = candidates(store, as_of, args.window)
        if df.empty:
            print("no qualifying filings in the window")
            return 0
        year = as_of.year
        out = []
        for r in df.itertuples():
            edge = expected_edge(store, str(r.bucket), year)
            out.append({"ticker": r.ticker, "kind": r.kind, "n_buyers": int(r.n_buyers),
                        "buy_usd": round(float(r.buy_usd)), "bucket": str(r.bucket),
                        "adv20_usd": round(float(r.adv20)), "limit_ref": round(float(r.last_close), 2),
                        "eligible": bool(r.eligible), "reason": r.reason, **edge})
        print(json.dumps(out[:25], indent=1))
        print(f"{sum(o['eligible'] for o in out)} eligible of {len(out)} qualifying names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
