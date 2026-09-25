"""S42 — the option cheapness gate with realized variance from 5-minute bars. Pre-registered 2026-09-25.

S26 evaluated the gate (ATM IV in the lower half of the day's cross-section AND RV20 < RV60) on
real option prices and found nothing: straddle pass − fail +0.31% (t 1.06). Its RV was Yang-Zhang
from daily OHLC — 20 observations, a noisy ruler. Realized variance from 5-minute bars
(Andersen & Bollerslev 1998) has an order of magnitude less sampling error, so a null with the
daily ruler can be a measurement failure rather than an absent signal.

Same events, contracts, exits and horizons as S26 (S&P names, monthlies 25–50 DTE, ATM,
2024-02 → 2026-08). Only the volatility estimate changes:
  rv5_20   annualized sqrt of the mean daily realized variance over the last 20 sessions, where a
           day's RV = sum of squared 5-minute log returns (regular session) + squared overnight return
  rv5_60   the same over 60 sessions
Gates (each read on its own):
  gate_s26   S26's rule with the daily RV  (control, must reproduce S26)
  gate_5m    S26's rule with rv5_20 / rv5_60
  gate_ratio IV / rv5_20 in the lowest quartile of the day (cheapness by ratio, the form the
             literature uses: Goyal & Saretto 2009 IV−HV)
Reading (unchanged from S26): a gate is worth keeping if the straddle's pass − fail mean return
has NW t >= 2 at both horizons (5 / 10 sessions), clustered by entry date, with the sign holding
in 2024, 2025 and 2026 separately. Diagnostics: correlation of rv5_20 with the daily RV20, and
the share of days on which the two gates disagree. Holm: gates are not book variants; the two
new gates add 2 to the signal-level count noted in §9.

Usage: python -m agent.s42_rv5_gate
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import duckdb
import numpy as np
import pandas as pd

from agent.s26_option_gate import exits, implied_vol, load_candidates, pick_atm, yang_zhang
from agent.sources.alpaca_intraday import INTRADAY_DB
from agent.sources.alpaca_options import OPTIONS_DB
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2024-02-01", "2026-08-31"


def rv5_daily(names: list[str], start: str) -> pd.DataFrame:
    """days x tickers: daily realized variance from 5-minute bars (+ overnight), annualized later."""
    con = duckdb.connect(str(INTRADAY_DB), read_only=True)
    b = con.execute(f"SELECT ticker, ts, c FROM bars5 WHERE ticker IN ({','.join('?' * len(names))}) AND ts >= ? ORDER BY ticker, ts",
                    names + [start]).df()
    con.close()
    b["day"] = b["ts"].dt.normalize()
    b["r"] = np.log(b["c"]).groupby(b["ticker"]).diff()
    # the first 5-min return of a day is close(prev day) -> first bar: keep it as the overnight leg
    rv = b.groupby(["ticker", "day"])["r"].apply(lambda s: float((s.dropna() ** 2).sum()))
    return rv.unstack("ticker")


def main() -> int:
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        names = [r[0] for r in store.con.execute("SELECT DISTINCT ticker FROM membership WHERE eff_date >= '2023-11-01'").fetchall()]
        lb = "2023-10-01"
        close, opn = store.bars_wide("close", start=lb), store.bars_wide("open", start=lb)
        high, low = store.bars_wide("high", start=lb), store.bars_wide("low", start=lb)
    cols = [c for c in names if c in close.columns]
    close, opn, high, low = close[cols], opn[cols], high[cols], low[cols]
    sessions = close.index
    rv20, rv60 = yang_zhang(opn, high, low, close, 20), yang_zhang(opn, high, low, close, 60)
    rvd = rv5_daily(cols, "2023-11-01").reindex(sessions)
    rv5_20 = np.sqrt(rvd.rolling(20, min_periods=15).mean() * 252) * 100
    rv5_60 = np.sqrt(rvd.rolling(60, min_periods=45).mean() * 252) * 100
    print(f"rv5 for {rvd.shape[1]} names [{time.time() - t0:.0f}s]", flush=True)
    spot = close.stack().rename("close").reset_index()
    spot.columns = ["trade_date", "ticker", "close"]
    spot["trade_date"] = spot["trade_date"].dt.date

    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    cand = load_candidates(START, END, spot, con)
    atm = pick_atm(cand)
    T = atm["dte"].to_numpy() / 365.0
    atm["iv"] = implied_vol(atm["opt_close"].to_numpy(float), atm["spot"].to_numpy(float), atm["strike"].to_numpy(float), T, (atm["cp"] == "C").to_numpy())
    for h in (5, 10):
        atm[f"exit{h}"] = exits(atm, con, sessions, h)
    con.close()
    piv = atm.pivot_table(index=["underlying", "trade_date"], columns="cp", values=["opt_close", "iv", "exit5", "exit10"], aggfunc="first")
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    ev = piv.reset_index()
    ev["date"] = pd.to_datetime(ev["trade_date"])
    ev["iv_atm"] = ev[["iv_C", "iv_P"]].mean(axis=1) * 100

    def at(frame, col):
        return [frame.at[d, t] if (t in frame.columns and d in frame.index) else np.nan for d, t in zip(ev["date"], ev["underlying"])]
    ev["rv20"], ev["rv60"], ev["rv5_20"], ev["rv5_60"] = at(rv20, "rv20"), at(rv60, "rv60"), at(rv5_20, "rv5_20"), at(rv5_60, "rv5_60")
    ev = ev.dropna(subset=["iv_atm", "rv20", "rv60", "rv5_20", "rv5_60"])
    ev["low_iv"] = ev["iv_atm"] <= ev.groupby("date")["iv_atm"].transform("median")
    ev["gate_s26"] = ev["low_iv"] & (ev["rv20"] < ev["rv60"])
    ev["gate_5m"] = ev["low_iv"] & (ev["rv5_20"] < ev["rv5_60"])
    ev["ratio"] = ev["iv_atm"] / ev["rv5_20"]
    ev["gate_ratio"] = ev["ratio"] <= ev.groupby("date")["ratio"].transform(lambda s: s.quantile(0.25))
    for h in (5, 10):
        ev[f"ret_S_{h}"] = ((ev[f"exit{h}_C"] + ev[f"exit{h}_P"]) / (ev["opt_close_C"] + ev["opt_close_P"]) - 1) * 100
    print(f"events {len(ev)}; pass rates s26 {ev['gate_s26'].mean():.0%}, 5m {ev['gate_5m'].mean():.0%}, ratio {ev['gate_ratio'].mean():.0%} [{time.time() - t0:.0f}s]", flush=True)

    def diff(gate: str, h: int) -> dict:
        col = f"ret_S_{h}"
        x = ev.dropna(subset=[col])
        a = x[x[gate]].groupby("date")[col].mean()
        b = x[~x[gate]].groupby("date")[col].mean()
        dd = (a - b).dropna()
        yrs = dd.groupby(dd.index.year).mean()
        return {"n_dates": int(len(dd)), "mean_pass": float(a.mean()), "mean_fail": float(b.mean()), "diff": float(dd.mean()),
                "t_nw": newey_west_t(dd.to_numpy(), lag=max(h // 5, 1)), "by_year": {int(y): round(float(v), 2) for y, v in yrs.items()}}
    res = {g: {f"h{h}": diff(g, h) for h in (5, 10)} for g in ("gate_s26", "gate_5m", "gate_ratio")}
    verdict = {g: bool(all(r[f"h{h}"]["t_nw"] >= 2 for h in (5, 10)) and all(v > 0 for v in r["h5"]["by_year"].values()) and all(v > 0 for v in r["h10"]["by_year"].values()))
               for g, r in res.items()}
    diag = {"corr_rv5_20_vs_rv20": float(ev[["rv5_20", "rv20"]].corr().iloc[0, 1]), "gate_disagree_share": float((ev["gate_s26"] != ev["gate_5m"]).mean()),
            "median_iv": float(ev["iv_atm"].median()), "median_rv20_daily": float(ev["rv20"].median()), "median_rv5_20": float(ev["rv5_20"].median())}
    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s42_rv5_gate_{stamp}")
    json.dump({"start": START, "end": END, "n_events": int(len(ev)), "results": res, "verdict": verdict, "diagnostics": diag}, open(base + ".json", "w"), indent=1, default=float)
    L = [f"# S42 — option cheapness gate with 5-minute realized variance ({stamp})", "",
         f"{START} → {END}, {len(ev)} underlying-days (S&P names, ATM monthlies 25–50 DTE). Straddle buyer return, pass − fail, clustered by entry date.", "",
         f"Diagnostics: corr(rv5_20, daily RV20) = {diag['corr_rv5_20_vs_rv20']:.2f}; the two S26-style gates disagree on {diag['gate_disagree_share']:.0%} of days; "
         f"median IV {diag['median_iv']:.1f}% vs RV20 daily {diag['median_rv20_daily']:.1f}% vs rv5_20 {diag['median_rv5_20']:.1f}%.", "",
         "| gate | h | dates | pass | fail | diff | NW t | 2024 | 2025 | 2026 | verdict |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for g, r in res.items():
        for h in (5, 10):
            s = r[f"h{h}"]
            by = s["by_year"]
            L.append(f"| {g} | {h} | {s['n_dates']} | {s['mean_pass']:+.2f} | {s['mean_fail']:+.2f} | {s['diff']:+.2f} | {s['t_nw']:.2f} | "
                     f"{by.get(2024, float('nan')):+.2f} | {by.get(2025, float('nan')):+.2f} | {by.get(2026, float('nan')):+.2f} | {'**KEEP**' if verdict[g] else 'no'} |")
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
