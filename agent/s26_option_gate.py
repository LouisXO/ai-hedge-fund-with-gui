"""S26 — the option cheapness gate on REAL option prices (Alpaca, 2024-01 → 2026-08).

What the shadow record has been doing since S3 (agent/daily.py): each day,
among S&P names, "cheap" = implied vol in the lower half of the
cross-section AND RV20 < RV60; the book buys ATM calls/puts with 25–50 DTE
and holds 7/14 days. The IV in that gate was a PROXY (K x RV20). Here the
same rule is evaluated with the contract's own price:

  contract   the monthly with the smallest DTE >= 25 (<= 50), strike nearest spot
  price      the contract's daily close (last trade), volume > 0 that day
  entry      day D close;  exit  the same contract's close 5 / 10 sessions later
  IV         Black-Scholes implied from the close, spot, DTE, r = 4.5%; ATM IV = mean(call, put)
  gate       IV <= that day's cross-sectional median  AND  RV20 < RV60   (S3's rule, unchanged)
  outcome    buyer return % of the call, the put and the straddle, gross of spread

Pre-registered before running (2026-09-22): the gate is worth keeping if
the straddle's mean return is higher when the gate passes than when it
fails, clustered by entry date, with a Newey-West t >= 2 on the difference
at both horizons, and the sign holds in 2024, 2025 and 2026 separately. A
decile table of IV/RV20 is reported as a diagnostic, not a test.

Usage: python -m agent.s26_option_gate [--start 2024-02-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os

import duckdb
import numpy as np
import pandas as pd

from agent.s8_insider import event_stats
from agent.sources.alpaca_options import OPTIONS_DB
from hedge_fund.features.panel import PanelStore
from hedge_fund.features.rv import yang_zhang
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
R = 0.045
DTE_LO, DTE_HI = 25, 50
MONEYNESS = 0.03


def _ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def bs_price(S, K, T, sigma, is_call):
    sigma = np.maximum(sigma, 1e-6)
    d1 = (np.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    call = S * _ncdf(d1) - K * np.exp(-R * T) * _ncdf(d2)
    put = K * np.exp(-R * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return np.where(is_call, call, put)


def implied_vol(price, S, K, T, is_call, iters: int = 40) -> np.ndarray:
    lo, hi = np.full_like(price, 0.01), np.full_like(price, 5.0)
    for _ in range(iters):
        mid = (lo + hi) / 2
        p = bs_price(S, K, T, mid, is_call)
        hi = np.where(p > price, mid, hi)
        lo = np.where(p > price, lo, mid)
    iv = (lo + hi) / 2
    intrinsic = np.where(is_call, np.maximum(S - K, 0), np.maximum(K - S, 0))
    return np.where(price <= intrinsic + 1e-6, np.nan, iv)


def load_candidates(start: str, end: str, spot: pd.DataFrame, con) -> pd.DataFrame:
    """bars x contracts x spot, near ATM and 25–50 DTE, for every (underlying, day)."""
    con.register("spot", spot)
    q = f"""
        SELECT b.symbol, c.underlying, b.trade_date, c.expiry, c.cp, c.strike, b.close AS opt_close, b.volume, s.close AS spot,
               datediff('day', b.trade_date, c.expiry) AS dte
        FROM opt_bars b JOIN opt_contracts c USING (symbol)
        JOIN spot s ON s.ticker = c.underlying AND s.trade_date = b.trade_date
        WHERE b.trade_date BETWEEN '{start}' AND '{end}' AND b.volume > 0 AND b.close > 0
          AND datediff('day', b.trade_date, c.expiry) BETWEEN {DTE_LO} AND {DTE_HI}
          AND abs(c.strike / s.close - 1) <= {MONEYNESS}"""
    df = con.execute(q).df()
    con.unregister("spot")
    return df


def pick_atm(df: pd.DataFrame) -> pd.DataFrame:
    """Per (underlying, day): the nearest expiry, then the strike nearest spot, needing both call and put."""
    df = df.copy()
    df["k_dist"] = (df["strike"] / df["spot"] - 1).abs()
    df = df.sort_values(["underlying", "trade_date", "dte", "k_dist"])
    first_exp = df.groupby(["underlying", "trade_date"])["expiry"].transform("first")
    df = df[df["expiry"] == first_exp]
    first_k = df.groupby(["underlying", "trade_date"])["strike"].transform("first")
    df = df[df["strike"] == first_k]
    both = df.groupby(["underlying", "trade_date"])["cp"].transform("nunique") == 2
    return df[both].drop_duplicates(["underlying", "trade_date", "cp"])


def exits(atm: pd.DataFrame, con, sessions: pd.DatetimeIndex, h: int) -> pd.Series:
    """The same contract's close h sessions after entry (or the next available bar within 2 days)."""
    pos = sessions.searchsorted(pd.to_datetime(atm["trade_date"]).to_numpy())
    target = sessions[np.minimum(pos + h, len(sessions) - 1)]
    key = pd.DataFrame({"symbol": atm["symbol"].to_numpy(), "t0": pd.DatetimeIndex(target).tz_localize(None),
                        "t1": (pd.DatetimeIndex(target) + pd.Timedelta(days=2)).tz_localize(None)})
    con.register("k", key)
    got = con.execute("""SELECT k.symbol, k.t0, first(b.close ORDER BY b.trade_date) AS px
                         FROM k JOIN opt_bars b ON b.symbol = k.symbol
                              AND b.trade_date BETWEEN CAST(k.t0 AS DATE) AND CAST(k.t1 AS DATE) AND b.volume > 0
                         GROUP BY 1, 2""").df()
    con.unregister("k")
    got["t0"] = pd.to_datetime(got["t0"])
    m = key.merge(got, on=["symbol", "t0"], how="left")
    return pd.Series(m["px"].to_numpy(), index=atm.index)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-02-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        names = [r[0] for r in store.con.execute("SELECT DISTINCT ticker FROM membership WHERE eff_date >= '2023-11-01'").fetchall()]
        lb = "2023-10-01"
        close, opn = store.bars_wide("close", start=lb), store.bars_wide("open", start=lb)
        high, low = store.bars_wide("high", start=lb), store.bars_wide("low", start=lb)
    cols = [c for c in names if c in close.columns]
    close, opn, high, low = close[cols], opn[cols], high[cols], low[cols]
    sessions = close.index
    rv20, rv60 = yang_zhang(opn, high, low, close, 20), yang_zhang(opn, high, low, close, 60)
    spot = close.stack().rename("close").reset_index()
    spot.columns = ["trade_date", "ticker", "close"]
    spot["trade_date"] = spot["trade_date"].dt.date

    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    cand = load_candidates(args.start, args.end, spot, con)
    print(f"candidates {len(cand)} rows", flush=True)
    atm = pick_atm(cand)
    print(f"ATM pairs: {len(atm) // 2} (underlying-days)", flush=True)
    T = atm["dte"].to_numpy() / 365.0
    atm["iv"] = implied_vol(atm["opt_close"].to_numpy(float), atm["spot"].to_numpy(float), atm["strike"].to_numpy(float), T,
                            (atm["cp"] == "C").to_numpy())
    for h in (5, 10):
        atm[f"exit{h}"] = exits(atm, con, sessions, h)
    con.close()

    # one row per (underlying, day): call, put, straddle
    piv = atm.pivot_table(index=["underlying", "trade_date"], columns="cp",
                          values=["opt_close", "iv", "exit5", "exit10"], aggfunc="first")
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    ev = piv.reset_index()
    ev["date"] = pd.to_datetime(ev["trade_date"])
    ev["iv_atm"] = ev[["iv_C", "iv_P"]].mean(axis=1) * 100
    d = ev["date"].to_numpy()
    ev["rv20"] = [rv20.at[dd, t] if t in rv20.columns else np.nan for dd, t in zip(ev["date"], ev["underlying"])]
    ev["rv60"] = [rv60.at[dd, t] if t in rv60.columns else np.nan for dd, t in zip(ev["date"], ev["underlying"])]
    ev = ev.dropna(subset=["iv_atm", "rv20", "rv60"])
    ev["iv_med"] = ev.groupby("date")["iv_atm"].transform("median")
    ev["low_iv"] = ev["iv_atm"] <= ev["iv_med"]
    ev["reverting"] = ev["rv20"] < ev["rv60"]
    ev["gate"] = ev["low_iv"] & ev["reverting"]
    ev["iv_rv"] = ev["iv_atm"] / ev["rv20"]
    for h in (5, 10):
        for side in ("C", "P"):
            ev[f"ret_{side}_{h}"] = (ev[f"exit{h}_{side}"] / ev[f"opt_close_{side}"] - 1) * 100
        ev[f"ret_S_{h}"] = ((ev[f"exit{h}_C"] + ev[f"exit{h}_P"]) / (ev["opt_close_C"] + ev["opt_close_P"]) - 1) * 100
    print(f"events with exits: {ev['ret_S_5'].notna().sum()} (h5), {ev['ret_S_10'].notna().sum()} (h10); gate pass rate {ev['gate'].mean():.0%}", flush=True)

    def daily_diff(col: str) -> dict:
        g = ev.dropna(subset=[col]).groupby("date")
        a = g.apply(lambda x: x.loc[x["gate"], col].mean() if x["gate"].any() else np.nan)
        b = g.apply(lambda x: x.loc[~x["gate"], col].mean() if (~x["gate"]).any() else np.nan)
        dd = (a - b).dropna()
        yrs = dd.groupby(dd.index.year).mean()
        return {"n_dates": int(len(dd)), "mean_pass": float(a.mean()), "mean_fail": float(b.mean()),
                "diff": float(dd.mean()), "t_nw": newey_west_t(dd.to_numpy(), lag=5),
                "by_year": {int(y): round(float(v), 2) for y, v in yrs.items()}, "hit_pass": float((a > 0).mean())}

    stats = {f"{inst}_{h}": daily_diff(f"ret_{inst}_{h}") for inst in ("S", "C", "P") for h in (5, 10)}
    ev["decile"] = ev.groupby("date")["iv_rv"].transform(lambda s: pd.qcut(s.rank(method="first"), 10, labels=False) + 1)
    dec = ev.groupby("decile")[["ret_S_5", "ret_S_10", "ret_C_10", "ret_P_10"]].mean().round(2)
    yearly = ev.groupby(ev["date"].dt.year)[["ret_S_5", "ret_S_10"]].mean().round(2)

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"s26_option_gate_{stamp}.json"), "w") as f:
        json.dump({"n_events": int(len(ev)), "gate_pass_rate": float(ev["gate"].mean()), "stats": stats,
                   "deciles": dec.to_dict(), "yearly": yearly.to_dict(),
                   "iv_median_pct": float(ev["iv_atm"].median()), "rv20_median_pct": float(ev["rv20"].median())}, f, indent=1, default=float)
    L = [f"# S26 — option cheapness gate on real prices ({stamp})", "",
         f"{args.start} → {args.end}, {len(ev)} underlying-days with an ATM call+put pair (25–50 DTE, monthly, traded that day), "
         f"S&P names. Entry at the contract's close, exit at its close 5/10 sessions later, gross of spread. "
         f"Median ATM IV {ev['iv_atm'].median():.1f}% vs median RV20 {ev['rv20'].median():.1f}%. Gate passes {ev['gate'].mean():.0%} of days.", "",
         "## Gate pass − fail, mean buyer return %, clustered by entry date", "",
         "| instrument | h | dates | pass | fail | diff | NW t | hit(pass>0) | 2024 | 2025 | 2026 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        inst, h = k.split("_")
        by = s["by_year"]
        L.append(f"| {'straddle' if inst == 'S' else 'call' if inst == 'C' else 'put'} | {h} | {s['n_dates']} | {s['mean_pass']:+.2f} | {s['mean_fail']:+.2f} | "
                 f"{s['diff']:+.2f} | {s['t_nw']:.2f} | {s['hit_pass']:.0%} | {by.get(2024, float('nan')):+.2f} | {by.get(2025, float('nan')):+.2f} | {by.get(2026, float('nan')):+.2f} |")
    L += ["", "## IV / RV20 deciles (1 = cheapest), mean buyer return % — diagnostic", "", dec.to_string(), "",
          "## By year, all days", "", yearly.to_string()]
    text = "\n".join(L) + "\n"
    with open(os.path.join(OUT_DIR, f"s26_option_gate_{stamp}.md"), "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
