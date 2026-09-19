"""S2: how accurate must a direction call be to pay for a long ATM option?

The agent's output is a bought call/put held 7-14 days, so a stock-return
IC is not the pass mark — the option has to clear premium decay, the
volatility risk premium and the spread. There is no free history of real
option prices (DoltHub's SQL API is gone, Alpha Vantage HISTORICAL_OPTIONS
is premium), so the hurdle is measured with a model:

  IV = k x Yang-Zhang RV20,  ATM strike at the entry price, 37 DTE,
  repriced with optradar's own radar.horizon.bs after h calendar days at
  unchanged IV, minus one round-trip spread.

k and the spread come from optradar's real picks (out/*.json), so the
model is anchored on quotes the pipeline actually received. What varies
across the panel is the realized move, which is real.

Reported per horizon: the breakeven move, the share of names that clear
it, the mean/median option P&L of a random pick, the same for a signal's
top-5, and the drift a signal must add for the book to break even.

Usage: python -m agent.s2_option_hurdle [--start 2015-01-01] [--dte 37]
Writes site-data/validation/s2_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

from hedge_fund.features.factors import mom_12_1, reversal_5
from hedge_fund.features.panel import PanelStore
from hedge_fund.features.rv import yang_zhang

sys.path.insert(0, "/Users/louis/optradar")
from radar.horizon import bs  # noqa: E402  (optradar's pricer, same conventions as the live brief)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")
OPTRADAR_OUT = "/Users/louis/optradar/out"


def calibrate(rv_panel: pd.DataFrame) -> dict:
    """IV / YangZhang-RV20 and spreads from the radar's own picks."""
    ks, spreads, be7, sig7 = [], [], [], []
    for f in sorted(glob.glob(os.path.join(OPTRADAR_OUT, "*.json"))):
        d = json.load(open(f))
        date = pd.Timestamp(d["date"])
        for i in d.get("ideas", []):
            iv, t = i.get("atm_iv"), i["ticker"].split(".")[-1]
            bp = i.get("budget_pick") or {}
            if bp.get("spread_pct") is not None:
                spreads.append(bp["spread_pct"])
            hz = (bp.get("horizon") or [{}])[0]
            if hz.get("breakeven") is not None:
                be7.append(hz["breakeven"])
                sig7.append(hz["sigma"])
            if iv and t in rv_panel.columns:
                prior = rv_panel[t].loc[:date].dropna()
                if len(prior):
                    ks.append(iv / prior.iloc[-1])
    return {"n_picks": len(ks), "k_iv_over_yzrv": float(np.median(ks)) if ks else float("nan"),
            "k_iqr": [float(np.quantile(ks, 0.25)), float(np.quantile(ks, 0.75))] if ks else None,
            "spread_pct_median": float(np.median(spreads)) if spreads else float("nan"),
            "live_be7_median": float(np.median(be7)) if be7 else None,
            "live_sigma7_median": float(np.median(sig7)) if sig7 else None}


def breakeven_move(iv: float, dte: int, hold: int, spread: float, right: str = "C") -> float:
    """Favourable move (%) needed to get the premium back after `hold` days."""
    v, T0, T1 = iv / 100, dte / 365, (dte - hold) / 365
    mid = bs(1.0, 1.0, T0, v, right)
    sgn = 1 if right == "C" else -1

    def ret(m):
        return (bs(1.0 + sgn * m / 100, 1.0, T1, v, right) / mid - 1) * 100 - spread

    if ret(0) >= 0:
        return 0.0
    lo, hi = 0.0, 100.0
    for _ in range(40):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if ret(m) < 0 else (lo, m)
    return hi


def option_pnl(iv: pd.DataFrame, move_pct: pd.DataFrame, dte: int, hold: int, spread: float,
               right: str = "C") -> pd.DataFrame:
    """% P&L of an ATM option entered at the money and held `hold` calendar days."""
    v, T0, T1 = iv.to_numpy() / 100, dte / 365, (dte - hold) / 365
    sgn = 1 if right == "C" else -1
    s_exit = 1 + sgn * move_pct.to_numpy() / 100
    mid = np.vectorize(bs)(1.0, 1.0, T0, v, right)
    out = np.vectorize(bs)(s_exit, 1.0, T1, v, right)
    with np.errstate(invalid="ignore", divide="ignore"):
        pnl = (out / mid - 1) * 100 - spread
    return pd.DataFrame(pnl, index=iv.index, columns=iv.columns)


def required_drift(iv, move, dte, hold, spread, right="C", lo=-1.0, hi=10.0) -> float:
    """Extra favourable drift (% of spot) that makes the mean option P&L zero."""
    def mean_pnl(mu):
        return np.nanmean(option_pnl(iv, move + mu, dte, hold, spread, right).to_numpy())
    if mean_pnl(lo) > 0:
        return lo
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if mean_pnl(mid) < 0 else (lo, mid)
    return hi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--dte", type=int, default=37)
    ap.add_argument("--sample-every", type=int, default=5, help="trading days between simulated entries")
    ap.add_argument("--k", type=float, default=None, help="override IV / YZ-RV20 multiple")
    ap.add_argument("--k-grid", default="1.0,1.1,1.2,1.3",
                    help="IV/RV multiples to report the hurdle at (the volatility risk premium a buyer pays)")
    args = ap.parse_args()

    with PanelStore(read_only=True) as store:
        start_lb = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).date().isoformat()
        close = store.bars_wide("close", start=start_lb)
        adj = store.bars_wide("adj_close", start=start_lb)
        opn = store.bars_wide("open", start=start_lb)
        high = store.bars_wide("high", start=start_lb)
        low = store.bars_wide("low", start=start_lb)
        cols = sorted(set(adj.columns))
        mask = store.membership_mask(adj.index, cols)

    adj_open = opn * (adj / close)
    rv = yang_zhang(opn, high, low, close, 20)
    cal = calibrate(rv) if args.k is None else {"k_iv_over_yzrv": args.k, "n_picks": 0}
    k = args.k or cal["k_iv_over_yzrv"]
    spread = cal.get("spread_pct_median") or 2.0

    sl = slice(args.start, adj.index[-12])
    rows = adj.loc[sl].index[::args.sample_every]
    iv = (rv.loc[rows] * k).where(mask.loc[rows])
    entry = adj_open.shift(-1).loc[rows].where(mask.loc[rows])

    signals = {"random": None, "reversal_5": reversal_5(adj).loc[rows], "mom_12_1": mom_12_1(adj).loc[rows]}
    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "end": str(adj.index[-12].date()), "dte": args.dte, "spread_pct": spread,
              "calibration": cal, "k_used": k, "n_entry_dates": int(len(rows)),
              "model": "IV = k x YangZhang RV20, ATM, BS reprice at unchanged IV, one round-trip spread",
              "horizons": {}}

    for hold, h_td in ((7, 5), (14, 10)):
        exit_px = adj.shift(-(1 + h_td - 1)).loc[rows]          # close of the h-th trading day after entry
        move = (exit_px / entry - 1) * 100
        both = iv.notna() & move.notna()
        iv_h, move_h = iv.where(both), move.where(both)
        be = breakeven_move(float(np.nanmedian(iv_h.to_numpy())), args.dte, hold, spread)
        block = {"breakeven_move_pct_at_median_iv": be,
                 "median_iv": float(np.nanmedian(iv_h.to_numpy())),
                 "median_abs_move_pct": float(np.nanmedian(np.abs(move_h.to_numpy()))),
                 "share_calls_clearing_be": float(np.nanmean((move_h.to_numpy() >= be))),
                 "n_obs": int(both.to_numpy().sum()), "by_signal": {}}
        for name, sig in signals.items():
            for right in ("C", "P"):
                if name == "random":
                    sel = both
                else:
                    r = sig.where(both).rank(axis=1, ascending=(right == "P"), method="first")
                    sel = both & (r <= 5)
                pnl = option_pnl(iv_h.where(sel), move_h.where(sel), args.dte, hold, spread, right)
                arr = pnl.to_numpy()[np.isfinite(pnl.to_numpy())]
                mv = move_h.where(sel).to_numpy()
                ivs = iv_h.where(sel).to_numpy()
                ok = np.isfinite(mv) & np.isfinite(ivs)
                mv, ivs = mv[ok], ivs[ok]
                # each name pays its OWN hurdle: a high-vol pick needs a bigger move
                be_i = np.vectorize(breakeven_move)(ivs, args.dte, hold, spread, right)
                sgn = 1 if right == "C" else -1
                block["by_signal"][f"{name}_{right}"] = {
                    "n": int(arr.size), "mean_pnl_pct": float(arr.mean()), "median_pnl_pct": float(np.median(arr)),
                    "win_rate": float((arr > 0).mean()), "mean_move_pct": float(mv.mean()),
                    "mean_breakeven_pct": float(be_i.mean()),
                    "share_clearing_own_be": float((sgn * mv >= be_i).mean()),
                    "mean_move_minus_be": float((sgn * mv - be_i).mean()),
                    "p90_pnl_pct": float(np.quantile(arr, 0.9)), "p10_pnl_pct": float(np.quantile(arr, 0.1))}
        block["required_extra_drift_pct_call"] = required_drift(iv_h, move_h, args.dte, hold, spread, "C")
        # The buyer's real cost is the volatility risk premium: IV sits above the
        # vol that is later realized. k is not knowable from free history, so the
        # hurdle is reported across a grid instead of at one calibrated point.
        block["k_grid"] = {}
        for kk in [float(x) for x in args.k_grid.split(",")]:
            iv_k = iv_h * (kk / k)
            pnl = option_pnl(iv_k, move_h, args.dte, hold, spread, "C").to_numpy()
            pnl = pnl[np.isfinite(pnl)]
            block["k_grid"][kk] = {
                "breakeven_move_pct": breakeven_move(float(np.nanmedian(iv_k.to_numpy())), args.dte, hold, spread),
                "mean_pnl_random_call": float(pnl.mean()), "win_rate": float((pnl > 0).mean()),
                "required_extra_drift_pct": required_drift(iv_k, move_h, args.dte, hold, spread, "C")}
        report["horizons"][hold] = block

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s2_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S2 report — option hurdle ({stamp})", "",
         f"Window {args.start} → {report['end']}, entries every {args.sample_every} trading days, "
         f"{args.dte} DTE ATM, spread {spread:.1f}%, IV = {k:.2f} x Yang-Zhang RV20 "
         f"(calibrated on {cal.get('n_picks', 0)} live radar picks).", ""]
    if cal.get("live_be7_median"):
        L += [f"Live radar anchor: breakeven 7d {cal['live_be7_median']:.2f}%, 1σ {cal['live_sigma7_median']:.2f}%.", ""]
    for hold, b in report["horizons"].items():
        L += [f"## Hold {hold} calendar days", "",
              f"- median IV {b['median_iv']:.1f}%, breakeven move **{b['breakeven_move_pct_at_median_iv']:.2f}%**, "
              f"median |move| {b['median_abs_move_pct']:.2f}%, share clearing breakeven {b['share_calls_clearing_be']:.1%}",
              f"- extra drift needed for a call book to break even: **{b['required_extra_drift_pct_call']:+.2f}%** of spot",
              ""]
        L += ["| IV / realized vol | breakeven move | mean P&L of a random call | win rate | drift needed |",
              "|---|---|---|---|---|"]
        for kk, g in b["k_grid"].items():
            L.append(f"| {float(kk):.2f} | {g['breakeven_move_pct']:.2f}% | {g['mean_pnl_random_call']:+.1f}% | "
                     f"{g['win_rate']:.1%} | {g['required_extra_drift_pct']:+.2f}% |")
        L += ["", "| pick | n | mean P&L | median P&L | win rate | mean move | own breakeven | clears own BE | move − BE |",
              "|---|---|---|---|---|---|---|---|---|"]
        for nm, s in b["by_signal"].items():
            L.append(f"| {nm} | {s['n']} | {s['mean_pnl_pct']:+.1f}% | {s['median_pnl_pct']:+.1f}% | "
                     f"{s['win_rate']:.1%} | {s['mean_move_pct']:+.2f}% | {s['mean_breakeven_pct']:.2f}% | "
                     f"{s['share_clearing_own_be']:.1%} | {s['mean_move_minus_be']:+.2f}% |")
        L.append("")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
