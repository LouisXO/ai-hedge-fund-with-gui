"""S13: real quoted spreads from Alpaca, because the high-low estimator failed calibration.

S12 charged a Corwin-Schultz spread and every net return went negative.
But that estimator put S&P 500 large caps at 0.72% round trip, ~20x their
true spread, so the conclusion was an artifact of the cost model, not a
result.

Alpaca's free tier serves historical NBBO quotes (verified: AAPL 2016
median relative spread 0.010%). Quoting every one of the 46k insider
events is unnecessary; spreads are a property of liquidity and era, so
this samples (ADV bucket x year) cells, takes the median relative spread
inside a one-hour mid-session window, and uses that grid as the cost of
any event in the same cell.

Usage:
  python -m agent.s13_real_spreads sample [--per-cell 25]   # builds the grid
  python -m agent.s13_real_spreads net                      # applies it to S11's events
Writes panel.spread_grid and site-data/validation/s13_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import urllib.parse

import numpy as np
import pandas as pd

from agent.s11_insider_wide import listed_mask
from agent.sources.price_probe import _get, keys_from_env
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")
BUCKETS = [-np.inf, 3e6, 2e7, 1e8, np.inf]
LABELS = ["micro", "small", "mid", "large"]
DDL = """CREATE TABLE IF NOT EXISTS spread_grid (
    bucket VARCHAR, year INT, n_obs INT, median_spread_pct DOUBLE, p75_spread_pct DOUBLE,
    measured_at TIMESTAMP, PRIMARY KEY (bucket, year))"""


def quote_spread(sym: str, day: pd.Timestamp, key: str, secret: str) -> float | None:
    """Median relative NBBO spread in a mid-session hour (14:30-15:30 UTC = 10:30-11:30 ET)."""
    q = urllib.parse.urlencode({"start": f"{day.date()}T14:30:00Z", "end": f"{day.date()}T15:30:00Z",
                                "limit": 300, "feed": "sip"})
    code, body = _get(f"https://data.alpaca.markets/v2/stocks/{sym}/quotes?{q}",
                      {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    if code != 200:
        return None
    rows = json.loads(body).get("quotes") or []
    sp = [(r["ap"] - r["bp"]) / ((r["ap"] + r["bp"]) / 2) * 100
          for r in rows if r.get("ap", 0) > 0 and r.get("bp", 0) > 0 and r["ap"] >= r["bp"]]
    return float(np.median(sp)) if len(sp) >= 10 else None


def build_grid(store: PanelStore, per_cell: int, pause: float = 0.25) -> pd.DataFrame:
    key, secret = keys_from_env("alpaca")
    close = store.bars_wide("close")
    vol = store.bars_wide("volume")
    adv = (close * vol).rolling(20).mean()
    rng = np.random.default_rng(7)
    rows = []
    for year in range(2016, dt.date.today().year + 1):
        days = adv.index[(adv.index.year == year)]
        if len(days) == 0:
            continue
        for label in LABELS:
            lo, hi = BUCKETS[LABELS.index(label)], BUCKETS[LABELS.index(label) + 1]
            got = []
            tries = 0
            while len(got) < per_cell and tries < per_cell * 6:
                tries += 1
                d = days[rng.integers(0, len(days))]
                row = adv.loc[d]
                cand = row[(row > lo) & (row <= hi)].dropna()
                if cand.empty:
                    continue
                sym = str(cand.index[rng.integers(0, len(cand))])
                s = quote_spread(sym, d, key, secret)
                time.sleep(pause)
                if s is not None:
                    got.append(s)
            if got:
                rows.append({"bucket": label, "year": year, "n_obs": len(got),
                             "median_spread_pct": float(np.median(got)),
                             "p75_spread_pct": float(np.percentile(got, 75)),
                             "measured_at": pd.Timestamp.now()})
                print(f"  {year} {label:6} n={len(got):3} median {np.median(got):.3f}%", flush=True)
    df = pd.DataFrame(rows)
    store.con.execute(DDL)
    store.insert("spread_grid", df)
    return df


def net_returns(store: PanelStore, start: str, min_adv: float, horizons: list[int]) -> dict:
    grid = store.con.execute("SELECT bucket, year, median_spread_pct FROM spread_grid").df()
    gmap = {(r.bucket, r.year): r.median_spread_pct for r in grid.itertuples()}
    ev = store.con.execute("""
        SELECT filing_date, ticker, count(DISTINCT owner_name) AS n_buyers, sum(value_usd) AS buy_usd
        FROM insider_tx WHERE trans_code='P' AND acq_disp='A' AND filing_date >= ?
        GROUP BY 1,2""", [start]).df()
    close, adj = store.bars_wide("close", start=start), store.bars_wide("adj_close", start=start)
    opn, vol = store.bars_wide("open", start=start), store.bars_wide("volume", start=start)
    spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()
    listed = listed_mask(store, adj.index, list(adj.columns))
    adj_open = opn * (adj / close)
    adv20 = (close * vol).rolling(20).mean()
    tradable = listed & (adv20 >= min_adv) & adj.notna()

    ev["date"] = pd.to_datetime(ev["filing_date"])
    ev = ev[ev["date"].isin(adj.index) & ev["ticker"].isin(adj.columns)]
    ev = ev[[tradable.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]]
    ev["adv"] = [adv20.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]
    ev["bucket"] = pd.cut(ev["adv"], BUCKETS, labels=LABELS)
    ev["spread"] = [gmap.get((str(b), d.year), np.nan) for b, d in zip(ev["bucket"], ev["date"])]
    ev = ev.dropna(subset=["spread"])

    out = {"n_events": int(len(ev)), "horizons": {}}
    for h in horizons:
        fwd = ((adj.shift(-h) / adj_open.shift(-1) - 1) * 100).where(tradable)
        mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
        block = {}
        groups = [("all", ev), ("cluster", ev[ev["n_buyers"] >= 2]),
                  ("big_usd", ev[ev["buy_usd"] >= 250_000])]
        groups += [(f"size_{b}", ev[ev["bucket"] == b]) for b in LABELS]
        for name, sub in groups:
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
            entry = {"n_dates": int(len(df)), "gross_pct": float(df["gross"].mean()),
                     "spread_pct": float(df["spread"].mean())}
            # how much of the quoted spread the execution actually pays: 1.0 = market
            # orders both ways, 0.5 = one side crossed, 0.0 = both fills at the midpoint
            for frac in (1.0, 0.5, 0.25):
                net = (df["gross"] - frac * df["spread"]).to_numpy()
                boot = bootstrap_ci(net, n_boot=2000)
                by_year = pd.Series(net, index=df.index).groupby(df.index.year).mean()
                entry[f"net_{frac}"] = {"pct": float(net.mean()),
                                        "t_nw": newey_west_t(net, lag=max(h // 5, 1)),
                                        "ci95": [boot["lo"], boot["hi"]],
                                        "years_positive": float((by_year > 0).mean())}
            block[name] = entry
        out["horizons"][h] = block
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "net"])
    ap.add_argument("--per-cell", type=int, default=25)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--min-adv", type=float, default=1e6)
    ap.add_argument("--horizons", default="5,10")
    args = ap.parse_args()
    horizons = [int(h) for h in args.horizons.split(",")]

    with PanelStore() as store:
        if args.cmd == "sample":
            df = build_grid(store, args.per_cell)
            print(df.to_string(index=False))
            return 0
        rep = net_returns(store, args.start, args.min_adv, horizons)

    rep.update({"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
                "min_adv_usd": args.min_adv,
                "spread_model": "Alpaca NBBO median, sampled by (ADV bucket x year), one round trip"})
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s13_{stamp}.json")
    with open(path, "w") as f:
        json.dump(rep, f, indent=1, default=float)
    L = [f"# S13 report — insider edge net of real quoted spreads ({stamp})", "",
         f"{args.start} → today, ADV floor ${args.min_adv:,.0f}, {rep['n_events']} events. "
         f"Spread = median Alpaca NBBO in a mid-session hour, sampled per (ADV bucket x year); "
         f"one full round trip charged.", "",
         "Cost sensitivity: the fraction of the quoted spread the execution pays "
         "(1.0 = market orders both ways, 0.5 = cross once, 0.25 = mostly passive).", "",
         "| group | h | dates | gross | spread | net @1.0 | t | net @0.5 | t | net @0.25 | t | yrs>0 @0.5 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for h, block in rep["horizons"].items():
        for name, s in block.items():
            if s.get("n_dates", 0) < 30:
                continue
            L.append(f"| {name} | {h} | {s['n_dates']} | {s['gross_pct']:+.3f}% | {s['spread_pct']:.3f}% | "
                     f"{s['net_1.0']['pct']:+.3f}% | {s['net_1.0']['t_nw']:+.2f} | "
                     f"{s['net_0.5']['pct']:+.3f}% | {s['net_0.5']['t_nw']:+.2f} | "
                     f"{s['net_0.25']['pct']:+.3f}% | {s['net_0.25']['t_nw']:+.2f} | "
                     f"{s['net_0.5']['years_positive']:.0%} |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
