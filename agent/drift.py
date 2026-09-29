"""Drift monitor: is the paper long book doing what the backtest's rules say, inside the backtest's range?

A gap between paper and backtest has three possible sources, and only the last is about the
strategy: (1) an implementation error, (2) execution, (3) the rules not working. This checks
(1) and (2) every day so that they are found in a day, not at the evaluation point
(2026-09-22/23: OPG orders expired for two days before anyone looked).

  holdings    the engine replayed on the recorded v1 lists from the paper start vs the lots the
              paper book actually holds
  nav         that replay's NAV vs the paper book's auction-basis NAV
  lists       the v1 list recomputed today for recent days vs the list recorded on the day
              (data revisions: a list that changes after the fact is not point-in-time)
  fills       share of the last 5 sessions' orders that filled, per book (S48)
  exec gap    mean fill vs opening cross, per book, against the 0.6%/side the simulator basis assumes
  insider     the insider book's orders on the last evening vs the names the rule gives for that
              evening (execute.insider_targets' rule rebuilt from the orders placed before it; S48)
  week        the book's last 5-session return inside the backtest's distribution of 5-session
              returns (site-data/validation/s47_base_nav.csv); outside the central 95% is flagged —
              which by construction happens one week in twenty, so it is a note, not an alarm

The replay and the backtest are total return (adjusted prices), so the paper side is the auction
basis plus the dividends the lots were entitled to (agent_auction_nav.equity_auction_tr, S48).
The replay's NAV goes into drift.json ("replay") every day, for agent/evaluate.py.

Output: out/agent/drift.json, shown by agent.health. Usage: python -m agent.drift [--db PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent.books import live as long_live
from agent.books.data import load_market
from agent.books.engine import simulate
from agent.books.long_term import TOP_N
from hedge_fund.features.panel import PanelStore

DB = "/Users/louis/optradar/optradar.db"
OUT = "/Users/louis/optradar/out/agent/drift.json"
NAV_REF = "/Users/louis/hedge-fund/site-data/validation/s47_base_nav.csv"   # the long book restated on the corrected data (S47)
AUCTION = "/Users/louis/optradar/out/agent/auction_basis.json"
PAPER_START = "2026-09-21"           # the first traded list; fills at the 09-22 open
BOOK_ZH = {"long": "长线", "insider": "内部人", "core": "SPY 核心"}
FINAL = ["filled", "expired", "canceled", "rejected"]


def chk(name: str, level: str, detail: str, **kw) -> dict:
    return {"name": name, "level": level, "detail": detail, **kw}


def fill_checks(orders: pd.DataFrame) -> list[dict]:
    """Share of the last 5 order days' final orders that filled, one check per book (S48: no longer pooled)."""
    out = []
    o5 = orders[orders["status"].isin(FINAL) | orders["filled"]]
    for book in [b for b in BOOK_ZH if b in set(o5["book"])] + sorted(set(o5["book"]) - set(BOOK_ZH)):
        g = o5[o5["book"] == book]
        days = sorted(g["as_of"].unique())[-5:]
        g = g[g["as_of"].isin(days)]
        rate = float(g["filled"].mean())
        by = g.groupby("as_of")["filled"].agg(["sum", "count"])
        lastrow = by.iloc[-1]
        out.append(chk(f"成交率 · {BOOK_ZH.get(book, book)}(最近 5 个下单日)",
                       "ok" if lastrow["sum"] == lastrow["count"] else "warn" if lastrow["sum"] / lastrow["count"] >= 0.5 else "bad",
                       f"5 日合计 {int(g['filled'].sum())}/{len(g)}({rate:.0%});最近一天 {int(lastrow['sum'])}/{int(lastrow['count'])}",
                       book=book, rate=rate))
    return out


def gap_checks(fill_rows: list[dict]) -> list[dict]:
    """Fill vs opening cross per book. auction_basis.gap_pct is already signed (positive = worse for either side)."""
    out = []
    fr = pd.DataFrame(fill_rows)
    if fr.empty or "gap_pct" not in fr:
        return out
    fr = fr[fr["gap_pct"].notna()]
    for book in [b for b in BOOK_ZH if b in set(fr["book"])]:
        g = fr[fr["book"] == book].sort_values("day")
        m, m10 = float(g["gap_pct"].mean()), float(g.tail(10)["gap_pct"].mean())
        sides = ";".join(f"{'买' if s == 'buy' else '卖'} {len(x)} 笔 {x['gap_pct'].mean():+.2f}%" for s, x in g.groupby("side"))
        out.append(chk(f"执行偏差 · {BOOK_ZH[book]}(成交价 对 开盘竞价)", "ok" if m10 <= 0.6 else "warn" if m10 <= 1.2 else "bad",
                       f"自 {g['day'].iloc[0]} 起 {len(g)} 笔平均 {m:+.2f}%/边({sides}),最近 10 笔 {m10:+.2f}%/边;模拟器口径假设 0.6%",
                       book=book, mean_pct=m, last10_pct=m10))
    return out


def insider_vs_rule(ordered: set[str], rule_names: list[str], free_slots: int, day: str) -> dict:
    """The insider book's buy orders of one evening vs the rule's names for it (in the rule's order).

    ordered but not given by the rule: an implementation error (bad). Given by the rule, inside the
    free slots, and not ordered: warn, not bad; a Form 4 loaded after that evening's run (the 06:00 job)
    shows up in the recomputation although the run could not have seen it, and cash can run out first.
    """
    extra = sorted(ordered - set(rule_names))
    missing = [t for t in rule_names[:max(free_slots, 0)] if t not in ordered]
    level = "bad" if extra else "warn" if missing else "ok"
    detail = (f"{day} 晚:规则给出 {len(rule_names)} 个,空槽位 {max(free_slots, 0)},实际下单 {len(ordered)}"
              + (f";下了单但规则没有:{', '.join(extra)}" if extra else "")
              + (f";规则有、有空位却没下单:{', '.join(missing)}(可能是当晚之后才入库的申报,或现金不足)" if missing else ""))
    return chk("内部人书:当晚订单 对 规则候选", level, detail, day=day, extra=extra, missing=missing)


def insider_check(con, store, since: dt.date | None) -> dict | None:
    """Rebuild the insider rule's names for the last order evening (from evaluate_from) and compare them with the orders."""
    from agent import signals_insider
    from agent.execute import BOOKS, blocking_orders
    last = con.execute("SELECT max(as_of) FROM agent_orders WHERE dry_run = FALSE AND as_of >= ?", [since or PAPER_START]).fetchone()[0]
    if last is None:
        return None
    day = pd.Timestamp(last)
    prior = con.execute("""SELECT ticker, status, filled_qty FROM agent_orders WHERE book = 'insider' AND side = 'buy' AND dry_run = FALSE
                           AND as_of >= ? AND as_of < ?""", [(day - pd.Timedelta(days=8)).date(), day.date()]).fetchall()
    skip, retry = blocking_orders(prior)
    cand = signals_insider.candidates(store, day, 2, None, longer={t: 3 for t in retry})
    names = [] if cand.empty else list(cand[cand["eligible"] & ~cand["ticker"].isin(skip)].sort_values("buy_usd", ascending=False)["ticker"])
    try:                                                     # as main() does: class shares are not entered (S47 addendum)
        from agent.execute import entry_candidates
        names = entry_candidates(names)
    except ImportError:
        pass
    others = {r[0] for r in con.execute("""SELECT ticker FROM agent_lots WHERE book <> 'insider' AND entry_day <= ?
                                           AND (exit_day IS NULL OR exit_day > ?)""", [day.date(), day.date()]).fetchall()}
    names = [t for t in names if t not in others]            # a name another book holds is not entered (plan_book)
    ordered = {r[0] for r in con.execute("""SELECT ticker FROM agent_orders WHERE book = 'insider' AND side = 'buy' AND dry_run = FALSE
                                            AND as_of = ?""", [day.date()]).fetchall()}
    held = con.execute("""SELECT count(*) FROM agent_lots WHERE book = 'insider' AND entry_day <= ?
                          AND (exit_day IS NULL OR exit_day > ?)""", [day.date(), day.date()]).fetchone()[0]
    sells = con.execute("""SELECT count(*) FROM agent_orders WHERE book = 'insider' AND side = 'sell' AND dry_run = FALSE AND as_of = ?""",
                        [day.date()]).fetchone()[0]
    return insider_vs_rule(ordered, names, int(BOOKS["insider"]["max_positions"]) - int(held) + int(sells), str(day.date()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB, help="the ledger (a copy, for a test run)")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--auction", default=AUCTION)
    args = ap.parse_args(argv)
    out = []
    con = duckdb.connect(args.db, read_only=True)
    picks = con.execute("SELECT as_of, ticker, rank FROM agent_picks WHERE signal_name = ? AND as_of >= ? ORDER BY as_of, rank", [long_live.SIGNAL, PAPER_START]).df()
    lots = con.execute("SELECT ticker FROM agent_lots WHERE book = 'long' AND status = 'open'").df()["ticker"].tolist()
    orders = con.execute("""SELECT as_of, book, coalesce(filled_qty, 0) > 0 AS filled, status FROM agent_orders
                            WHERE dry_run = FALSE AND as_of >= current_date - 9""").df()
    auct = con.execute("SELECT * FROM agent_auction_nav WHERE book = 'long' ORDER BY as_of").df()
    capital = float(con.execute("SELECT alloc_usd FROM agent_books WHERE book = 'long'").fetchone()[0])   # S48: not a constant
    if "equity_auction_tr" not in auct:                                  # before the S48 columns: price NAV only
        auct["equity_auction_tr"] = auct["equity_auction"]
    auct["equity_auction_tr"] = auct["equity_auction_tr"].fillna(auct["equity_auction"])
    lists = {pd.Timestamp(d): g["ticker"].tolist() for d, g in picks.groupby("as_of")}
    with PanelStore(read_only=True) as store:
        market = load_market(store, PAPER_START)
        last = market.adj.index[-1]
        # 1) + 2) replay
        from agent.s44_v2_bundle import entry_zone
        res = simulate(market, lists, PAPER_START, str(last.date()), TOP_N, None, 0.0, capital=capital, fixed_cost_pct=0.0,
                       sizes=entry_zone(lists, TOP_N))
        replay = {"long": {"start": PAPER_START, "capital": capital, "basis": "规则重放,复权价(含分红),开盘入场,零成本",
                           "days": [str(d.date()) for d in res.nav.index], "nav": [round(float(x), 2) for x in res.nav]}}
        # the engine does not return open positions, so rebuild them from the rule: enter from the top N, keep inside the top 2N
        held = set()
        prev = None
        for d in market.adj.loc[PAPER_START:].index:
            if prev is not None and prev in lists:
                want = set(lists[prev])
                held = {t for t in held if t in want}
                for t in lists[prev][:TOP_N]:
                    if len(held) >= TOP_N:
                        break
                    if t not in held and t in market.adj_open.columns and pd.notna(market.adj_open.at[d, t]):
                        held.add(t)
            prev = d
        paper = set(lots)
        miss, extra = sorted(held - paper), sorted(paper - held)
        n_diff = len(miss) + len(extra)
        out.append(chk("持仓:模拟盘 对 规则重放", "ok" if n_diff == 0 else "warn" if n_diff <= 4 else "bad",
                       f"一致 {len(held & paper)}/{len(held)}" + (f";规则该有而没有:{', '.join(miss)}" if miss else "") + (f";多出来的:{', '.join(extra)}" if extra else ""),
                       missing=miss, extra=extra))
        if len(auct):
            a = auct.set_index(pd.to_datetime(auct["as_of"]))["equity_auction_tr"]
            d0 = a.index[-1]
            if d0 in res.nav.index:
                gap = (float(a.iloc[-1]) / float(res.nav.loc[d0]) - 1) * 100
                out.append(chk("净值:竞价口径含分红 对 规则重放", "ok" if abs(gap) <= 0.5 else "warn" if abs(gap) <= 1.5 else "bad",
                               f"模拟盘 ${a.iloc[-1]:,.0f},重放 ${res.nav.loc[d0]:,.0f},差 {gap:+.2f}%(两边都含分红)", gap_pct=gap))
        # 3) list revisions over the last 5 recorded days — only lists recorded on the current data definition:
        #    lists before the S47 correction (evaluate_from) differ from a recomputation by design
        try:
            import yaml
            since = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml"))).get("evaluate_from")
        except Exception:
            since = None
        comparable = [d for d in sorted(lists) if since is None or d.date() >= pd.Timestamp(str(since)).date()]
        ov30, ov60, worst = [], [], None
        for d in comparable[-5:]:
            if d not in market.adj.index:
                continue
            now = [r["ticker"] for r in long_live.targets(store, market, d, TOP_N)]
            rec = lists[d]
            a30, a60 = len(set(now[:30]) & set(rec[:30])), len(set(now) & set(rec))
            ov30.append(a30)
            ov60.append(a60)
            if worst is None or a30 < worst[1]:
                worst = (str(d.date()), a30)
        if ov30:
            lo = min(ov30)
            out.append(chk("名单:今天重算 对 当天记录", "ok" if lo >= 28 else "warn" if lo >= 25 else "bad",
                           f"前 30 重合最低 {lo}/30({worst[0]}),前 60 平均 {np.mean(ov60):.0f}/60;不一致来自事后修订的数据", min_top30=lo))
        # insider book: that evening's orders vs the rule (S48; drift covered the long book only)
        try:
            con = duckdb.connect(args.db, read_only=True)
            try:
                c = insider_check(con, store, pd.Timestamp(str(since)).date() if since else None)
            finally:
                con.close()
            if c:
                out.append(c)
        except Exception as exc:                                     # never let this check stop the others
            out.append(chk("内部人书:当晚订单 对 规则候选", "warn", f"没算出来:{exc}"))
    # 4) fills, per book
    out += fill_checks(orders)
    # 5) execution gap, per book
    try:
        out += gap_checks(json.load(open(args.auction)).get("fill_rows", []))
    except Exception:
        pass
    # 6) the week inside the backtest's distribution (both total return)
    if os.path.exists(NAV_REF) and len(auct) >= 6:
        ref = pd.read_csv(NAV_REF, index_col=0, parse_dates=True)["v1c"]
        r5 = (ref / ref.shift(5) - 1).dropna() * 100
        a = auct["equity_auction_tr"].to_numpy(dtype=float)
        mine = (a[-1] / a[-6] - 1) * 100
        pct = float((r5 < mine).mean() * 100)
        out.append(chk("最近 5 日收益 在 回测分布里的位置", "ok" if 2.5 <= pct <= 97.5 else "warn",
                       f"{mine:+.2f}%,回测第 {pct:.0f} 百分位(回测 5 日收益中位数 {r5.median():+.2f}%,2.5%–97.5% 区间 {r5.quantile(.025):+.1f}% 到 {r5.quantile(.975):+.1f}%)",
                       ret5_pct=mine, percentile=pct))
    rep = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "checks": out, "replay": replay,
           "level": "bad" if any(c["level"] == "bad" for c in out) else "warn" if any(c["level"] == "warn" for c in out) else "ok"}
    with open(args.out, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=float)
    for c in out:
        print(f"[{c['level']:4s}] {c['name']}: {c['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
