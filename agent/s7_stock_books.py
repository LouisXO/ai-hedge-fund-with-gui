"""S7: do the same signals survive as STOCK books, where there is no option hurdle?

S2 measured why the option book is hard: an ATM call must clear a 0.8-1.0%
breakeven over 7 days, which erases any small edge. A stock position has no
premium to recover — only transaction costs — so a signal that dies in
options can still be alive in shares. That is the whole question here.

Two books, both on the point-in-time S&P 500 panel, entered at the open
after the signal date (same convention as S1/S2):
  short  — weekly rebalance, horizons of 5 and 21 trading days
  long   — monthly/quarterly trend following, horizons of 21 and 63 days

Each is scored long-only (top-K), short-only (bottom-K) and long-short,
gross and net of `--cost-bps` per side, with a Newey-West t on the
per-rebalance series and a by-year breakdown. Annualization assumes the
position is held for the full horizon and rolled.

Usage: python -m agent.s7_stock_books [--start 2015-01-01] [--top 20] [--cost-bps 5]
Writes site-data/validation/s7_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from agent.s3_signals import resid_reversal_5
from hedge_fund.features.factors import mom_12_1, reversal_5
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")
TRADING_DAYS = 252


def leg_returns(sig: pd.DataFrame, fwd: pd.DataFrame, k: int, cost_bps: float) -> dict:
    """Per-rebalance mean return of the top-k, bottom-k and the long-short book."""
    both = sig.notna() & fwd.notna()
    s, r = sig.where(both), fwd.where(both)
    hi = s.rank(axis=1, ascending=False, method="first")
    lo = s.rank(axis=1, ascending=True, method="first")
    top = r.where(hi <= k).mean(axis=1)
    bot = r.where(lo <= k).mean(axis=1)
    mkt = r.mean(axis=1)
    cost = 2 * cost_bps / 100          # in %, round trip on one leg
    return {"top": (top - cost).dropna(), "bottom": (bot - cost).dropna(),
            "ls": (top - bot - 2 * cost).dropna(), "market": mkt.dropna(),
            "top_gross": top.dropna(), "excess": (top - mkt - cost).dropna()}


def summarize(series: pd.Series, h: int) -> dict:
    x = series.to_numpy()
    if len(x) < 12:
        return {"n": int(len(x))}
    per_year = TRADING_DAYS / h
    boot = bootstrap_ci(x, n_boot=1000)
    by_year = series.groupby(series.index.year).mean()
    return {"n": int(len(x)), "mean_pct": float(x.mean()), "median_pct": float(np.median(x)),
            "ann_pct": float(x.mean() * per_year), "vol_ann_pct": float(x.std() * np.sqrt(per_year)),
            "sharpe": float(x.mean() / x.std() * np.sqrt(per_year)) if x.std() else float("nan"),
            "t_nw": newey_west_t(x, lag=2), "boot_ci95": [boot["lo"], boot["hi"]],
            "hit_rate": float((x > 0).mean()), "worst_pct": float(x.min()),
            "share_years_positive": float((by_year > 0).mean()),
            "first_half": float(series.iloc[: len(series) // 2].mean()),
            "second_half": float(series.iloc[len(series) // 2:].mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    args = ap.parse_args()

    with PanelStore(read_only=True) as store:
        lb = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).date().isoformat()
        close, adj = store.bars_wide("close", start=lb), store.bars_wide("adj_close", start=lb)
        opn = store.bars_wide("open", start=lb)
        mask = store.membership_mask(adj.index, sorted(adj.columns))
        spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()

    adj_open = opn * (adj / close)
    books = {"short": {"horizons": (5, 21), "every": 5,
                       "signals": {"reversal_5": reversal_5(adj), "resid_reversal_5": resid_reversal_5(adj, spy)}},
             "long": {"horizons": (21, 63), "every": 21,
                      "signals": {"mom_12_1": mom_12_1(adj)}}}

    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "end": str(adj.index[-64].date()), "top_k": args.top, "cost_bps_per_side": args.cost_bps,
              "entry": "open of t+1", "note": "stock books — no option premium to recover", "books": {}}

    for book, cfg in books.items():
        rows = adj.loc[args.start:adj.index[-64]].index[::cfg["every"]]
        m = mask.loc[rows]
        out = {}
        for h in cfg["horizons"]:
            fwd = (adj.shift(-h).loc[rows] / adj_open.shift(-1).loc[rows] - 1) * 100
            fwd = fwd.where(m)
            mkt_h = (spy.shift(-h).loc[rows] / spy.shift(-1).loc[rows] - 1) * 100
            for name, sig in cfg["signals"].items():
                legs = leg_returns(sig.loc[rows].where(m), fwd, args.top, args.cost_bps)
                entry = {leg: summarize(s, h) for leg, s in legs.items() if leg != "market"}
                entry["market_mean_pct"] = float(mkt_h.dropna().mean())
                out[f"{name}|h{h}"] = entry
        report["books"][book] = out

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s7_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S7 report — stock books ({stamp})", "",
         f"{args.start} → {report['end']}, point-in-time S&P 500, entry at next open, top-{args.top} names, "
         f"{args.cost_bps}bp per side. Annualized by rolling the holding period.", ""]
    for book, out in report["books"].items():
        L += [f"## {book} book", "",
              "| signal / horizon | leg | n | mean | ann | vol | Sharpe | NW t | boot CI95 | hit | yrs>0 | 1st | 2nd |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for key, entry in out.items():
            for leg in ("top", "bottom", "ls", "excess"):
                s = entry.get(leg, {})
                if s.get("n", 0) < 12:
                    continue
                L.append(f"| {key} | {leg} | {s['n']} | {s['mean_pct']:+.2f}% | {s['ann_pct']:+.1f}% | "
                         f"{s['vol_ann_pct']:.1f}% | {s['sharpe']:+.2f} | {s['t_nw']:+.2f} | "
                         f"[{s['boot_ci95'][0]:+.2f}, {s['boot_ci95'][1]:+.2f}] | {s['hit_rate']:.0%} | "
                         f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f}% | {s['second_half']:+.2f}% |")
            L.append(f"| {key} | market | — | {entry['market_mean_pct']:+.2f}% | | | | | | | | | |")
        L.append("")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
