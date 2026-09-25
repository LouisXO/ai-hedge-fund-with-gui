"""S42b — is the IV / rv5 ratio gate (S42, KEEP) real or a stale-print artifact? Written 2026-09-25 after S42.

Worry: the option "price" is the day's last trade. An illiquid contract whose last print is stale
and low shows a low IV (so it passes the ratio gate) and then "returns" to fair value at the next
print — a spurious buyer gain. Checks, all on S42's gate_ratio (IV / rv5_20 in the day's lowest
quartile), straddle return pass − fail at 5 and 10 sessions:

  F0  as S42 (volume > 0 on entry and exit days)
  F1  entry: both legs volume >= 50 contracts and n_trades >= 10
  F2  F1 + exit legs volume >= 50
  F3  valued at the day's VWAP (entry and exit) instead of the last trade
  F4  F1 + exclude entries where the straddle's close fell more than 30% from the prior session
      (the signature of a stale or off-market print)
  by underlying dollar-volume tercile (liquid names should show it too if it is real)
Also the pass side's own mean return (what a buyer would earn, gross of spread).
Reading: the gate survives if F2 and F3 keep NW t >= 2 at both horizons and the effect is present
in the top liquidity tercile. Diagnostic, not a new Holm entry.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent.s26_option_gate import implied_vol, load_candidates, pick_atm
from agent.s42_rv5_gate import rv5_daily
from agent.sources.alpaca_options import OPTIONS_DB
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2024-02-01", "2026-08-31"


def exits_full(atm, con, sessions, h):
    pos = sessions.searchsorted(pd.to_datetime(atm["trade_date"]).to_numpy())
    target = sessions[np.minimum(pos + h, len(sessions) - 1)]
    key = pd.DataFrame({"symbol": atm["symbol"].to_numpy(), "t0": pd.DatetimeIndex(target).tz_localize(None),
                        "t1": (pd.DatetimeIndex(target) + pd.Timedelta(days=2)).tz_localize(None)})
    con.register("k", key)
    got = con.execute("""SELECT k.symbol, k.t0, first(b.close ORDER BY b.trade_date) AS px, first(b.vwap ORDER BY b.trade_date) AS vwap,
                                first(b.volume ORDER BY b.trade_date) AS vol
                         FROM k JOIN opt_bars b ON b.symbol = k.symbol AND b.trade_date BETWEEN CAST(k.t0 AS DATE) AND CAST(k.t1 AS DATE) AND b.volume > 0
                         GROUP BY 1, 2""").df()
    con.unregister("k")
    got["t0"] = pd.to_datetime(got["t0"])
    m = key.merge(got, on=["symbol", "t0"], how="left")
    return m["px"].to_numpy(), m["vwap"].to_numpy(), m["vol"].to_numpy()


def main() -> int:
    with PanelStore(read_only=True) as store:
        names = [r[0] for r in store.con.execute("SELECT DISTINCT ticker FROM membership WHERE eff_date >= '2023-11-01'").fetchall()]
        close = store.bars_wide("close", start="2023-10-01")
        vol = store.bars_wide("volume", start="2023-10-01")
    cols = [c for c in names if c in close.columns]
    close, vol = close[cols], vol[cols]
    sessions = close.index
    adv = (close * vol).rolling(20).mean()
    rvd = rv5_daily(cols, "2023-11-01").reindex(sessions)
    rv5_20 = np.sqrt(rvd.rolling(20, min_periods=15).mean() * 252) * 100
    spot = close.stack().rename("close").reset_index()
    spot.columns = ["trade_date", "ticker", "close"]
    spot["trade_date"] = spot["trade_date"].dt.date

    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    cand = load_candidates(START, END, spot, con)
    # extra columns for the checks
    con.register("c0", cand[["symbol", "trade_date"]])
    extra = con.execute("""SELECT c0.symbol, c0.trade_date, b.vwap, b.n_trades,
                                  (SELECT p.close FROM opt_bars p WHERE p.symbol = c0.symbol AND p.trade_date < c0.trade_date AND p.volume > 0
                                   ORDER BY p.trade_date DESC LIMIT 1) AS prev_close
                           FROM c0 JOIN opt_bars b ON b.symbol = c0.symbol AND b.trade_date = c0.trade_date""").df()
    con.unregister("c0")
    cand = cand.merge(extra, on=["symbol", "trade_date"], how="left")
    atm = pick_atm(cand)
    T = atm["dte"].to_numpy() / 365.0
    atm["iv"] = implied_vol(atm["opt_close"].to_numpy(float), atm["spot"].to_numpy(float), atm["strike"].to_numpy(float), T, (atm["cp"] == "C").to_numpy())
    for h in (5, 10):
        px, vw, v = exits_full(atm, con, sessions, h)
        atm[f"exit{h}"], atm[f"exitvw{h}"], atm[f"exitvol{h}"] = px, vw, v
    con.close()
    piv = atm.pivot_table(index=["underlying", "trade_date"], columns="cp",
                          values=["opt_close", "vwap", "volume", "n_trades", "prev_close", "iv", "exit5", "exit10", "exitvw5", "exitvw10", "exitvol5", "exitvol10"], aggfunc="first")
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    ev = piv.reset_index()
    ev["date"] = pd.to_datetime(ev["trade_date"])
    ev["iv_atm"] = ev[["iv_C", "iv_P"]].mean(axis=1) * 100
    ev["rv5_20"] = [rv5_20.at[d, t] if (t in rv5_20.columns and d in rv5_20.index) else np.nan for d, t in zip(ev["date"], ev["underlying"])]
    ev["adv"] = [adv.at[d, t] if (t in adv.columns and d in adv.index) else np.nan for d, t in zip(ev["date"], ev["underlying"])]
    ev = ev.dropna(subset=["iv_atm", "rv5_20"])
    ev["ratio"] = ev["iv_atm"] / ev["rv5_20"]
    ev["gate"] = ev["ratio"] <= ev.groupby("date")["ratio"].transform(lambda s: s.quantile(0.25))
    s_entry = ev["opt_close_C"] + ev["opt_close_P"]
    s_prev = ev["prev_close_C"] + ev["prev_close_P"]
    for h in (5, 10):
        ev[f"ret{h}"] = ((ev[f"exit{h}_C"] + ev[f"exit{h}_P"]) / s_entry - 1) * 100
        ev[f"retvw{h}"] = ((ev[f"exitvw{h}_C"] + ev[f"exitvw{h}_P"]) / (ev["vwap_C"] + ev["vwap_P"]) - 1) * 100
    filt = {"F0": pd.Series(True, index=ev.index),
            "F1": (ev["volume_C"] >= 50) & (ev["volume_P"] >= 50) & (ev["n_trades_C"] >= 10) & (ev["n_trades_P"] >= 10)}
    filt["F2_5"] = filt["F1"] & (ev["exitvol5_C"] >= 50) & (ev["exitvol5_P"] >= 50)
    filt["F2_10"] = filt["F1"] & (ev["exitvol10_C"] >= 50) & (ev["exitvol10_P"] >= 50)
    filt["F4"] = filt["F1"] & ~(s_entry < 0.7 * s_prev)
    ev["adv_ter"] = ev.groupby("date")["adv"].transform(lambda s: pd.qcut(s.rank(method="first"), 3, labels=["low", "mid", "high"]) if s.notna().sum() >= 30 else None)

    def diff(mask, col):
        x = ev[mask].dropna(subset=[col])
        a = x[x["gate"]].groupby("date")[col].mean()
        b = x[~x["gate"]].groupby("date")[col].mean()
        dd = (a - b).dropna()
        return {"n_dates": int(len(dd)), "n_pass": int(x["gate"].sum()), "pass": float(a.mean()), "fail": float(b.mean()),
                "diff": float(dd.mean()), "t_nw": newey_west_t(dd.to_numpy(), lag=2) if len(dd) > 10 else float("nan")}
    res = {}
    for name in ("F0", "F1", "F4"):
        for h in (5, 10):
            res[f"{name}_h{h}"] = diff(filt[name], f"ret{h}")
    for h in (5, 10):
        res[f"F2_h{h}"] = diff(filt[f"F2_{h}"], f"ret{h}")
        res[f"F3_vwap_h{h}"] = diff(filt["F1"], f"retvw{h}")
        for ter in ("low", "mid", "high"):
            res[f"F1_{ter}_h{h}"] = diff(filt["F1"] & (ev["adv_ter"] == ter), f"ret{h}")
    ok = all(res[f"F2_h{h}"]["t_nw"] >= 2 and res[f"F3_vwap_h{h}"]["t_nw"] >= 2 and res[f"F1_high_h{h}"]["t_nw"] >= 2 for h in (5, 10))
    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s42b_rv5_gate_robustness_{stamp}")
    json.dump({"results": res, "survives": ok}, open(base + ".json", "w"), indent=1, default=float)
    L = [f"# S42b — robustness of the IV / rv5 ratio gate ({stamp})", "", "Straddle buyer return %, gate pass − fail, clustered by entry date.", "",
         "| filter | h | dates | n pass | pass | fail | diff | NW t |", "|---|---|---|---|---|---|---|---|"]
    for k, s in res.items():
        L.append(f"| {k.rsplit('_h', 1)[0]} | {k.rsplit('_h', 1)[1]} | {s['n_dates']} | {s['n_pass']} | {s['pass']:+.2f} | {s['fail']:+.2f} | {s['diff']:+.2f} | {s['t_nw']:.2f} |")
    L += ["", f"Survives (F2, VWAP and top-liquidity tercile all t >= 2 at both horizons): **{'yes' if ok else 'no'}**"]
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
