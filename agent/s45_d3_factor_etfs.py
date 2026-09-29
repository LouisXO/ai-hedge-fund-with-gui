"""S45 D3 — the long book's base against factor ETFs (docs/AGENT_PLAN.md "### S45", report only).

The daily returns of the long v1 base on the corrected data (site-data/validation/s47_base_nav.csv, the S47
restatement) regressed on SPY and IWM - SPY (= alpha2), then with factor ETFs added as returns in excess of
SPY: momentum (MTUM), value (VLUE), quality (QUAL), low volatility (USMV), small value (IJS - IWM) and small
growth vs small value (IWO - IWN). Read: how much of alpha2 is left once the book's factor tilts are priced
at what an ETF would have charged for them. Adjusted closes from index_daily (agent.backfill.FACTOR_ETFS,
backfilled 2026-09-29); NW t with lag 5, as engine.metrics. AVUV starts 2019-09, so the model that uses it
runs on its own shorter window. Counted once in the ledger at S45 (D3 +1); no variant is chosen from it.

Usage: python -m agent.s45_d3_factor_etfs
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
NAV = os.path.join(OUT_DIR, "s47_base_nav.csv")
START, END = "2017-01-01", "2026-08-31"
SYMBOLS = ["SPY", "IWM", "MTUM", "VLUE", "QUAL", "USMV", "IJS", "IWN", "IWO", "AVUV", "QQQ"]
MODELS = {                                    # name -> regressors (columns of the factor frame)
    "alpha2 (SPY, IWM-SPY)": ["mkt", "size"],
    "+ QQQ-SPY": ["mkt", "size", "qqq"],
    "+ momentum, value, quality, low vol": ["mkt", "size", "mom", "val", "qual", "lowvol"],
    "+ small value, small growth-value": ["mkt", "size", "mom", "val", "qual", "lowvol", "sv", "sgv"],
    "AVUV window: alpha2": ["mkt", "size"],
    "AVUV window: + all above + AVUV-IWM": ["mkt", "size", "mom", "val", "qual", "lowvol", "sv", "sgv", "avuv"],
}


def factors(store) -> pd.DataFrame:
    px = store.con.execute(f"""SELECT symbol, trade_date, adj_close FROM index_daily
                               WHERE symbol IN ({', '.join('?' * len(SYMBOLS))})""", SYMBOLS).df()
    r = px.pivot(index="trade_date", columns="symbol", values="adj_close").sort_index().pct_change()
    r.index = pd.to_datetime(r.index)
    return pd.DataFrame({"mkt": r["SPY"], "size": r["IWM"] - r["SPY"], "qqq": r["QQQ"] - r["SPY"],
                         "mom": r["MTUM"] - r["SPY"], "val": r["VLUE"] - r["SPY"], "qual": r["QUAL"] - r["SPY"],
                         "lowvol": r["USMV"] - r["SPY"], "sv": r["IJS"] - r["IWM"], "sgv": r["IWO"] - r["IWN"],
                         "avuv": r["AVUV"] - r["IWM"]})


def fit(y: pd.Series, X: pd.DataFrame) -> dict:
    d = pd.concat([y.rename("y"), X], axis=1).dropna()
    A = np.column_stack([np.ones(len(d)), d[X.columns].to_numpy()])
    b = np.linalg.lstsq(A, d["y"].to_numpy(), rcond=None)[0]
    resid = d["y"].to_numpy() - A @ b
    r2 = 1 - resid.var() / d["y"].var()
    return {"n_days": int(len(d)), "start": str(d.index[0].date()), "end": str(d.index[-1].date()),
            "alpha_ann_pct": float(b[0] * 252 * 100), "alpha_t_nw": float(newey_west_t(resid + b[0], lag=5)),
            "betas": {c: round(float(v), 3) for c, v in zip(X.columns, b[1:])}, "r2": round(float(r2), 3)}


def main() -> int:
    nav = pd.read_csv(NAV, index_col=0, parse_dates=True).iloc[:, 0]
    y = nav.pct_change().loc[START:END].dropna()
    with PanelStore(read_only=True) as store:
        F = factors(store)
    out = {}
    for name, cols in MODELS.items():
        yy = y.loc["2019-10-01":] if name.startswith("AVUV") else y
        out[name] = fit(yy, F[cols])
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s45_d3_factor_etfs_{stamp}")
    with open(path + ".json", "w") as f:
        json.dump({"nav": os.path.basename(NAV), "start": START, "end": END, "models": out}, f, indent=1)
    names = {"mkt": "SPY", "size": "IWM-SPY", "qqq": "QQQ-SPY", "mom": "MTUM", "val": "VLUE", "qual": "QUAL",
             "lowvol": "USMV", "sv": "IJS-IWM", "sgv": "IWO-IWN", "avuv": "AVUV-IWM"}
    L = [f"# S45 D3 — long v1 base vs factor ETFs ({stamp})", "",
         f"Base: {os.path.basename(NAV)} (S47 restatement), daily, {START} → {END}. Factor ETFs in excess of SPY "
         "(small value and AVUV in excess of IWM). NW t, lag 5. Report only (S45).", "",
         "| model | days | alpha/yr | t | R² | betas |", "|---|---|---|---|---|---|"]
    for name, m in out.items():
        betas = ", ".join(f"{names[k]} {v:+.2f}" for k, v in m["betas"].items())
        L.append(f"| {name} | {m['n_days']} | {m['alpha_ann_pct']:+.2f}% | {m['alpha_t_nw']:.2f} | {m['r2']:.2f} | {betas} |")
    text = "\n".join(L) + "\n"
    with open(path + ".md", "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
