"""S53a — ADS ratio data correction: checks and the long book's restatement (docs/AGENT_PLAN.md "### S53").

Runs on a COPY of panel.db (--panel), never the live one:
  prepare  ads_ratio (agent.sources.ads_ratio CACHE + agent/ads_ratio.yaml) into the copy; the copy's
           fundamentals_pit kept as fundamentals_pit_before, then rebuilt with the S53a build
  check    the pre-registered checks that must pass before the switch (S53a 修正定义 4):
           (1) every foreign filer scored on the last bar: our market cap (listed-unit shares x close)
               against moomoo's snapshot (--moomoo csv: ticker, issued_shares, last_price, total_market_val);
               more than 20% apart -> to be checked by hand
           (2) every row whose ratio differs from the same company's previous filing -> listed
  restate  long v1, 2017-01-01 -> 2026-08-31, same rules and engine settings as S47 / S47b, before and after
           on the same copy (so only the fundamentals differ); the insider book does not read fundamentals
           and is not rerun. Plus: daily top-30 overlap, foreign filers in the top 30 / 60, the names that
           entered or left most often, and the last bar's top 30 / 60 before and after (the evaluate_from
           rule is applied on the switch day's bar, this is a preview).
A restatement is not a new variant (+0); whatever the numbers, v1 keeps running.

Usage: python -m agent.s53a_restate prepare --panel /path/copy/panel.db
       python -m agent.s53a_restate check --panel ... --moomoo snap.csv
       python -m agent.s53a_restate restate --panel ...
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import numpy as np
import pandas as pd

from agent.books import fundamentals as F
from agent.books import long_v2_bundle as v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.s44_v2_bundle import entry_zone
from agent.sources import ads_ratio
from hedge_fund.features.panel import PanelStore

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site-data", "validation")
START, END = "2017-01-01", "2026-08-31"
OFFICIAL = {"alpha2_ann_pct": 8.54, "alpha2_t_nw": None, "source": "s47b_restatement_2026-09-30 (the current base)"}
KEYS = ["cagr_pct", "alpha2_ann_pct", "alpha2_t_nw", "excess_cagr_pct", "active_t_nw", "beta_mkt", "beta_size", "vol_pct",
        "max_drawdown_pct", "sharpe", "n_trades", "trade_hit_rate", "avg_exposure", "by_year"]
MCAP_TOL = 0.20


def prepare(panel: str) -> None:
    ads_ratio.commit(panel)
    with PanelStore(panel) as store:
        have = set(store.con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])
        if "fundamentals_pit_before" not in have:
            store.con.execute("CREATE TABLE fundamentals_pit_before AS SELECT * FROM fundamentals_pit")
        F.factor_inputs(store)
        n = store.con.execute("SELECT shares_flag, count(*) n FROM fundamentals_pit GROUP BY 1 ORDER BY 2 DESC").df()
        print(n.to_string(index=False))


def _fpi_ciks(store: PanelStore) -> set[int]:
    return set(store.con.execute("SELECT DISTINCT cik FROM xbrl_facts WHERE form IN ('20-F','40-F')").df()["cik"].astype(int))


def check(panel: str, moomoo_csv: str) -> dict:
    with PanelStore(panel, read_only=True) as store:
        market = load_market(store, "2025-06-01")
        after = fundamentals(store)
        before = fundamentals(store, "fundamentals_pit_before")
        fpi = _fpi_ciks(store)
        ratios = store.con.execute("SELECT cik, accn, form, filed, ratio, status, cover_text, doc FROM ads_ratio").df()
    day = market.close.index[-1]
    fs = v2.scored(market, after, day)
    lb = after[(after["filed"] <= day) & (after["filed"] >= day - pd.Timedelta(days=200))]
    lb = lb.sort_values("filed").drop_duplicates("ticker", keep="last").set_index("ticker")
    lb0 = before[(before["filed"] <= day) & (before["filed"] >= day - pd.Timedelta(days=200))]
    lb0 = lb0.sort_values("filed").drop_duplicates("ticker", keep="last").set_index("ticker")
    fpi_t = lb.index[lb["cik"].astype(int).isin(fpi)]
    snap = pd.read_csv(moomoo_csv).set_index("ticker")
    close = market.close.loc[day]
    rows = []
    for t in fpi_t:
        mm = snap["total_market_val"].get(t, np.nan)
        ours = close.get(t, np.nan) * lb.at[t, "shares"]
        old = close.get(t, np.nan) * lb0.at[t, "shares"] if t in lb0.index else np.nan
        rows.append({"ticker": t, "cik": int(lb.at[t, "cik"]), "filed": lb.at[t, "filed"].date(), "flag": lb.at[t, "shares_flag"],
                     "scored": t in fs.index, "rank": int(fs.index.get_loc(t)) + 1 if t in fs.index else None,
                     "mcap_after": ours, "mcap_before": old, "mcap_moomoo": mm,
                     "dev_after": ours / mm - 1 if mm and mm > 0 else np.nan, "dev_before": old / mm - 1 if mm and mm > 0 else np.nan})
    cx = pd.DataFrame(rows)
    has = cx["mcap_after"].notna() & cx["mcap_moomoo"].notna()
    bad = cx[has & (cx["dev_after"].abs() > MCAP_TOL)].sort_values("dev_after")
    # ratio changes between consecutive filings of a company
    r = ratios[ratios["ratio"].notna()].sort_values(["cik", "filed"])
    r["prev"] = r.groupby("cik")["ratio"].shift()
    changes = r[r["prev"].notna() & (r["ratio"] != r["prev"])]
    out = {"day": str(day.date()), "fpi_rows_last_200d": int(len(cx)), "with_both_mcaps": int(has.sum()),
           "within_20pct_after": int((has & (cx["dev_after"].abs() <= MCAP_TOL)).sum()),
           "within_20pct_before": int((cx["mcap_before"].notna() & cx["mcap_moomoo"].notna() & (cx["dev_before"].abs() <= MCAP_TOL)).sum()),
           "scored_fpi": int(cx["scored"].sum()),
           "scored_outside_20pct": bad[bad["scored"]][["ticker", "rank", "dev_after", "dev_before", "flag"]].to_dict("records"),
           "outside_20pct": bad[["ticker", "dev_after", "dev_before", "flag", "scored"]].to_dict("records"),
           "ratio_changes": changes[["cik", "filed", "prev", "ratio", "status", "doc"]].astype({"filed": str}).to_dict("records"),
           "status_counts": ratios["status"].value_counts().to_dict()}
    stamp = dt.date.today().isoformat()
    cx.to_csv(os.path.join(os.path.dirname(panel), f"s53a_crosscheck_{stamp}.csv"), index=False)
    with open(os.path.join(os.path.dirname(panel), f"s53a_check_{stamp}.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps({k: v for k, v in out.items() if k not in ("outside_20pct", "ratio_changes")}, indent=1, default=str))
    print(f"outside 20%: {len(bad)}, of them scored: {int(bad['scored'].sum())}; ratio changes: {len(changes)}")
    return out


def _lists(market, fund) -> dict:
    return {d: v2.scored(market, fund, d) for d in market.adj.loc[START:END].index}


def restate(panel: str) -> dict:
    t0 = time.time()
    with PanelStore(panel, read_only=True) as store:
        market = load_market(store, START)
        f_after = fundamentals(store)
        f_before = fundamentals(store, "fundamentals_pit_before")
        fpi = _fpi_ciks(store)
        tick_cik = f_after.drop_duplicates("ticker", keep="last").set_index("ticker")["cik"].astype(int)
    fpi_t = set(tick_cik[tick_cik.isin(fpi)].index)
    res, lists, scored = {}, {}, {}
    for name, fund in (("before", f_before), ("after", f_after)):
        sc = _lists(market, fund)
        scored[name] = sc
        lists[name] = {d: fs.index[:v2.KEEP_MULT * TOP_N].tolist() for d, fs in sc.items()}
        r = simulate(market, lists[name], START, END, TOP_N, None, 0.5, cash_in_spy=True, sizes=entry_zone(lists[name], TOP_N))
        res[name] = r
        print(f"{name}: alpha2 {r.metrics['alpha2_ann_pct']:+.2f}% t {r.metrics['alpha2_t_nw']:.2f} [{time.time() - t0:.0f}s]", flush=True)
    days = list(lists["after"])
    ov30 = pd.Series({d: len(set(lists["before"][d][:TOP_N]) & set(lists["after"][d][:TOP_N])) for d in days})
    fpi30 = {k: pd.Series({d: len(set(lists[k][d][:TOP_N]) & fpi_t) for d in days}) for k in lists}
    fpi60 = {k: pd.Series({d: len(set(lists[k][d]) & fpi_t) for d in days}) for k in lists}
    entered, left = {}, {}
    for d in days:
        b, a = set(lists["before"][d][:TOP_N]), set(lists["after"][d][:TOP_N])
        for t in a - b:
            entered[t] = entered.get(t, 0) + 1
        for t in b - a:
            left[t] = left.get(t, 0) + 1
    top = lambda m: sorted(m.items(), key=lambda kv: -kv[1])[:15]
    after = {k: res["after"].metrics.get(k) for k in KEYS}
    before = {k: res["before"].metrics.get(k) for k in KEYS}
    out = {"start": START, "end": END, "official_base": OFFICIAL, "before": before, "restated": after,
           "top30_overlap": {"mean": float(ov30.mean()), "min": int(ov30.min()), "days_lt_28": int((ov30 < 28).sum()), "n_days": len(days)},
           "fpi_in_top30_mean": {k: float(v.mean()) for k, v in fpi30.items()},
           "fpi_in_top60_mean": {k: float(v.mean()) for k, v in fpi60.items()},
           "entered_top30_days": top(entered), "left_top30_days": top(left)}
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s53a_restatement_{stamp}")
    res["after"].nav.rename("v1c").to_frame().to_csv(os.path.join(OUT_DIR, "s53a_base_nav.csv"))
    with open(path + ".json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    m0, m1 = before, after
    L = [f"# S53a — ADS ratio data correction: long v1 restatement ({stamp})", "",
         f"{START} → {END}. Same rules and engine settings as S47 / S47b; before and after run on the same copy of the "
         "panel, so only the share counts of 20-F/40-F filers differ. Not a new variant (+0); v1 keeps running whatever "
         "the result. The insider book does not read fundamentals and is unchanged.", "",
         "| | CAGR | alpha2/yr | alpha2 t | excess vs SPY | active t | MaxDD | Sharpe | trades | hit |", "|---|---|---|---|---|---|---|---|---|---|"]
    for lab, m in (("before (ratio taken as 1)", m0), ("restated (listed units)", m1)):
        L.append(f"| {lab} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.2f}% | {m['alpha2_t_nw']:.2f} | {m['excess_cagr_pct']:+.1f}% | "
                 f"{m['active_t_nw']:.2f} | {m['max_drawdown_pct']:.0f}% | {m['sharpe']:.2f} | {m['n_trades']} | {m['trade_hit_rate']:.0%} |")
    L += ["", f"Official base before this correction: {OFFICIAL['source']}, alpha2 {OFFICIAL['alpha2_ann_pct']:+.2f}%/yr.", "",
          f"Top 30, before vs after, same day: mean overlap {ov30.mean():.1f}/30, minimum {ov30.min()}, "
          f"{int((ov30 < 28).sum())} of {len(days)} days below 28.",
          f"Foreign filers in the top 30 (mean per day): before {fpi30['before'].mean():.2f}, after {fpi30['after'].mean():.2f}; "
          f"in the top 60: before {fpi60['before'].mean():.2f}, after {fpi60['after'].mean():.2f}.", "",
          "Entered the top 30 because of the correction (days): " + ", ".join(f"{t} {n}" for t, n in top(entered)),
          "", "Left the top 30 (days): " + ", ".join(f"{t} {n}" for t, n in top(left)), "",
          "By year (total return)", "", "| | " + " | ".join(str(y) for y in m1["by_year"]) + " |", "|---|" + "---|" * len(m1["by_year"])]
    for lab, m in (("before", m0), ("restated", m1)):
        L.append(f"| {lab} | " + " | ".join(f"{m['by_year'].get(y, float('nan')) * 100:+.0f}%" for y in m1["by_year"]) + " |")
    text = "\n".join(L) + "\n"
    with open(path + ".md", "w") as f:
        f.write(text)
    print(text)
    return out


def preview(panel: str) -> dict:
    """The last bar's top 30 / 60 before and after (the switch day's comparison decides evaluate_from)."""
    with PanelStore(panel, read_only=True) as store:
        market = load_market(store, "2025-06-01")
        a, b = fundamentals(store), fundamentals(store, "fundamentals_pit_before")
    day = market.close.index[-1]
    la, lb = v2.scored(market, a, day).index, v2.scored(market, b, day).index
    out = {"day": str(day.date()), "top30_same": len(set(la[:TOP_N]) & set(lb[:TOP_N])),
           "top60_same": len(set(la[:2 * TOP_N]) & set(lb[:2 * TOP_N])),
           "top30_in": sorted(set(la[:TOP_N]) - set(lb[:TOP_N])), "top30_out": sorted(set(lb[:TOP_N]) - set(la[:TOP_N])),
           "top60_in": sorted(set(la[:2 * TOP_N]) - set(lb[:2 * TOP_N])), "top60_out": sorted(set(lb[:2 * TOP_N]) - set(la[:2 * TOP_N]))}
    print(json.dumps(out, indent=1))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "check", "restate", "preview"])
    ap.add_argument("--panel", required=True)
    ap.add_argument("--moomoo")
    a = ap.parse_args(argv)
    live = os.path.realpath(os.path.expanduser("~/.hedge-fund/agent/panel.db"))
    if os.path.realpath(a.panel) == live:
        raise SystemExit("S53a runs on a copy of panel.db, not the live one")
    {"prepare": lambda: prepare(a.panel), "check": lambda: check(a.panel, a.moomoo),
     "restate": lambda: restate(a.panel), "preview": lambda: preview(a.panel)}[a.cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
