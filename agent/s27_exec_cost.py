"""S27 — replace the backtest's cost assumption with the measured paper execution cost.

The books were backtested at 0.5 x quoted spread per side (≈0.1–0.3%).
The first live morning (2026-09-22, DAY limit orders) filled +0.61% above
the open on average. This reads every filled paper order, measures the
cost per side by book and order type (opg vs day), and re-runs the two
books with that cost as a flat % per side, so the alpha numbers can be
compared with the ones the books were adopted on.

Usage: python -m agent.s27_exec_cost [--min-fills 20] [--start 2017-01-01] [--end 2026-08-31]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import pandas as pd

from agent import ledger
from agent.books import long_v2, short_term
from agent.books.data import fundamentals, insider_flows, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"


def measured(con) -> pd.DataFrame:
    df = con.execute("""SELECT book, tif, side, ticker, CAST(filled_at AS DATE) AS day, filled_qty, filled_avg_px, model_px, ref_close
                        FROM agent_orders WHERE filled_qty > 0 AND model_px > 0 AND dry_run = FALSE""").df()
    if df.empty:
        return df
    df["cost_pct"] = ((df["filled_avg_px"] / df["model_px"] - 1) * 100).where(df["side"] == "buy",
                                                                             (df["model_px"] / df["filled_avg_px"] - 1) * 100)
    return df


def summary(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["book", "tif", "side"])["cost_pct"].agg(["count", "mean", "median", "std"]).reset_index()
    return g.round(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-fills", type=int, default=20)
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--cost", type=float, default=None, help="override: flat cost % per side")
    args = ap.parse_args()
    con = ledger.connect(ledger.OPTRADAR_DB, read_only=True)
    try:
        df = measured(con)
    finally:
        con.close()
    print(summary(df).to_string(index=False) if not df.empty else "no fills yet")
    per_book = {}
    for book in ("long", "insider"):
        sub = df[df["book"] == book] if not df.empty else df
        per_book[book] = float(sub["cost_pct"].mean()) if len(sub) >= args.min_fills else None
    if args.cost is not None:
        per_book = {k: args.cost for k in per_book}
    print("cost per side used:", per_book)
    if all(v is None for v in per_book.values()):
        print(f"fewer than {args.min_fills} fills per book — nothing re-run")
        return 0

    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        flows = insider_flows(store, (dt.date.fromisoformat(args.start) - dt.timedelta(days=200)).isoformat())
    books = {}
    if per_book["long"] is not None:
        sc = long_v2.daily_scores(market, fund, args.start, args.end)
        tg = long_v2.targets_from_scores(sc, TOP_N)
        for label, cost in (("half_spread", None), (f"measured_{per_book['long']:.2f}pct", per_book["long"])):
            books[f"long_{label}"] = simulate(market, tg, args.start, args.end, TOP_N, None, 0.5, cash_in_spy=True,
                                              fixed_cost_pct=cost).metrics
    if per_book["insider"] is not None:
        tg = short_term.targets(market, flows, args.start, args.end)
        for label, cost in (("half_spread", None), (f"measured_{per_book['insider']:.2f}pct", per_book["insider"])):
            books[f"insider_{label}"] = simulate(market, tg, args.start, args.end, short_term.MAX_POSITIONS,
                                                 short_term.HOLD_DAYS, 0.5, cash_in_spy=True, fixed_cost_pct=cost).metrics
    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"s27_exec_cost_{stamp}.json"), "w") as f:
        json.dump({"measured": df.to_dict("records") if not df.empty else [], "cost_per_side": per_book, "books": books},
                  f, indent=1, default=str)
    L = [f"# S27 — measured execution cost vs the backtest assumption ({stamp})", "",
         "```\n" + summary(df).to_string(index=False) + "\n```" if not df.empty else "", "",
         "| book | cost model | CAGR | alpha2/yr | alpha2 t | trades |", "|---|---|---|---|---|---|"]
    for k, m in books.items():
        book, label = k.split("_", 1)
        L.append(f"| {book} | {label} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['n_trades']} |")
    with open(os.path.join(OUT_DIR, f"s27_exec_cost_{stamp}.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
