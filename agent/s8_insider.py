"""S8: do Form 4 insider buys predict anything, on our own panel?

The first signal built from a primary source rather than the price series
(docs/AGENT_PLAN.md §9, S7's conclusion). Two pre-registered hypotheses,
both long-only in sign:

  insider_buy      any open-market purchase filed in the trailing window,
                   scaled by the dollar value over 20-day dollar volume
  insider_cluster  the same, but only when >= 2 distinct insiders bought
                   (the classic "cluster buy"; single buys are noisier)

Point-in-time by construction: a row is visible from its FILING_DATE, not
its transaction date — Form 4 is due two business days after the trade,
and the median lag matters.

Sparse track (few names per day have a buy), so this is an event study
rather than a cross-sectional IC: for every (ticker, filing date) the
forward return is compared against the same-day mean of the whole index,
with a stationary bootstrap over event dates and a per-year breakdown.
A long-only top-K book is reported too, for comparison with S7.

Usage: python -m agent.s8_insider [--start 2015-01-01] [--windows 5,20]
Writes site-data/validation/s8_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")


def load_events(store: PanelStore, start: str) -> pd.DataFrame:
    return store.con.execute("""
        SELECT filing_date, ticker, count(DISTINCT owner_name) AS n_buyers,
               sum(value_usd) AS buy_usd, max(CASE WHEN relationship LIKE '%Officer%'
                                                   OR relationship LIKE '%Director%' THEN 1 ELSE 0 END) AS insider_role
        FROM insider_tx
        WHERE trans_code = 'P' AND acq_disp = 'A' AND filing_date >= ?
        GROUP BY 1, 2""", [start]).df()


def event_stats(ev: pd.DataFrame, fwd: pd.DataFrame, mkt: pd.Series, horizon: int, label: str) -> dict:
    """Mean abnormal return of the events, clustered by filing date."""
    rows = []
    for d, g in ev.groupby("date"):
        if d not in fwd.index:
            continue
        r = fwd.loc[d].reindex(g["ticker"]).dropna()
        if r.empty or pd.isna(mkt.get(d, np.nan)):
            continue
        rows.append({"date": d, "n": len(r), "abn": float(r.mean() - mkt.loc[d]), "raw": float(r.mean())})
    if len(rows) < 30:
        return {"label": label, "n_dates": len(rows)}
    df = pd.DataFrame(rows).set_index("date").sort_index()
    x = df["abn"].to_numpy()
    boot = bootstrap_ci(x, n_boot=2000)
    by_year = df["abn"].groupby(df.index.year).mean()
    return {"label": label, "horizon": horizon, "n_events": int(df["n"].sum()), "n_dates": int(len(df)),
            "mean_abn_pct": float(x.mean()), "median_abn_pct": float(np.median(x)),
            "mean_raw_pct": float(df["raw"].mean()), "t_nw": newey_west_t(x, lag=max(horizon // 5, 1)),
            "boot_ci95": [boot["lo"], boot["hi"]], "hit_rate": float((x > 0).mean()),
            "share_years_positive": float((by_year > 0).mean()),
            "first_half": float(df["abn"].iloc[: len(df) // 2].mean()),
            "second_half": float(df["abn"].iloc[len(df) // 2:].mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--horizons", default="5,10,21,63")
    args = ap.parse_args()
    horizons = [int(h) for h in args.horizons.split(",")]

    with PanelStore(read_only=True) as store:
        ev = load_events(store, args.start)
        close, adj = store.bars_wide("close", start=args.start), store.bars_wide("adj_close", start=args.start)
        opn = store.bars_wide("open", start=args.start)
        mask = store.membership_mask(adj.index, sorted(adj.columns))
        spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()

    adj_open = opn * (adj / close)
    ev["date"] = pd.to_datetime(ev["filing_date"])
    # a filing is actionable at the next open, the same convention as S1/S2/S7
    idx = adj.index
    ev = ev[ev["date"].isin(idx)]
    ev = ev[ev["ticker"].isin(adj.columns)]

    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "source": "SEC Form 345 quarterly data sets, open-market buys (code P), by FILING date",
              "n_event_rows": int(len(ev)), "n_tickers": int(ev["ticker"].nunique()),
              "median_filing_lag_days": None, "horizons": {}}

    for h in horizons:
        fwd = ((adj.shift(-h) / adj_open.shift(-1) - 1) * 100).where(mask)
        mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
        block = {"insider_buy": event_stats(ev, fwd, mkt, h, "any buy"),
                 "insider_cluster": event_stats(ev[ev["n_buyers"] >= 2], fwd, mkt, h, ">=2 buyers"),
                 "insider_big": event_stats(ev[ev["buy_usd"] >= 250_000], fwd, mkt, h, ">= $250k")}
        report["horizons"][h] = block

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s8_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S8 report — Form 4 insider buys ({stamp})", "",
         f"{args.start} → today, point-in-time S&P 500, event = a Form 4 open-market purchase, dated by its "
         f"FILING date and entered at the next open. Abnormal = event mean minus the index that day.",
         f"{report['n_event_rows']} (ticker, filing-date) events across {report['n_tickers']} names.", "",
         "| variant | h | events | dates | mean abn | NW t | boot CI95 | hit | yrs>0 | 1st half | 2nd half |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for h, block in report["horizons"].items():
        for name, s in block.items():
            if s.get("n_dates", 0) < 30:
                L.append(f"| {name} | {h} | — | {s.get('n_dates', 0)} | too few events | | | | | | |")
                continue
            L.append(f"| {name} ({s['label']}) | {h} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.3f}% | "
                     f"{s['t_nw']:+.2f} | [{s['boot_ci95'][0]:+.3f}, {s['boot_ci95'][1]:+.3f}] | {s['hit_rate']:.0%} | "
                     f"{s['share_years_positive']:.0%} | {s['first_half']:+.3f}% | {s['second_half']:+.3f}% |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
