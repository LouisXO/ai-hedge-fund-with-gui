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
  fills       share of the last 5 sessions' orders that filled
  exec gap    mean fill vs opening cross, against the 0.6%/side the simulator basis assumes
  week        the book's last 5-session return inside the backtest's distribution of 5-session
              returns (site-data/validation/s44_nav.csv); outside the central 95% is flagged —
              which by construction happens one week in twenty, so it is a note, not an alarm

Output: out/agent/drift.json, shown by agent.health. Usage: python -m agent.drift
"""
from __future__ import annotations

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
NAV_REF = "/Users/louis/hedge-fund/site-data/validation/s44_nav.csv"
PAPER_START = "2026-09-21"           # the first traded list; fills at the 09-22 open
CAPITAL = 60_000.0


def chk(name: str, level: str, detail: str, **kw) -> dict:
    return {"name": name, "level": level, "detail": detail, **kw}


def main() -> int:
    out = []
    con = duckdb.connect(DB, read_only=True)
    picks = con.execute("SELECT as_of, ticker, rank FROM agent_picks WHERE signal_name = ? AND as_of >= ? ORDER BY as_of, rank", [long_live.SIGNAL, PAPER_START]).df()
    lots = con.execute("SELECT ticker FROM agent_lots WHERE book = 'long' AND status = 'open'").df()["ticker"].tolist()
    orders = con.execute("""SELECT as_of, book, coalesce(filled_qty, 0) > 0 AS filled, status FROM agent_orders
                            WHERE dry_run = FALSE AND as_of >= current_date - 9""").df()
    auct = con.execute("SELECT as_of, equity_auction FROM agent_auction_nav WHERE book = 'long' ORDER BY as_of").df()
    con.close()
    lists = {pd.Timestamp(d): g["ticker"].tolist() for d, g in picks.groupby("as_of")}
    with PanelStore(read_only=True) as store:
        market = load_market(store, PAPER_START)
        last = market.adj.index[-1]
        # 1) + 2) replay
        from agent.s44_v2_bundle import entry_zone
        res = simulate(market, lists, PAPER_START, str(last.date()), TOP_N, None, 0.0, capital=CAPITAL, fixed_cost_pct=0.0,
                       sizes=entry_zone(lists, TOP_N))
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
            a = auct.set_index(pd.to_datetime(auct["as_of"]))["equity_auction"]
            d0 = a.index[-1]
            if d0 in res.nav.index:
                gap = (float(a.iloc[-1]) / float(res.nav.loc[d0]) - 1) * 100
                out.append(chk("净值:竞价口径 对 规则重放", "ok" if abs(gap) <= 0.5 else "warn" if abs(gap) <= 1.5 else "bad",
                               f"模拟盘 ${a.iloc[-1]:,.0f},重放 ${res.nav.loc[d0]:,.0f},差 {gap:+.2f}%", gap_pct=gap))
        # 3) list revisions over the last 5 recorded days
        ov30, ov60, worst = [], [], None
        for d in sorted(lists)[-5:]:
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
    # 4) fills
    o5 = orders[orders["status"].isin(["filled", "expired", "canceled", "rejected"]) | orders["filled"]]
    if len(o5):
        days = sorted(o5["as_of"].unique())[-5:]
        o5 = o5[o5["as_of"].isin(days)]
        rate = float(o5["filled"].mean())
        by = o5.groupby("as_of")["filled"].agg(["sum", "count"])
        lastrow = by.iloc[-1]
        out.append(chk("成交率(最近 5 个交易日)", "ok" if lastrow["sum"] == lastrow["count"] else "warn" if lastrow["sum"] / lastrow["count"] >= 0.5 else "bad",
                       f"5 日合计 {int(o5['filled'].sum())}/{len(o5)}({rate:.0%});最近一天 {int(lastrow['sum'])}/{int(lastrow['count'])}", rate=rate))
    # 5) execution gap
    try:
        ab = json.load(open("/Users/louis/optradar/out/agent/auction_basis.json"))
        fr = pd.DataFrame(ab.get("fill_rows", []))
        if len(fr):
            fr["signed"] = fr["gap_pct"] * np.where(fr["side"] == "buy", 1, -1)
            m, m10 = float(fr["signed"].mean()), float(fr.sort_values("day").tail(10)["signed"].mean())
            out.append(chk("执行偏差(成交价 对 开盘竞价)", "ok" if m10 <= 0.6 else "warn" if m10 <= 1.2 else "bad",
                           f"全部 {len(fr)} 笔平均 {m:+.2f}%/边,最近 10 笔 {m10:+.2f}%/边;模拟器口径假设 0.6%", mean_pct=m, last10_pct=m10))
    except Exception:
        pass
    # 6) the week inside the backtest's distribution
    if os.path.exists(NAV_REF) and len(auct) >= 6:
        ref = pd.read_csv(NAV_REF, index_col=0, parse_dates=True)["v1c"]
        r5 = (ref / ref.shift(5) - 1).dropna() * 100
        a = auct["equity_auction"].to_numpy(dtype=float)
        mine = (a[-1] / a[-6] - 1) * 100
        pct = float((r5 < mine).mean() * 100)
        out.append(chk("最近 5 日收益 在 回测分布里的位置", "ok" if 2.5 <= pct <= 97.5 else "warn",
                       f"{mine:+.2f}%,回测第 {pct:.0f} 百分位(回测 5 日收益中位数 {r5.median():+.2f}%,2.5%–97.5% 区间 {r5.quantile(.025):+.1f}% 到 {r5.quantile(.975):+.1f}%)",
                       ret5_pct=mine, percentile=pct))
    rep = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "checks": out,
           "level": "bad" if any(c["level"] == "bad" for c in out) else "warn" if any(c["level"] == "warn" for c in out) else "ok"}
    with open(OUT, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=float)
    for c in out:
        print(f"[{c['level']:4s}] {c['name']}: {c['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
