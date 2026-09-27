"""Weekly attribution of the paper books: where the week's profit and loss came from.

For every lot and every day of the week the position's return is split into layers that add
up exactly (dollars, then as % of the book's equity at the start of the week):

  market     what the same dollars earned in SPY
  style      our pond vs SPY: the equal-weight return of the tradable universe minus SPY
  industry   the name's Fama-French 12 industry (equal weight inside the universe) minus the universe
  stock      the name minus its industry — the only layer that is stock selection
  execution  fill vs the day's open, on entry and exit days

Entry days are measured open → close, exit days previous close → open, other days close →
close, and every benchmark uses the same leg, so the layers stay comparable.

Factors: for each family (value, quality, momentum, low vol) and the composite, the rank
correlation between the score on the previous Friday and the week's return across the scored
universe, plus the top-30 minus universe spread. One week of this is noise; the file
out/agent/attribution.jsonl accumulates one row per week for the evaluation point.

Reading rule: a single week never justifies a rule change (docs/AGENT_PLAN.md S44). Observations
go to docs/HYPOTHESES.md and are tested on data that excludes the week that suggested them.

Usage: python -m agent.attribution [--end 2026-09-25]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent.books import long_v2_bundle as v2
from agent.books.data import fundamentals, load_market
from agent.books.industry import industry_by_ticker
from agent.books.long_term import ADV_FLOOR
from hedge_fund.features.panel import PanelStore

DB = "/Users/louis/optradar/optradar.db"
OUT_DIR = "/Users/louis/optradar/out/agent"
LAYERS = ["market", "style", "industry", "stock", "execution"]
MIN_GROUP = 10
FAMILIES = ["value", "quality", "momentum", "lowvol", "composite"]


def legs(market, store, days: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """cc / oc / co returns per ticker (rows = days), plus SPY's in column '__SPY__'."""
    adj, opn = market.adj, market.adj_open
    prev = adj.shift(1)
    out = {"cc": adj / prev - 1, "oc": adj / opn - 1, "co": opn / prev - 1}
    spy = store.con.execute("SELECT trade_date, open, close, adj_close FROM index_daily WHERE symbol = 'SPY' ORDER BY 1").df().set_index("trade_date")
    spy.index = pd.to_datetime(spy.index)
    spy = spy.reindex(adj.index)
    so = spy["open"] * spy["adj_close"] / spy["close"]
    sp = spy["adj_close"].shift(1)
    s = {"cc": spy["adj_close"] / sp - 1, "oc": spy["adj_close"] / so - 1, "co": so / sp - 1}
    return {k: v.loc[days].assign(__SPY__=s[k].loc[days]) for k, v in out.items()}


def attribute(lots: pd.DataFrame, market, store, groups: pd.Series, days: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per (day, lot) rows with the five layers in dollars; and per-day universe / group returns used."""
    L = legs(market, store, days)
    trad = market.tradable(ADV_FLOOR, np.inf)
    g = groups.reindex(market.adj.columns).fillna("Other")
    raw_open = market.adj_open * (market.close / market.adj)          # undo the adjustment: dollars are in raw prices
    rows = []
    for d in days:
        uni = trad.loc[d][trad.loc[d]].index
        bench = {}
        for leg in ("cc", "oc", "co"):
            r = L[leg].loc[d].reindex(uni).astype(float)
            r = r[np.isfinite(r)].clip(-0.9, 3.0)
            by_g = r.groupby(g.reindex(r.index)).agg(["mean", "count"])
            bench[leg] = (float(r.mean()), {k: (float(v["mean"]) if v["count"] >= MIN_GROUP else float(r.mean())) for k, v in by_g.iterrows()}, float(L[leg].at[d, "__SPY__"]))
        for lot in lots.itertuples():
            e, x = pd.Timestamp(lot.entry_day), (pd.Timestamp(lot.exit_day) if pd.notna(lot.exit_day) else None)
            if e > d or (x is not None and x < d) or lot.ticker not in market.adj.columns:
                continue
            if x is not None and x == d and e == d:
                continue
            leg = "oc" if e == d else "co" if (x is not None and x == d) else "cc"
            r_i = L[leg].at[d, lot.ticker]
            if not np.isfinite(r_i):
                continue
            o = raw_open.at[d, lot.ticker]
            p0 = o if leg == "oc" else market.close.shift(1).at[d, lot.ticker]
            v0 = float(lot.qty) * float(p0)
            r_u, r_g, r_s = bench[leg][0], bench[leg][1].get(g.get(lot.ticker, "Other"), bench[leg][0]), bench[leg][2]
            ex = 0.0
            if leg == "oc" and pd.notna(lot.entry_px):
                ex = float(lot.qty) * (float(o) - float(lot.entry_px))
            if leg == "co" and pd.notna(lot.exit_px):
                ex = float(lot.qty) * (float(lot.exit_px) - float(o))
            rows.append({"day": d, "book": lot.book, "ticker": lot.ticker, "industry": g.get(lot.ticker, "Other"), "leg": leg, "v0": v0,
                         "market": v0 * r_s, "style": v0 * (r_u - r_s), "industry_l": v0 * (r_g - r_u), "stock": v0 * (r_i - r_g), "execution": ex})
    df = pd.DataFrame(rows).rename(columns={"industry_l": "industry_layer"})
    return df


def factor_week(market, fund, prev_friday: pd.Timestamp, end: pd.Timestamp) -> dict:
    fs = v2.scored(market, fund, prev_friday)
    r = (market.adj.loc[end] / market.adj.loc[prev_friday] - 1).reindex(fs.index)
    ok = np.isfinite(r)
    out = {"n": int(ok.sum()), "universe_ew_pct": float(r[ok].mean() * 100), "top30_pct": float(r.reindex(fs.index[:30]).mean() * 100)}
    for f in FAMILIES:
        s = fs.loc[ok, f]
        m = s.notna()
        out[f] = {"rank_ic": float(s[m].rank().corr(r[ok][m].rank())) if m.sum() > 50 else None,
                  "top_decile_minus_universe_pct": float((r[ok][m][s[m] >= s[m].quantile(0.9)].mean() - r[ok][m].mean()) * 100) if m.sum() > 50 else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        market = load_market(store, (dt.date.today() - dt.timedelta(days=40)).isoformat())
        fund = fundamentals(store)
        groups = industry_by_ticker(store)
        idx = market.adj.index
        end = pd.Timestamp(args.end) if args.end else idx[-1]
        end = idx[idx <= end][-1]
        monday = end - pd.Timedelta(days=end.weekday())
        days = idx[(idx >= monday) & (idx <= end)]
        prev = idx[idx < monday][-1]
        con = duckdb.connect(DB, read_only=True)
        lots = con.execute("SELECT book, ticker, qty, entry_day, entry_px, exit_day, exit_px FROM agent_lots WHERE book IN ('long', 'insider')").df()
        nav = con.execute("SELECT as_of, book, equity_usd FROM agent_book_nav WHERE as_of BETWEEN ? AND ?", [prev.date(), end.date()]).df()
        con.close()
        df = attribute(lots, market, store, groups, days)
        factors = factor_week(market, fund, prev, end)
    out = {"week_end": str(end.date()), "week_start": str(days[0].date()), "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "spy_week_pct": float((market.spy.loc[end] / market.spy.loc[prev] - 1) * 100), "books": {}, "factors": factors}
    for book in ("long", "insider"):
        b = df[df["book"] == book] if len(df) else df
        n0 = nav[(nav["book"] == book)].sort_values("as_of")
        if b.empty or n0.empty:
            continue
        eq0, eq1 = float(n0["equity_usd"].iloc[0]), float(n0["equity_usd"].iloc[-1])
        lay = {k: float(b["industry_layer" if k == "industry" else k].sum()) for k in LAYERS}
        total = sum(lay.values())
        b = b.assign(pnl=b[["market", "style", "industry_layer", "stock", "execution"]].sum(axis=1))
        by_t = b.groupby("ticker").agg(industry=("industry", "first"), pnl=("pnl", "sum"), stock=("stock", "sum"), industry_layer=("industry_layer", "sum")).sort_values("pnl")
        by_i = b.groupby("industry").agg(pnl=("pnl", "sum"), industry_layer=("industry_layer", "sum"), stock=("stock", "sum"), n=("ticker", "nunique"),
                                         v0=("v0", "mean")).sort_values("pnl")
        neg = by_t[by_t["pnl"] < 0]["pnl"]
        out["books"][book] = {
            "equity_start": eq0, "equity_end": eq1, "nav_change_usd": eq1 - eq0, "nav_change_pct": (eq1 / eq0 - 1) * 100,
            "layers_usd": lay, "layers_pct": {k: v / eq0 * 100 for k, v in lay.items()}, "explained_usd": total, "unexplained_usd": (eq1 - eq0) - total,
            "n_names": int(by_t.shape[0]), "n_up": int((by_t["pnl"] > 0).sum()),
            "top1_share_of_losses": float(neg.min() / neg.sum()) if len(neg) else None, "top5_share_of_losses": float(neg.nsmallest(5).sum() / neg.sum()) if len(neg) else None,
            "worst": [{"ticker": t, "industry": r.industry, "pnl": float(r.pnl), "stock": float(r.stock)} for t, r in by_t.head(5).iterrows()],
            "best": [{"ticker": t, "industry": r.industry, "pnl": float(r.pnl), "stock": float(r.stock)} for t, r in by_t.tail(5)[::-1].iterrows()],
            "by_industry": [{"industry": i, "n": int(r.n), "pnl": float(r.pnl), "industry_layer": float(r.industry_layer), "stock": float(r.stock)} for i, r in by_i.iterrows()]}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"attribution_{out['week_end']}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    hist = os.path.join(OUT_DIR, "attribution.jsonl")
    rows = [json.loads(x) for x in open(hist)] if os.path.exists(hist) else []
    rows = [r for r in rows if r["week_end"] != out["week_end"]] + [out]
    with open(hist, "w") as f:
        for r in sorted(rows, key=lambda r: r["week_end"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for book, b in out["books"].items():
        print(f"{book} {out['week_start']}→{out['week_end']}: NAV {b['nav_change_pct']:+.2f}% = " + " ".join(f"{k} {v:+.2f}%" for k, v in b["layers_pct"].items())
              + f" | unexplained ${b['unexplained_usd']:+.0f}")
    print("factors:", {f: factors[f]["rank_ic"] for f in FAMILIES}, f"top30 {factors['top30_pct']:+.2f}% vs universe {factors['universe_ew_pct']:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
