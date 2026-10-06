"""S50 — a mechanical replica of Balder's monthly picks. Pre-registered 2026-10-06 before any run.

Descriptive only: not a candidate rule, never a paper signal (Balder's content is never a signal, AGENT.md).
The user asked why Balder picked RKLB and TSM for September and AVGO and ASTS for October. On the pick
dates (2026-08-31, 2026-10-01) the four shared: a beta to QQQ in the top decile of liquid names (1.57-2.53),
a 52-week high in May-June 2026 and a drawdown from it of 13-57%, RSI(14) 35-48, and QQQ up 4-5% over the
month and above its 50-day average. This tests whether that setup has a mechanical edge, or whether his hits
are what such names do in a rising market.

Universe   each month end 2017-01 → 2026-08: 20-day dollar volume >= $100M and close >= $5 (point in time;
           no share counts, so ADRs such as TSM are in).
Features   beta: 252-day daily log returns on QQQ's (>= 200 days); drawdown: adjusted close over its 252-day
           high; RSI(14) on adjusted closes (Wilder).
Baskets    (equal weight, entry at the month-end close as his own reports measure, held 21 sessions)
  HB       the top decile of beta in the universe that day
  FHB      HB with a drawdown between -10% and -60% and RSI(14) between 30 and 50 ("fallen high beta")
  FHB_UP   FHB only in months when QQQ is above its 50-day average and up over the last 21 sessions;
           otherwise the month holds QQQ
  ALL      the whole universe
Per name   21-session return; excess over QQQ for the same window; beta-adjusted excess (return - beta x QQQ);
           touched +6% within the 21 sessions (daily high, adjusted: his first targets were +5% to +7%);
           touched +15% (his final targets +16% to +26%).
Readings (fixed now)
  1  FHB_UP's monthly beta-adjusted excess with mean > 0 and Newey-West t >= 2 (lag 1) = the setup has a
     mechanical edge; otherwise his hits are explained by beta, a rising market and volatility.
  2  if FHB's +6% touch rate is within 5 percentage points of HB's, hitting the first target comes from
     volatility, not from the pullback selection.
  3  FHB's excess in QQQ-up months vs QQQ-down months is reported (regime dependence), not judged.
Multiple testing: a descriptive replica of a third party, not one of our candidates (+0); anything that
looks useful needs its own pre-registration and data after 2026-10.

Usage: python -m agent.s50_balder_replica [--start 2017-01-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
ADV_MIN, PX_MIN = 100e6, 5.0
H = 21
TOUCH = (0.06, 0.15)


def rsi(adj: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = adj.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def month_ends(index: pd.DatetimeIndex, start: str, end: str) -> list[pd.Timestamp]:
    d = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    return list(pd.Series(d, index=d).groupby([d.year, d.month]).last())


def load(start: str):
    lb = (pd.Timestamp(start) - pd.Timedelta(days=420)).date().isoformat()
    with PanelStore(read_only=True) as store:
        close, adj = store.bars_wide("close", start=lb), store.bars_wide("adj_close", start=lb)
        high, vol = store.bars_wide("high", start=lb), store.bars_wide("volume", start=lb)
        q = store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'QQQ' AND trade_date >= ?", [lb]).df()
    q["trade_date"] = pd.to_datetime(q["trade_date"])
    qqq = q.set_index("trade_date")["adj_close"].sort_index().reindex(adj.index).ffill()
    adj_high = high * (adj / close)
    adv = (close * vol).rolling(20).mean()
    return close, adj, adj_high, adv, qqq


def features(day, adj, close, adv, qqq, rs) -> pd.DataFrame:
    i = adj.index.get_loc(day)
    ok = (adv.iloc[i] >= ADV_MIN) & (close.iloc[i] >= PX_MIN)
    names = ok[ok].index
    a = adj.iloc[max(0, i - 252):i + 1][names]
    lr = np.log(a).diff().iloc[1:]
    qr = np.log(qqq.iloc[max(0, i - 252):i + 1]).diff().iloc[1:]
    n_ok = lr.notna().sum()
    cov = lr.sub(lr.mean()).mul(qr - qr.mean(), axis=0).sum() / (n_ok - 1)
    beta = (cov / qr.var()).where(n_ok >= 200)
    dd = a.iloc[-1] / a.max() - 1
    return pd.DataFrame({"beta": beta, "dd": dd, "rsi": rs.iloc[i].reindex(names)}).dropna()


def outcomes(day, names, adj, adj_high, qqq, beta) -> pd.DataFrame:
    i = adj.index.get_loc(day)
    if i + H >= len(adj):
        return pd.DataFrame()
    p0 = adj.iloc[i][names]
    r = adj.iloc[i + H][names] / p0 - 1
    mx = adj_high.iloc[i + 1:i + H + 1][names].max() / p0 - 1
    rq = float(qqq.iloc[i + H] / qqq.iloc[i] - 1)
    out = pd.DataFrame({"ret": r, "max": mx, "excess": r - rq, "beta_adj": r - beta.reindex(names) * rq})
    for x in TOUCH:
        out[f"touch_{int(x * 100)}"] = (mx >= x).astype(float).where(mx.notna())
    return out.dropna(subset=["ret"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    close, adj, adj_high, adv, qqq = load(args.start)
    rs = rsi(adj)
    months = month_ends(adj.index, args.start, args.end)
    rows, per_name = [], {k: [] for k in ("HB", "FHB", "ALL")}
    for d in months:
        f = features(d, adj, close, adv, qqq, rs)
        if len(f) < 50:
            continue
        i = adj.index.get_loc(d)
        q_up = bool(qqq.iloc[i] > qqq.iloc[i - 49:i + 1].mean() and qqq.iloc[i] > qqq.iloc[i - H])
        hb = f.index[f["beta"] >= f["beta"].quantile(0.9)]
        fhb = f.loc[hb][(f.loc[hb, "dd"] <= -0.10) & (f.loc[hb, "dd"] >= -0.60) & f.loc[hb, "rsi"].between(30, 50)].index
        rq = float(qqq.iloc[i + H] / qqq.iloc[i] - 1) if i + H < len(adj) else np.nan
        row = {"day": d, "q_up": q_up, "qqq": rq, "n_univ": len(f), "n_hb": len(hb), "n_fhb": len(fhb)}
        for k, names in (("HB", hb), ("FHB", fhb), ("ALL", f.index)):
            o = outcomes(d, list(names), adj, adj_high, qqq, f["beta"])
            if o.empty:
                continue
            per_name[k].append(o.assign(day=d, q_up=q_up))
            row[f"{k}_ret"], row[f"{k}_excess"], row[f"{k}_beta_adj"] = o["ret"].mean(), o["excess"].mean(), o["beta_adj"].mean()
        if q_up and "FHB_ret" in row:
            row["FHB_UP_ret"], row["FHB_UP_excess"], row["FHB_UP_beta_adj"] = row["FHB_ret"], row["FHB_excess"], row["FHB_beta_adj"]
        elif not np.isnan(rq):
            row["FHB_UP_ret"], row["FHB_UP_excess"], row["FHB_UP_beta_adj"] = rq, 0.0, 0.0
        rows.append(row)
    m = pd.DataFrame(rows).set_index("day")
    summ = {}
    for k in ("ALL", "HB", "FHB", "FHB_UP"):
        if f"{k}_ret" not in m:
            continue
        x = m[[f"{k}_ret", f"{k}_excess", f"{k}_beta_adj"]].dropna()
        summ[k] = {"months": int(len(x)), "ret_mean": float(x[f"{k}_ret"].mean()), "excess_mean": float(x[f"{k}_excess"].mean()),
                   "excess_t": newey_west_t(x[f"{k}_excess"].to_numpy(), lag=1),
                   "beta_adj_mean": float(x[f"{k}_beta_adj"].mean()), "beta_adj_t": newey_west_t(x[f"{k}_beta_adj"].to_numpy(), lag=1),
                   "ann_excess": float((1 + x[f"{k}_excess"].mean()) ** 12 - 1)}
    touch = {}
    for k, parts in per_name.items():
        if not parts:
            continue
        o = pd.concat(parts)
        touch[k] = {"names": int(len(o)), "touch_6": float(o["touch_6"].mean()), "touch_15": float(o["touch_15"].mean()),
                    "ret_mean": float(o["ret"].mean()), "ret_median": float(o["ret"].median()),
                    "touch_6_up": float(o.loc[o["q_up"], "touch_6"].mean()), "touch_6_down": float(o.loc[~o["q_up"], "touch_6"].mean()),
                    "excess_up": float(o.loc[o["q_up"], "excess"].mean()), "excess_down": float(o.loc[~o["q_up"], "excess"].mean())}
    regime = {k: {"up_excess": float(m.loc[m["q_up"], f"{k}_excess"].mean()), "down_excess": float(m.loc[~m["q_up"], f"{k}_excess"].mean()),
                  "up_months": int(m["q_up"].sum()), "down_months": int((~m["q_up"]).sum())} for k in ("HB", "FHB") if f"{k}_excess" in m}
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end,
           "baskets": summ, "per_name": touch, "regime": regime,
           "sizes": {"univ_median": float(m["n_univ"].median()), "hb_median": float(m["n_hb"].median()), "fhb_median": float(m["n_fhb"].median())},
           "verdict": {"edge": bool(summ.get("FHB_UP", {}).get("beta_adj_mean", 0) > 0 and summ.get("FHB_UP", {}).get("beta_adj_t", 0) >= 2),
                       "first_target_is_volatility": bool(abs(touch.get("FHB", {}).get("touch_6", np.nan) - touch.get("HB", {}).get("touch_6", np.nan)) <= 0.05)}}
    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"s50_balder_replica_{stamp}")
    with open(base + ".json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    L = [f"# S50 — mechanical replica of Balder's monthly picks ({stamp})", "",
         f"{args.start} → {args.end}, month ends; universe ADV >= $100M and close >= $5 (median {out['sizes']['univ_median']:.0f} names); "
         f"HB = top-decile beta to QQQ (median {out['sizes']['hb_median']:.0f}); FHB = HB with drawdown -10%..-60% and RSI 30-50 "
         f"(median {out['sizes']['fhb_median']:.0f}); FHB_UP = FHB only when QQQ > 50-day average and up over 21 sessions, else QQQ.", "",
         "| basket | months | mean 21-day return | excess vs QQQ | t | beta-adjusted excess | t |", "|---|---|---|---|---|---|---|"]
    for k, v in summ.items():
        L.append(f"| {k} | {v['months']} | {v['ret_mean']:+.2%} | {v['excess_mean']:+.2%} | {v['excess_t']:.2f} | {v['beta_adj_mean']:+.2%} | {v['beta_adj_t']:.2f} |")
    L += ["", "| names | n | touch +6% | touch +15% | mean return | median | touch +6% (QQQ up / down months) | excess (up / down) |", "|---|---|---|---|---|---|---|---|"]
    for k, v in touch.items():
        L.append(f"| {k} | {v['names']} | {v['touch_6']:.0%} | {v['touch_15']:.0%} | {v['ret_mean']:+.2%} | {v['ret_median']:+.2%} | "
                 f"{v['touch_6_up']:.0%} / {v['touch_6_down']:.0%} | {v['excess_up']:+.2%} / {v['excess_down']:+.2%} |")
    L += ["", f"Verdict: mechanical edge {out['verdict']['edge']}; first-target hits explained by volatility {out['verdict']['first_target_is_volatility']}."]
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
