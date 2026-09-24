"""S38 — short interest as a negative filter. Pre-registered 2026-09-24, before any run.

Literature: high short interest predicts low future returns (Asquith, Pathak & Ritter 2005;
Rapach, Ringgenberg & Zhou 2016 — aggregate), concentrated in names that are hard to borrow.
Data: FINRA consolidated short interest (agent/sources/finra_short.py), twice a month, from
2020-04; point-in-time date = settlement + 9 business days. SI ratio = short shares / shares
outstanding (latest filing on or before the public date, the same shares the books use).

Signals, fixed now:
  hi_si20   SI ratio >= 20%
  hi_dtc10  days to cover >= 10
  hi_decile top decile of SI ratio within the day's tradable universe (ADV >= $5M, mcap >= $100M)
Each holds from its public date until the next public date.

Tests (window 2020-06-01 → 2026-08-31):
  1. Cross-section: on each public date, mean abnormal return (vs SPY) from the next open over
     20 sessions of flagged names vs all others, clustered by date.
  2. As an entry veto on both live books (blocks entries only, like S33).
Adopt a veto for a book only if (a) the book's alpha2 improves by >= 0.5%/yr with NW t not
lower, AND (b) the vetoed names' abnormal return over the book's horizon (20 long, 5 insider)
has NW t <= -2. Otherwise record and do not adopt. Holm budget +6 (3 vetoes x 2 books).

Usage: python -m agent.s38_short_interest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import duckdb
import numpy as np
import pandas as pd

from agent.books import long_v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import simulate
from agent.books.factors import latest_before
from agent.books.long_term import ADV_FLOOR, TOP_N
from agent.events import LINES
from agent.s8_insider import event_stats
from agent.s33_negative_filters import direct_test, veto_sizes
from agent.sources.finra_short import SHORT_DB
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
HORIZON = {"long": 20, "insider": 5}


def flags(market, fund, start: str, end: str) -> dict[str, pd.DataFrame]:
    con = duckdb.connect(str(SHORT_DB), read_only=True)
    si = con.execute("SELECT ticker, public_date, short_qty, days_to_cover FROM short_interest WHERE public_date BETWEEN ? AND ?",
                     [pd.Timestamp(start) - pd.Timedelta(days=40), end]).df()
    con.close()
    si["public_date"] = pd.to_datetime(si["public_date"])
    idx, cols = market.adj.index, market.adj.columns
    out = {k: pd.DataFrame(False, index=idx, columns=cols) for k in ("hi_si20", "hi_dtc10", "hi_decile")}
    ratio_panel = pd.DataFrame(np.nan, index=idx, columns=cols)
    pubs = sorted(si["public_date"].unique())
    tradable = market.tradable(ADV_FLOOR, np.inf)
    for i, pdate in enumerate(pubs):
        day_pos = idx.searchsorted(pdate)
        if day_pos >= len(idx):
            continue
        d0 = idx[day_pos]
        d1 = idx[idx.searchsorted(pubs[i + 1])] if i + 1 < len(pubs) and idx.searchsorted(pubs[i + 1]) < len(idx) else idx[-1]
        g = si[si["public_date"] == pdate].drop_duplicates("ticker").set_index("ticker")
        g = g[g.index.isin(cols)]
        f = latest_before(fund, d0).reindex(g.index)
        ratio = (g["short_qty"] / f["shares"]).replace([np.inf, -np.inf], np.nan)
        ratio = ratio[(ratio >= 0) & (ratio <= 1.5)]                        # > 150% is a share-count error
        mcap = market.close.loc[d0].reindex(ratio.index) * f["shares"].reindex(ratio.index)
        uni = ratio[(mcap >= 1e8) & tradable.loc[d0].reindex(ratio.index).fillna(False)]
        cut = uni.quantile(0.9) if len(uni) > 50 else np.inf
        span = idx[(idx >= d0) & (idx < d1)] if d1 > d0 else idx[idx >= d0][:1]
        out["hi_si20"].loc[span, ratio[ratio >= 0.20].index] = True
        out["hi_dtc10"].loc[span, g[g["days_to_cover"] >= 10].index] = True
        out["hi_decile"].loc[span, uni[uni >= cut].index] = True
        ratio_panel.loc[d0, ratio.index] = ratio
    return out, si


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-06-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        ins = LINES["insider_buy"]
        ins_ev = ins.events(store, args.start, args.end)
    fl, si = flags(market, fund, args.start, args.end)
    print(f"flags built [{time.time() - t0:.0f}s]; flagged name-days: " + ", ".join(f"{k} {int(v.loc[args.start:args.end].sum().sum())}" for k, v in fl.items()), flush=True)

    # 1. cross-section on each public date
    pubs = sorted({d for d in pd.to_datetime(si["public_date"]).unique() if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)})
    idx = market.adj.index
    fwd = (market.adj.shift(-20) / market.adj_open.shift(-1) - 1) * 100
    mkt = (market.spy.shift(-20) / market.spy.shift(-1) - 1) * 100
    xs = {}
    tradable = market.tradable(ADV_FLOOR, np.inf)
    for k, f in fl.items():
        ev_f, ev_o = [], []
        for p in pubs:
            pos = idx.searchsorted(p)
            if pos >= len(idx):
                continue
            d = idx[pos]
            row, ok = f.loc[d], tradable.loc[d]
            ev_f += [(d, t) for t in row[row & ok].index]
            ev_o += [(d, t) for t in ok[ok & ~row].index]
        xs[k] = {"flagged": event_stats(pd.DataFrame(ev_f, columns=["date", "ticker"]), fwd, mkt, 20, f"{k} flagged h20"),
                 "others": event_stats(pd.DataFrame(ev_o, columns=["date", "ticker"]), fwd, mkt, 20, f"{k} others h20")}
    print(f"cross-section done [{time.time() - t0:.0f}s]", flush=True)

    # 2. vetoes on both books
    sc = long_v2.daily_scores(market, fund, args.start, args.end)
    long_tg = long_v2.targets_from_scores(sc, TOP_N)
    long_entry = [(d, t) for d, s in sc.items() for t in s.head(TOP_N).index]
    ins_tg = ins.targets(market, ins_ev, args.start, args.end)
    ins_pairs = [(d, t) for d, names in ins_tg.items() for t in names]
    books = {"long_base": simulate(market, long_tg, args.start, args.end, TOP_N, None, args.exec_frac, cash_in_spy=True).metrics,
             "insider_base": simulate(market, ins_tg, args.start, args.end, ins.spec.max_slots, ins.spec.hold_days, args.exec_frac, cash_in_spy=True).metrics}
    direct, verdict = {}, {}
    for k, veto in fl.items():
        sz, pairs = veto_sizes(long_tg, veto)
        books[f"long_{k}"] = simulate(market, long_tg, args.start, args.end, TOP_N, None, args.exec_frac, cash_in_spy=True, sizes=sz).metrics
        direct[f"long_{k}"] = direct_test(market, [p for p in pairs if p in set(long_entry)], long_entry, HORIZON["long"], f"long {k}")
        sz, pairs = veto_sizes(ins_tg, veto)
        books[f"insider_{k}"] = simulate(market, ins_tg, args.start, args.end, ins.spec.max_slots, ins.spec.hold_days, args.exec_frac, cash_in_spy=True, sizes=sz).metrics
        direct[f"insider_{k}"] = direct_test(market, pairs, ins_pairs, HORIZON["insider"], f"insider {k}")
        for b in ("long", "insider"):
            m, base, dv = books[f"{b}_{k}"], books[f"{b}_base"], direct[f"{b}_{k}"]["vetoed"]
            a = m["alpha2_ann_pct"] - base["alpha2_ann_pct"] >= 0.5 and m["alpha2_t_nw"] >= base["alpha2_t_nw"]
            c = "t_nw" in dv and dv["t_nw"] <= -2.0
            verdict[f"{b}_{k}"] = {"book_improves": bool(a), "vetoed_negative": bool(c), "adopt": bool(a and c)}
        print(f"  {k}: long {books[f'long_{k}']['alpha2_ann_pct']:+.1f}% insider {books[f'insider_{k}']['alpha2_ann_pct']:+.1f}% [{time.time() - t0:.0f}s]", flush=True)

    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s38_short_interest_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"start": args.start, "end": args.end, "cross_section": xs, "books": books, "direct": direct, "verdict": verdict}, f, indent=1, default=float)

    def row(s):
        if "mean_abn_pct" not in s:
            return f"| {s['label']} | — | — | {s['n_dates']} | too few | | | | |"
        return (f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['t_nw']:.2f} | "
                f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    L = [f"# S38 — short interest as a negative filter ({stamp})", "",
         f"FINRA consolidated short interest, {args.start} → {args.end}, point-in-time = settlement + 9 business days. SI ratio = short shares / shares outstanding.", "",
         "## Cross-section: abnormal return from the next open over 20 sessions", "",
         "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    for k, v in xs.items():
        L += [row(v["flagged"]), row(v["others"])]
    L += ["", "## Books (veto blocks entries only)", "", "| book | CAGR | alpha2/yr | alpha2 t | trades |", "|---|---|---|---|---|"]
    for k, m in books.items():
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['n_trades']} |")
    L += ["", "## Vetoed vs kept target names", "", "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    for k, d in direct.items():
        L += [row(d["vetoed"]), row(d["kept"])]
    L += ["", "## Verdict", "", "| variant | book improves | vetoed negative | adopt |", "|---|---|---|---|"]
    for k, v in verdict.items():
        L.append(f"| {k} | {'✓' if v['book_improves'] else ''} | {'✓' if v['vetoed_negative'] else ''} | {'**ADOPT**' if v['adopt'] else 'no'} |")
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
