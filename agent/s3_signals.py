"""S3: which (direction signal x cheapness gate) pair clears the option hurdle?

S2 showed the pass mark is not IC and not the raw move: it is
`move - that name's own breakeven`, because a high-vol pick pays a much
bigger hurdle (1.50% vs 0.91% at 7 days). So every candidate here is
scored in option terms, on the same modelled pricing as S2.

Direction candidates (pre-registered sign, computed on the point-in-time
S&P 500 panel):
  resid_reversal_5  minus the 5-day return residual to SPY (beta-adjusted)
  reversal_5        raw 5-day reversal (S1's borderline result, as control)
  mom_12_1          12-1 momentum (control)

Cheapness gates (the option leg — mandatory per S2):
  none              every name
  low_iv            IV below the cross-sectional median that day
  vol_reverting     RV20 < RV60 (the premium is struck off a lull, and
                    vol that mean-reverts up pays the buyer)
  low_iv + vol_reverting

The gate is what the model can honestly test: with IV = k x RV20 the
IV/RV ratio is constant by construction, so "cheap vs its own history"
is only visible through the RV20/RV60 term, and "cheap in the cross
section" only through the IV level. Real IV-vs-RV richness needs the
option archive S6 starts collecting.

Usage: python -m agent.s3_signals [--start 2015-01-01] [--top 5]
Writes site-data/validation/s3_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from agent.s2_option_hurdle import breakeven_move, option_pnl
from hedge_fund.features.factors import mom_12_1, reversal_5
from hedge_fund.features.panel import PanelStore
from hedge_fund.features.rv import yang_zhang
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")
SPREAD_PCT = 2.0
DTE = 37
K_IV = 1.15          # IV / realized-vol multiple; S2 grid showed 1.1-1.2 is the honest range


def resid_reversal_5(adj: pd.DataFrame, spy: pd.Series, window: int = 60) -> pd.DataFrame:
    """Minus the market-adjusted 5-day return (beta estimated on 60 daily returns)."""
    r = np.log(adj / adj.shift(1))
    m = np.log(spy / spy.shift(1)).reindex(r.index)
    cov = r.mul(m, axis=0).rolling(window).mean() - r.rolling(window).mean().mul(m.rolling(window).mean(), axis=0)
    beta = cov.div(m.rolling(window).var(ddof=0), axis=0)
    resid = r.sub(beta.mul(m, axis=0))
    return -resid.rolling(5).sum()


def evaluate(sel: pd.DataFrame, iv: pd.DataFrame, move: pd.DataFrame, right: str, hold: int,
             be: pd.DataFrame) -> dict:
    """Option-terms score for one (signal, gate, side) over the sampled dates."""
    sgn = 1 if right == "C" else -1
    pnl = option_pnl(iv.where(sel), move.where(sel), DTE, hold, SPREAD_PCT, right)
    edge = (sgn * move - be).where(sel)                      # move minus own breakeven
    per_date = pnl.mean(axis=1).dropna()
    arr = pnl.to_numpy()[np.isfinite(pnl.to_numpy())]
    e = edge.to_numpy()[np.isfinite(edge.to_numpy())]
    if arr.size < 100:
        return {"n": int(arr.size)}
    boot = bootstrap_ci(per_date.to_numpy(), n_boot=1000)
    by_year = per_date.groupby(per_date.index.year).mean()
    return {"n": int(arr.size), "n_dates": int(len(per_date)),
            "mean_pnl_pct": float(arr.mean()), "median_pnl_pct": float(np.median(arr)),
            "win_rate": float((arr > 0).mean()),
            "mean_edge_pct": float(e.mean()), "share_clearing_be": float((e > 0).mean()),
            "t_nw": newey_west_t(per_date.to_numpy(), lag=hold // 7 + 1),
            "boot_ci95": boot["lo"], "boot_ci95_hi": boot["hi"],
            "share_years_positive": float((by_year > 0).mean()),
            "first_half": float(per_date.iloc[: len(per_date) // 2].mean()),
            "second_half": float(per_date.iloc[len(per_date) // 2:].mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--sample-every", type=int, default=5)
    args = ap.parse_args()

    with PanelStore(read_only=True) as store:
        lb = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).date().isoformat()
        close, adj = store.bars_wide("close", start=lb), store.bars_wide("adj_close", start=lb)
        opn, high, low = store.bars_wide("open", start=lb), store.bars_wide("high", start=lb), store.bars_wide("low", start=lb)
        mask = store.membership_mask(adj.index, sorted(adj.columns))
        spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()

    adj_open = opn * (adj / close)
    rv20 = yang_zhang(opn, high, low, close, 20)
    rv60 = yang_zhang(opn, high, low, close, 60)
    rows = adj.loc[args.start:adj.index[-12]].index[::args.sample_every]
    m = mask.loc[rows]
    iv = (rv20.loc[rows] * K_IV).where(m)
    entry = adj_open.shift(-1).loc[rows].where(m)

    signals = {"resid_reversal_5": resid_reversal_5(adj, spy).loc[rows],
               "reversal_5": reversal_5(adj).loc[rows],
               "mom_12_1": mom_12_1(adj).loc[rows]}
    cheap = {"none": pd.DataFrame(True, index=rows, columns=iv.columns),
             "low_iv": iv.le(iv.median(axis=1), axis=0),
             "vol_reverting": (rv20.loc[rows] < rv60.loc[rows]),
             "low_iv+vol_reverting": iv.le(iv.median(axis=1), axis=0) & (rv20.loc[rows] < rv60.loc[rows])}

    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "end": str(adj.index[-12].date()), "k_iv": K_IV, "dte": DTE, "spread_pct": SPREAD_PCT,
              "top": args.top, "metric": "option P&L and move minus own breakeven (S2)", "horizons": {}}

    for hold, h_td in ((7, 5), (14, 10)):
        move = (adj.shift(-h_td).loc[rows] / entry - 1) * 100
        ok = iv.notna() & move.notna()
        iv_h, move_h = iv.where(ok), move.where(ok)
        be = pd.DataFrame(np.vectorize(breakeven_move)(np.nan_to_num(iv_h.to_numpy(), nan=30.0), DTE, hold, SPREAD_PCT),
                          index=rows, columns=iv.columns).where(ok)
        block = {}
        for sname, sig in signals.items():
            for gname, gate in cheap.items():
                for right in ("C", "P"):
                    pool = ok & gate.reindex_like(ok).fillna(False)
                    r = sig.where(pool).rank(axis=1, ascending=(right == "P"), method="first")
                    sel = pool & (r <= args.top)
                    res = evaluate(sel, iv_h, move_h, right, hold, be)
                    if res.get("n", 0) >= 100:
                        block[f"{sname}|{gname}|{right}"] = res
        # baseline: random pick inside each gate (same count per date)
        for gname, gate in cheap.items():
            pool = ok & gate.reindex_like(ok).fillna(False)
            rng = np.random.default_rng(7)
            noise = pd.DataFrame(rng.standard_normal(pool.shape), index=rows, columns=iv.columns)
            sel = pool & (noise.where(pool).rank(axis=1, method="first") <= args.top)
            block[f"random|{gname}|C"] = evaluate(sel, iv_h, move_h, "C", hold, be)
        report["horizons"][hold] = block

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s3_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S3 report — signals in option terms ({stamp})", "",
         f"{args.start} → {report['end']}, top-{args.top} per date, {DTE} DTE ATM, IV = {K_IV} x YZ-RV20, "
         f"spread {SPREAD_PCT}%. Score = option P&L; edge = move − that name's own breakeven.", ""]
    for hold, block in report["horizons"].items():
        L += [f"## Hold {hold} calendar days", "",
              "| signal \\| gate \\| side | n | mean P&L | win | mean edge | clears BE | NW t | yrs>0 | 1st half | 2nd half |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for key, s in sorted(block.items(), key=lambda kv: -kv[1].get("mean_pnl_pct", -99)):
            L.append(f"| {key} | {s['n']} | {s['mean_pnl_pct']:+.1f}% | {s['win_rate']:.0%} | "
                     f"{s['mean_edge_pct']:+.2f}% | {s['share_clearing_be']:.0%} | {s['t_nw']:+.2f} | "
                     f"{s['share_years_positive']:.0%} | {s['first_half']:+.1f}% | {s['second_half']:+.1f}% |")
        L.append("")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
