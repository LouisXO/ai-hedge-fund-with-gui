"""S33 — the two negative signals as entry vetoes on the live books.

Two findings with the right sign and Holm-level support are so far unused:
  S25  a large one-day move (|abn| >= max(5%, 3 sd), either direction, news or
       not) is followed by underperformance over 5-20 sessions
  S32  a bearish unusual-options-flow day (volume >= 3x 20-day mean, >= 500
       contracts, call share <= 30%) is followed by -0.4% / -0.6% over 10 / 20
       sessions (S&P names, 2024-02 onwards only)

This turns each into a veto: a name the book wants to ENTER is skipped while
the flag is fresh; a name already held is unaffected (the engine's size-0 rule:
keep if held, never enter). Exits are never triggered by a veto.

Variants, pre-registered 2026-09-23 before any run:
  jump1   veto if the target day itself was a big-move day
  jump5   veto if any big-move day in the last 5 sessions (target day included)
  bear5   veto if any bearish-flow day in the last 5 sessions
  both5   jump5 or bear5
applied to the long book (v1, 30/60) and the insider line (v1, 5-session hold).

Reading: a veto is adopted for a book only if BOTH
  (a) the book's alpha2 improves by >= 0.5%/yr over the unfiltered base with
      the NW t not lower, and
  (b) the direct test is negative: the vetoed target names' mean abnormal return
      from the next open over the book's horizon (20 sessions long, 5 insider)
      has NW t <= -2, clustered by date.
Anything else = not adopted, recorded, Holm budget +8 (4 vetoes x 2 books).

Usage: python -m agent.s33_negative_filters [--start 2017-01-01] [--end 2026-08-31]
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
from agent.books.data import Market, fundamentals, load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from agent.events import LINES
from agent.events.move_news import MIN_ABS_PCT, SD_WINDOW, SIGMA_MULT
from agent.s8_insider import event_stats
from agent.sources.alpaca_options import OPTIONS_DB
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
HORIZON = {"long": 20, "insider": 5}


def big_move_days(market: Market) -> pd.DataFrame:
    """Boolean days x tickers: S25's screen without the ADV band (a veto applies to any name)."""
    ret = market.adj.pct_change() * 100
    abn = ret.sub(market.spy.pct_change() * 100, axis=0)
    sd = ret.shift(1).rolling(SD_WINDOW, min_periods=40).std()
    z = abn / sd
    return ((z.abs() >= SIGMA_MULT) & (abn.abs() >= MIN_ABS_PCT)).fillna(False)


def bearish_flow_days(market: Market, ratio: float = 3.0, share: float = 0.7) -> pd.DataFrame:
    """Boolean days x tickers from S32's definition; False everywhere before 2024-02 (no data)."""
    con = duckdb.connect(str(OPTIONS_DB), read_only=True)
    vol = con.execute("""SELECT c.underlying, b.trade_date, sum(b.volume) AS v,
                                sum(CASE WHEN c.cp = 'C' THEN b.volume ELSE 0 END) AS vc
                         FROM opt_bars b JOIN opt_contracts c USING (symbol) GROUP BY 1, 2""").df()
    con.close()
    vol["trade_date"] = pd.to_datetime(vol["trade_date"])
    vol = vol.sort_values(["underlying", "trade_date"])
    vol["avg20"] = vol.groupby("underlying")["v"].transform(lambda s: s.shift(1).rolling(20, min_periods=15).mean())
    bear = vol[(vol["v"] / vol["avg20"] >= ratio) & (vol["v"] >= 500) & (vol["vc"] / vol["v"] <= 1 - share)]
    m = pd.DataFrame(False, index=market.adj.index, columns=market.adj.columns)
    for d, t in zip(bear["trade_date"], bear["underlying"]):
        if d in m.index and t in m.columns:
            m.at[d, t] = True
    return m


def fresh(flag: pd.DataFrame, window: int) -> pd.DataFrame:
    return flag.rolling(window, min_periods=1).max().astype(bool) if window > 1 else flag.astype(bool)


def veto_sizes(targets: dict, veto: pd.DataFrame) -> tuple[dict, list[tuple]]:
    """Engine `sizes`: 0 for vetoed names (never enter, keep if held). Also the vetoed (day, ticker) pairs."""
    sizes, pairs = {}, []
    for d, names in targets.items():
        if d not in veto.index:
            continue
        row = veto.loc[d]
        z = {t: 0.0 for t in names if t in row.index and bool(row[t])}
        if z:
            sizes[d] = z
            pairs += [(d, t) for t in z]
    return sizes, pairs


def direct_test(market: Market, pairs_vetoed: list[tuple], pairs_all: list[tuple], h: int, label: str) -> dict:
    fwd = (market.adj.shift(-h) / market.adj_open.shift(-1) - 1) * 100
    mkt = (market.spy.shift(-h) / market.spy.shift(-1) - 1) * 100
    v = pd.DataFrame(pairs_vetoed, columns=["date", "ticker"])
    vs = set(pairs_vetoed)
    k = pd.DataFrame([p for p in pairs_all if p not in vs], columns=["date", "ticker"])
    return {"vetoed": event_stats(v, fwd, mkt, h, f"{label} vetoed h{h}"),
            "kept": event_stats(k, fwd, mkt, h, f"{label} kept h{h}")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--exec-frac", type=float, default=0.5)
    args = ap.parse_args()
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, args.start)
        fund = fundamentals(store)
        ins_line = LINES["insider_buy"]
        ins_ev = ins_line.events(store, args.start, args.end)
    print(f"market {market.adj.shape} [{time.time() - t0:.0f}s]", flush=True)

    jump = big_move_days(market)
    bear = bearish_flow_days(market)
    print(f"big-move days {int(jump.loc[args.start:args.end].sum().sum())}, bearish-flow days {int(bear.sum().sum())} [{time.time() - t0:.0f}s]", flush=True)
    vetoes = {"jump1": fresh(jump, 1), "jump5": fresh(jump, 5), "bear5": fresh(bear, 5)}
    vetoes["both5"] = vetoes["jump5"] | vetoes["bear5"]

    scores = long_v2.daily_scores(market, fund, args.start, args.end)
    long_tg = long_v2.targets_from_scores(scores, TOP_N)
    long_entry_pairs = [(d, t) for d, s in scores.items() for t in s.head(TOP_N).index]      # the entry zone
    ins_tg = ins_line.targets(market, ins_ev, args.start, args.end)
    ins_pairs = [(d, t) for d, names in ins_tg.items() for t in names]
    print(f"scored long book, {len(long_tg)} days; insider {len(ins_pairs)} target names [{time.time() - t0:.0f}s]", flush=True)

    books, direct = {}, {}
    books["long_base"] = simulate(market, long_tg, args.start, args.end, TOP_N, None, args.exec_frac, cash_in_spy=True).metrics
    books["insider_base"] = simulate(market, ins_tg, args.start, args.end, ins_line.spec.max_slots, ins_line.spec.hold_days,
                                     args.exec_frac, cash_in_spy=True).metrics
    for name, veto in vetoes.items():
        sz, pairs = veto_sizes(long_tg, veto)
        books[f"long_{name}"] = simulate(market, long_tg, args.start, args.end, TOP_N, None, args.exec_frac, cash_in_spy=True, sizes=sz).metrics
        books[f"long_{name}"]["n_vetoed_pairs"] = len(pairs)
        entry_vetoed = [p for p in pairs if p in set(long_entry_pairs)]
        direct[f"long_{name}"] = direct_test(market, entry_vetoed, long_entry_pairs, HORIZON["long"], f"long {name}")
        sz, pairs = veto_sizes(ins_tg, veto)
        books[f"insider_{name}"] = simulate(market, ins_tg, args.start, args.end, ins_line.spec.max_slots, ins_line.spec.hold_days,
                                            args.exec_frac, cash_in_spy=True, sizes=sz).metrics
        books[f"insider_{name}"]["n_vetoed_pairs"] = len(pairs)
        direct[f"insider_{name}"] = direct_test(market, pairs, ins_pairs, HORIZON["insider"], f"insider {name}")
        print(f"  {name}: long alpha2 {books[f'long_{name}']['alpha2_ann_pct']:+.1f}% (t {books[f'long_{name}']['alpha2_t_nw']:.2f}), "
              f"insider {books[f'insider_{name}']['alpha2_ann_pct']:+.1f}% (t {books[f'insider_{name}']['alpha2_t_nw']:.2f}) [{time.time() - t0:.0f}s]", flush=True)

    verdict = {}
    for book in ("long", "insider"):
        base = books[f"{book}_base"]
        for name in vetoes:
            m, dt_ = books[f"{book}_{name}"], direct[f"{book}_{name}"]["vetoed"]
            a = m["alpha2_ann_pct"] - base["alpha2_ann_pct"] >= 0.5 and m["alpha2_t_nw"] >= base["alpha2_t_nw"]
            b = "t_nw" in dt_ and dt_["t_nw"] <= -2.0
            verdict[f"{book}_{name}"] = {"book_improves": bool(a), "vetoed_negative": bool(b), "adopt": bool(a and b)}

    stamp = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"s33_negative_filters_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start, "end": args.end,
                   "books": books, "direct": direct, "verdict": verdict}, f, indent=1, default=float)
    L = [f"# S33 — negative signals as entry vetoes ({stamp})", "",
         f"{args.start} → {args.end}, half spread per side, next-open entry, idle in SPY. A veto blocks entries only; held names are unaffected. "
         "Bearish-flow data exists from 2024-02 (S&P names), so bear5 is a no-op before that.", "",
         "## Books", "", "| book | CAGR | alpha2/yr | alpha2 t | β mkt | β size | MaxDD | trades | hit | vetoed pairs |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, m in books.items():
        L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {m['beta_mkt']:.2f} | {m['beta_size']:.2f} | "
                 f"{m['max_drawdown_pct']:.0f}% | {m['n_trades']} | {m['trade_hit_rate']:.0%} | {m.get('n_vetoed_pairs', '')} |")
    L += ["", "## Direct test — vetoed vs kept target names, abnormal return from the next open", "",
          "| cut | h | events | dates | mean abn % | NW t | hit | years>0 | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, d in direct.items():
        for side in ("vetoed", "kept"):
            s = d[side]
            if "mean_abn_pct" not in s:
                L.append(f"| {s['label']} | — | — | {s['n_dates']} | too few | | | | | |")
                continue
            L.append(f"| {s['label']} | {s['horizon']} | {s['n_events']} | {s['n_dates']} | {s['mean_abn_pct']:+.2f} | {s['t_nw']:.2f} | "
                     f"{s['hit_rate']:.0%} | {s['share_years_positive']:.0%} | {s['first_half']:+.2f} | {s['second_half']:+.2f} |")
    L += ["", "## Verdict (pre-registered rule: adopt only if the book improves >= 0.5%/yr with t not lower AND vetoed names have NW t <= -2)", "",
          "| variant | book improves | vetoed negative | adopt |", "|---|---|---|---|"]
    for k, v in verdict.items():
        L.append(f"| {k} | {'✓' if v['book_improves'] else ''} | {'✓' if v['vetoed_negative'] else ''} | {'**ADOPT**' if v['adopt'] else 'no'} |")
    text = "\n".join(L) + "\n"
    with open(base + ".md", "w") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
