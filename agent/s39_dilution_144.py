"""S39 — share offerings, shelf registrations and planned insider sales as entry vetoes.
Pre-registered 2026-09-24, before any run.

Evidence base: seasoned equity offerings underperform for months after pricing (Loughran & Ritter
1995; smaller but still negative post-2000); Form 144 is filed before the insider sale it
announces, so it is the earliest public sign of an insider selling. Both are the negative mirror
of the insider line's buy signal. NEOV this week (S-3 then a 27% drop) is the anecdote, not
evidence.

Data: agent/sources/sec_forms.py (EDGAR full-text search). A filing is public on its filing date
and usable from that day's close.

Flags (a target name on day D is vetoed if):
  off5     a 424B5 (priced offering) filed in the 5 sessions ending D
  shelf20  an S-3 / S-3/A / S-3ASR filed in the 20 sessions ending D
  f144_5   a Form 144 filed in the 5 sessions ending D (2023-06 onward; electronic filing began 2023-04)
Windows: off5 and shelf20 2017-01-01 → 2026-08-31; f144_5 2023-06-01 → 2026-08-31.

Also an event study: every 424B5 in the tradable universe, abnormal return from the next open at
h5 / h20 (not a line — we do not short).

Adopt a veto for a book only if (a) its alpha2 improves by >= 0.5%/yr with the NW t not lower and
(b) the vetoed names' abnormal return over the book's horizon (20 long, 5 insider) has NW t <= -2.
Otherwise record, do not adopt. Holm budget +6.

Usage: python -m agent.s39_dilution_144
"""
from __future__ import annotations

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
from agent.books.long_term import ADV_FLOOR, TOP_N
from agent.events import LINES
from agent.s8_insider import event_stats
from agent.s33_negative_filters import direct_test, veto_sizes
from agent.sources.sec_forms import FILINGS_DB
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
HORIZON = {"long": 20, "insider": 5}
FLAGS = {"off5": (["424B5"], 5, "2017-01-01"), "shelf20": (["S-3", "S-3/A", "S-3ASR"], 20, "2017-01-01"), "f144_5": (["144"], 5, "2023-06-01")}
END = "2026-08-31"


def flag_frame(market, forms: list[str], window: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    con = duckdb.connect(str(FILINGS_DB), read_only=True)
    f = con.execute(f"SELECT DISTINCT ticker, filed FROM sec_forms WHERE ticker IS NOT NULL AND form IN ({','.join('?' * len(forms))})", forms).df()
    con.close()
    f["filed"] = pd.to_datetime(f["filed"])
    idx, cols = market.adj.index, market.adj.columns
    hit = pd.DataFrame(False, index=idx, columns=cols)
    f = f[f["ticker"].isin(cols)]
    pos = idx.searchsorted(f["filed"].to_numpy())                     # the filing date's session (or the next one)
    ev = []
    for (t, d), p in zip(f[["ticker", "filed"]].itertuples(index=False), pos):
        if p < len(idx):
            hit.iat[p, cols.get_loc(t)] = True
            ev.append((idx[p], t))
    flag = hit.rolling(window, min_periods=1).max().astype(bool)
    return flag, pd.DataFrame(ev, columns=["date", "ticker"]).drop_duplicates()


def main() -> int:
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, "2017-01-01")
        fund = fundamentals(store)
        ins = LINES["insider_buy"]
        ins_ev = ins.events(store, "2017-01-01", END)
    tradable = market.tradable(ADV_FLOOR, np.inf)
    sc = long_v2.daily_scores(market, fund, "2017-01-01", END)
    long_tg_all = long_v2.targets_from_scores(sc, TOP_N)
    ins_tg_all = ins.targets(market, ins_ev, "2017-01-01", END)
    print(f"books scored [{time.time() - t0:.0f}s]", flush=True)

    books, direct, verdict, study = {}, {}, {}, {}
    for name, (forms, window, start) in FLAGS.items():
        flag, ev = flag_frame(market, forms, window)
        long_tg = {d: v for d, v in long_tg_all.items() if d >= pd.Timestamp(start)}
        ins_tg = {d: v for d, v in ins_tg_all.items() if d >= pd.Timestamp(start)}
        long_entry = [(d, t) for d, s in sc.items() if d >= pd.Timestamp(start) for t in s.head(TOP_N).index]
        ins_pairs = [(d, t) for d, names in ins_tg.items() for t in names]
        base_l = simulate(market, long_tg, start, END, TOP_N, None, 0.5, cash_in_spy=True).metrics
        base_i = simulate(market, ins_tg, start, END, ins.spec.max_slots, ins.spec.hold_days, 0.5, cash_in_spy=True).metrics
        books[f"long_base_{name}"], books[f"insider_base_{name}"] = base_l, base_i
        sz, pairs = veto_sizes(long_tg, flag)
        books[f"long_{name}"] = simulate(market, long_tg, start, END, TOP_N, None, 0.5, cash_in_spy=True, sizes=sz).metrics
        direct[f"long_{name}"] = direct_test(market, [p for p in pairs if p in set(long_entry)], long_entry, HORIZON["long"], f"long {name}")
        sz, pairs = veto_sizes(ins_tg, flag)
        books[f"insider_{name}"] = simulate(market, ins_tg, start, END, ins.spec.max_slots, ins.spec.hold_days, 0.5, cash_in_spy=True, sizes=sz).metrics
        direct[f"insider_{name}"] = direct_test(market, pairs, ins_pairs, HORIZON["insider"], f"insider {name}")
        for b, base in (("long", base_l), ("insider", base_i)):
            m, dv = books[f"{b}_{name}"], direct[f"{b}_{name}"]["vetoed"]
            a = m["alpha2_ann_pct"] - base["alpha2_ann_pct"] >= 0.5 and m["alpha2_t_nw"] >= base["alpha2_t_nw"]
            c = "t_nw" in dv and dv["t_nw"] <= -2.0
            verdict[f"{b}_{name}"] = {"book_improves": bool(a), "vetoed_negative": bool(c), "adopt": bool(a and c)}
        # event study over the tradable universe
        ev = ev[(ev["date"] >= pd.Timestamp(start)) & (ev["date"] <= pd.Timestamp(END))]
        ev = ev[[bool(tradable.at[d, t]) for d, t in zip(ev["date"], ev["ticker"])]]
        for h in (5, 20):
            fwd = (market.adj.shift(-h) / market.adj_open.shift(-1) - 1) * 100
            mkt = (market.spy.shift(-h) / market.spy.shift(-1) - 1) * 100
            study[f"{name}_h{h}"] = event_stats(ev, fwd, mkt, h, f"{name} all filings h{h}")
        print(f"  {name}: {len(ev)} tradable filings; long {books[f'long_{name}']['alpha2_ann_pct']:+.1f}% vs {base_l['alpha2_ann_pct']:+.1f}%, "
              f"insider {books[f'insider_{name}']['alpha2_ann_pct']:+.1f}% vs {base_i['alpha2_ann_pct']:+.1f}% [{time.time() - t0:.0f}s]", flush=True)

    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s39_dilution_144_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"flags": {k: {"forms": v[0], "window": v[1], "start": v[2]} for k, v in FLAGS.items()}, "end": END,
                   "books": books, "direct": direct, "verdict": verdict, "event_study": study}, f, indent=1, default=float)

    def row(s):
        if "mean_abn_pct" not in s:
            return f"| {s['label']} | — | — | {s['n_dates']} | too few | | | | |"
        return (f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['t_nw']:.2f} | "
                f"{s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    L = [f"# S39 — offerings, shelf registrations, Form 144 as entry vetoes ({stamp})", "",
         "## Event study, every filing in the tradable universe (abnormal vs SPY from the next open)", "",
         "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    L += [row(s) for s in study.values()]
    L += ["", "## Books (veto blocks entries only; each flag compared with the base over its own window)", "",
          "| book | CAGR | alpha2/yr | alpha2 t | trades |", "|---|---|---|---|---|"]
    for k, m in books.items():
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['n_trades']} |")
    L += ["", "## Vetoed vs kept target names", "", "| cut | h | events | dates | mean abn % | NW t | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|"]
    for d in direct.values():
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
