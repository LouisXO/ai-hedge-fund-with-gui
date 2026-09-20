"""S12: what survives of the insider edge after the spread?

S11 measured +0.607% over 5 days in micro caps (t 4.91), but micro caps
are where the spread lives too, and we have no historical quotes. So the
spread is estimated from the bars we do have:

  Corwin & Schultz (2012) high-low estimator — two consecutive days of
  highs and lows separate the price move from the bid-ask bounce. Negative
  estimates (noise) are floored at zero and averaged over a month, which
  is how the original paper recommends using it.

A round trip pays the full spread (buy at ask, sell at bid), which is the
same convention optradar's paper ledger already uses for options.

Reported per size bucket: gross abnormal return, the estimated spread, the
net, and the breakeven spread — the cost level at which the edge vanishes.

Usage: python -m agent.s12_insider_costs [--start 2016-01-01] [--min-adv 1e6]
Writes site-data/validation/s12_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from agent.s11_insider_wide import listed_mask
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")
K = 3 - 2 * np.sqrt(2)


def corwin_schultz(high: pd.DataFrame, low: pd.DataFrame) -> pd.DataFrame:
    """Daily high-low spread estimate in %, floored at zero, per name."""
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(high / low) ** 2
        beta = hl + hl.shift(-1)
        h2 = np.maximum(high, high.shift(-1))
        l2 = np.minimum(low, low.shift(-1))
        gamma = np.log(h2 / l2) ** 2
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / K - np.sqrt(gamma / K)
        s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return (s.clip(lower=0) * 100).replace([np.inf, -np.inf], np.nan)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--min-adv", type=float, default=1e6)
    ap.add_argument("--horizons", default="5,10")
    args = ap.parse_args()
    horizons = [int(h) for h in args.horizons.split(",")]

    with PanelStore(read_only=True) as store:
        ev = store.con.execute("""
            SELECT filing_date, ticker, count(DISTINCT owner_name) AS n_buyers, sum(value_usd) AS buy_usd
            FROM insider_tx WHERE trans_code='P' AND acq_disp='A' AND filing_date >= ?
            GROUP BY 1,2""", [args.start]).df()
        close = store.bars_wide("close", start=args.start)
        adj = store.bars_wide("adj_close", start=args.start)
        opn, high, low = (store.bars_wide(f, start=args.start) for f in ("open", "high", "low"))
        vol = store.bars_wide("volume", start=args.start)
        spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()
        listed = listed_mask(store, adj.index, list(adj.columns))

    adj_open = opn * (adj / close)
    adv20 = (close * vol).rolling(20).mean()
    tradable = listed & (adv20 >= args.min_adv) & adj.notna()
    # monthly mean of the daily estimate, shifted so an event only sees the past
    spread = corwin_schultz(high, low).rolling(21).mean().shift(1)

    ev["date"] = pd.to_datetime(ev["filing_date"])
    ev = ev[ev["date"].isin(adj.index) & ev["ticker"].isin(adj.columns)]
    ev = ev[[tradable.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]]
    ev["adv"] = [adv20.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]
    ev["spread"] = [spread.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]
    ev["bucket"] = pd.cut(ev["adv"], [-np.inf, 3e6, 2e7, 1e8, np.inf],
                          labels=["micro", "small", "mid", "large"])
    ev = ev.dropna(subset=["spread"])

    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "min_adv_usd": args.min_adv, "spread_model": "Corwin-Schultz high-low, 21-day mean, lagged 1 day",
              "cost_convention": "one full round-trip spread per trade", "n_events": int(len(ev)),
              "horizons": {}}

    for h in horizons:
        fwd = ((adj.shift(-h) / adj_open.shift(-1) - 1) * 100).where(tradable)
        mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
        block = {}
        for name, sub in [("all", ev), ("cluster", ev[ev["n_buyers"] >= 2]),
                          *[(f"size_{b}", ev[ev["bucket"] == b]) for b in ("micro", "small", "mid", "large")],
                          ("cluster_small+", ev[(ev["n_buyers"] >= 2) & ev["bucket"].isin(["small", "mid"])])]:
            rows = []
            for d, g in sub.groupby("date"):
                if d not in fwd.index or pd.isna(mkt.get(d, np.nan)):
                    continue
                r = fwd.loc[d].reindex(g["ticker"])
                ok = r.notna().to_numpy()
                if not ok.any():
                    continue
                rows.append({"date": d, "gross": float(r[ok].mean() - mkt.loc[d]),
                             "spread": float(np.nanmean(g["spread"].to_numpy()[ok]))})
            if len(rows) < 30:
                block[name] = {"n_dates": len(rows)}
                continue
            df = pd.DataFrame(rows).set_index("date").sort_index()
            net = (df["gross"] - df["spread"]).to_numpy()
            boot = bootstrap_ci(net, n_boot=2000)
            block[name] = {"n_dates": int(len(df)), "gross_pct": float(df["gross"].mean()),
                           "spread_pct": float(df["spread"].mean()),
                           "spread_median_pct": float(df["spread"].median()),
                           "net_pct": float(net.mean()), "net_t_nw": newey_west_t(net, lag=max(h // 5, 1)),
                           "net_ci95": [boot["lo"], boot["hi"]],
                           "breakeven_spread_pct": float(df["gross"].mean()),
                           "net_positive_years": float((df.assign(net=net).groupby(df.index.year)["net"]
                                                        .mean() > 0).mean())}
        report["horizons"][h] = block

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s12_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S12 report — insider edge after the spread ({stamp})", "",
         f"{args.start} → today, ADV floor ${args.min_adv:,.0f}, {report['n_events']} events. "
         f"Spread from Corwin-Schultz high-low (21-day mean, lagged); one full round trip charged per trade.", "",
         "| group | h | dates | gross | spread | net | net NW t | net CI95 | yrs net>0 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for h, block in report["horizons"].items():
        for name, s in block.items():
            if s.get("n_dates", 0) < 30:
                continue
            L.append(f"| {name} | {h} | {s['n_dates']} | {s['gross_pct']:+.3f}% | {s['spread_pct']:.3f}% | "
                     f"{s['net_pct']:+.3f}% | {s['net_t_nw']:+.2f} | "
                     f"[{s['net_ci95'][0]:+.3f}, {s['net_ci95'][1]:+.3f}] | {s['net_positive_years']:.0%} |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
