"""S11: the insider-buy test again, on the universe where the evidence lives.

S8 found the right shape but not significance (best t 1.95), and named the
reason: S&P 500 executives rarely buy on the open market, and the
literature's insider effect is a small-cap phenomenon. S9 showed free
prices could not support a wider universe; S10 showed Alpaca's free tier
can (NYSE 98% / NASDAQ 85% including delisted).

Universe here is point-in-time and survivorship-free by construction:
a name is in on day D if
  - listing_status says it was listed (ipo_date <= D < delisting_date), and
  - it had a Form 4 filer that quarter (panel.issuer_seen), and
  - its 20-day dollar volume on D clears --min-adv (default $1M), which is
    what makes a position executable at all.
Size buckets split the result: micro (<$300M), small ($300M-$2B), mid
($2B-$10B), large (>$10B), using price x 20-day volume as a crude proxy
for float since we have no share counts here.

Usage: python -m agent.s11_insider_wide [--start 2015-01-01] [--min-adv 1e6]
Writes site-data/validation/s11_<date>.{json,md}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")


def listed_mask(store: PanelStore, dates: pd.DatetimeIndex, tickers: list[str]) -> pd.DataFrame:
    """True where listing_status says the symbol was trading that day."""
    ls = store.con.execute("""SELECT symbol, min(ipo_date) AS ipo, max(delisting_date) AS delist
                              FROM listing_status WHERE asset_type = 'Stock' GROUP BY 1""").df()
    ls = ls[ls["symbol"].isin(tickers)]
    out = pd.DataFrame(False, index=dates, columns=tickers)
    for sym, ipo, delist in ls.itertuples(index=False):
        lo = pd.Timestamp(ipo) if pd.notna(ipo) else dates[0]
        hi = pd.Timestamp(delist) if pd.notna(delist) else dates[-1]
        out.loc[(dates >= lo) & (dates <= hi), sym] = True
    return out


def event_stats(ev: pd.DataFrame, fwd: pd.DataFrame, mkt: pd.Series, horizon: int, label: str) -> dict:
    rows = []
    for d, g in ev.groupby("date"):
        if d not in fwd.index or pd.isna(mkt.get(d, np.nan)):
            continue
        r = fwd.loc[d].reindex(g["ticker"]).dropna()
        if r.empty:
            continue
        rows.append({"date": d, "n": len(r), "abn": float(r.mean() - mkt.loc[d])})
    if len(rows) < 30:
        return {"label": label, "n_dates": len(rows)}
    df = pd.DataFrame(rows).set_index("date").sort_index()
    x = df["abn"].to_numpy()
    boot = bootstrap_ci(x, n_boot=2000)
    by_year = df["abn"].groupby(df.index.year).mean()
    return {"label": label, "horizon": horizon, "n_events": int(df["n"].sum()), "n_dates": int(len(df)),
            "mean_abn_pct": float(x.mean()), "median_abn_pct": float(np.median(x)),
            "t_nw": newey_west_t(x, lag=max(horizon // 5, 1)), "boot_ci95": [boot["lo"], boot["hi"]],
            "hit_rate": float((x > 0).mean()), "share_years_positive": float((by_year > 0).mean()),
            "first_half": float(df["abn"].iloc[: len(df) // 2].mean()),
            "second_half": float(df["abn"].iloc[len(df) // 2:].mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--min-adv", type=float, default=1e6)
    ap.add_argument("--horizons", default="5,10,21,63")
    args = ap.parse_args()
    horizons = [int(h) for h in args.horizons.split(",")]

    with PanelStore(read_only=True) as store:
        ev = store.con.execute("""
            SELECT filing_date, ticker, count(DISTINCT owner_name) AS n_buyers, sum(value_usd) AS buy_usd
            FROM insider_tx WHERE trans_code = 'P' AND acq_disp = 'A' AND filing_date >= ?
            GROUP BY 1, 2""", [args.start]).df()
        close = store.bars_wide("close", start=args.start)
        adj = store.bars_wide("adj_close", start=args.start)
        opn = store.bars_wide("open", start=args.start)
        vol = store.bars_wide("volume", start=args.start)
        spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()
        listed = listed_mask(store, adj.index, list(adj.columns))

    adj_open = opn * (adj / close)
    adv20 = (close * vol).rolling(20).mean()
    tradable = listed & (adv20 >= args.min_adv) & adj.notna()
    size = close * vol.rolling(20).mean()          # crude size proxy: 20-day dollar volume

    ev["date"] = pd.to_datetime(ev["filing_date"])
    ev = ev[ev["date"].isin(adj.index) & ev["ticker"].isin(adj.columns)]
    keep = [tradable.at[d, t] for d, t in zip(ev["date"], ev["ticker"])]
    ev = ev[keep]
    adv_at = pd.Series([size.at[d, t] for d, t in zip(ev["date"], ev["ticker"])], index=ev.index)
    ev["bucket"] = pd.cut(adv_at, [-np.inf, 3e6, 2e7, 1e8, np.inf],
                          labels=["micro", "small", "mid", "large"])

    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "min_adv_usd": args.min_adv, "universe": "listed & liquid, point-in-time (Alpaca bars)",
              "n_events": int(len(ev)), "n_tickers": int(ev["ticker"].nunique()),
              "n_names_panel": int(adj.shape[1]), "horizons": {}}

    for h in horizons:
        fwd = ((adj.shift(-h) / adj_open.shift(-1) - 1) * 100).where(tradable)
        mkt = (spy.shift(-h) / spy.shift(-1) - 1) * 100
        block = {"all_buys": event_stats(ev, fwd, mkt, h, "all"),
                 "cluster": event_stats(ev[ev["n_buyers"] >= 2], fwd, mkt, h, ">=2 buyers"),
                 "big_usd": event_stats(ev[ev["buy_usd"] >= 250_000], fwd, mkt, h, ">= $250k")}
        for b in ("micro", "small", "mid", "large"):
            block[f"size_{b}"] = event_stats(ev[ev["bucket"] == b], fwd, mkt, h, f"{b} caps")
        report["horizons"][h] = block

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s11_{stamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=float)

    L = [f"# S11 report — insider buys on the wide universe ({stamp})", "",
         f"{args.start} → today. Universe: listed (point-in-time) and 20-day dollar volume >= "
         f"${args.min_adv:,.0f}; {report['n_names_panel']} names in the panel. "
         f"{report['n_events']} filing events across {report['n_tickers']} tickers. "
         f"Abnormal = event mean minus SPY over the same window, entry at the next open.", "",
         "| variant | h | events | dates | mean abn | NW t | boot CI95 | hit | yrs>0 | 1st half | 2nd half |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for h, block in report["horizons"].items():
        for name, s in block.items():
            if s.get("n_dates", 0) < 30:
                L.append(f"| {name} | {h} | — | {s.get('n_dates', 0)} | too few | | | | | | |")
                continue
            L.append(f"| {name} ({s['label']}) | {h} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.3f}% | "
                     f"{s['t_nw']:+.2f} | [{s['boot_ci95'][0]:+.3f}, {s['boot_ci95'][1]:+.3f}] | "
                     f"{s['hit_rate']:.0%} | {s['share_years_positive']:.0%} | {s['first_half']:+.3f}% | "
                     f"{s['second_half']:+.3f}% |")
    with open(path.replace(".json", ".md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
