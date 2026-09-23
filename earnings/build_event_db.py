#!/usr/bin/env python
"""建全市场财报事件库 — 大样本统计的地基。

为什么值得做:单票 10 次财报的样本量下,任何模型都提取不出稳定信号
(2026-09-13 的实验已经证明)。而 get_financials_earnings_price_history
**不消耗历史K线额度**,所以样本规模只受时间限制,不受配额限制。

两段式:
  universe  从 earnings_calendar 扫出候选池(市值/期权活跃度过滤)
  events    对每只票拉财报前后 ±5 日的价格+IV/HV,再按财报周批量补 EPS 意外

可断点续跑:已入库的 ticker 直接跳过。
Usage:
  python masters/build_event_db.py universe --weeks 8 --min-cap 2e9
  python masters/build_event_db.py events [--limit 100] [--workers 3]
  python masters/build_event_db.py stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import duckdb
import moomoo as mm
from moomoo import RET_OK, OpenQuoteContext

from integrations.moomoo_client import MoomooDataClient, MoomooError, _f, _items, _num, _surprise

DB = os.path.join(ROOT, "site-data", "events.db")

DDL = [
    """CREATE TABLE IF NOT EXISTS universe (
        ticker VARCHAR PRIMARY KEY, name VARCHAR,
        market_cap DOUBLE, option_volume BIGINT, seen_date DATE)""",
    """CREATE TABLE IF NOT EXISTS earnings_events (
        ticker VARCHAR, period VARCHAR, filing_date DATE, filing_window VARCHAR,
        iv_pre DOUBLE, hv_pre DOUBLE, iv_crush DOUBLE,
        move_d0 DOUBLE, move_d1 DOUBLE, move_d3 DOUBLE, move_d4 DOUBLE, move_d5 DOUBLE,
        eps_actual DOUBLE, eps_est DOUBLE, eps_surprise VARCHAR,
        rev_actual DOUBLE, rev_est DOUBLE, rev_surprise VARCHAR,
        PRIMARY KEY (ticker, period))""",
    """CREATE TABLE IF NOT EXISTS fetch_log (
        ticker VARCHAR PRIMARY KEY, n_events INT, status VARCHAR, ts TIMESTAMP)""",
]


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = duckdb.connect(DB)
    for d in DDL:
        con.execute(d)
    return con


def cmd_universe(args, con) -> int:
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    today = dt.date.today()
    rows, seen = [], set()
    try:
        for w in range(args.weeks):
            b = today - dt.timedelta(days=7 * w + 6)
            e = today - dt.timedelta(days=7 * w)
            ret, d = ctx.get_earnings_calendar(mm.Market.US,
                                               begin_date=b.isoformat(),
                                               end_date=e.isoformat())
            if ret != RET_OK:
                print(f"  {b}: {str(d)[:60]}", flush=True)
                continue
            for r in d.to_dict("records"):
                t = r.get("security")
                cap = _num(r.get("market_cap")) or 0
                vol = _num(r.get("option_volume")) or 0
                if not t or t in seen or cap < args.min_cap or vol < args.min_opt_vol:
                    continue
                seen.add(t)
                rows.append((t, str(r.get("name") or "")[:80], cap, int(vol), e))
            print(f"  {b}~{e}: 累计候选 {len(rows)}", flush=True)
            time.sleep(1.5)
    finally:
        ctx.close()
    con.executemany("INSERT OR REPLACE INTO universe VALUES (?,?,?,?,?)", rows)
    print(f"\n✅ 候选池 {len(rows)} 只 → {DB}")
    return 0


def cmd_events(args, con) -> int:
    # Incremental refresh: skipping every ticker already in fetch_log would make
    # a weekly run a no-op — those tickers keep reporting. A ticker is re-fetched
    # when its newest stored event is older than --stale-days (a new print has
    # almost certainly landed since), and always on a full rebuild.
    done = {r[0] for r in con.execute("""
        SELECT f.ticker FROM fetch_log f
        LEFT JOIN (SELECT ticker, max(filing_date) mx FROM earnings_events GROUP BY 1) e
               ON e.ticker = replace(f.ticker, 'US.', '')
        WHERE f.status = 'ok'
          AND (? = 0 OR e.mx IS NULL OR e.mx > current_date - INTERVAL (?) DAY)
    """, [args.stale_days, args.stale_days]).fetchall()}
    todo = [r[0] for r in con.execute(
        "SELECT ticker FROM universe ORDER BY market_cap DESC").fetchall() if r[0] not in done]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("没有待抓取的标的(全部已入库)")
        return 0
    print(f"待抓 {len(todo)} 只(已完成 {len(done)})", flush=True)

    client = MoomooDataClient()
    t0, ok, fail, total_ev = time.time(), 0, 0, 0
    try:
        for i, ticker in enumerate(todo, 1):
            bare = ticker.split(".")[-1]
            try:
                evs = client.earnings_events(bare, limit=args.per_ticker, fast=args.fast)
            except (MoomooError, Exception) as exc:
                con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,now())",
                            [ticker, 0, f"err:{str(exc)[:80]}"])
                fail += 1
                if fail <= 5:
                    print(f"  [{i}] {bare}: {str(exc)[:70]}", flush=True)
                continue
            rows = [(bare, e.get("period"), e.get("filed"), str(e.get("window") or ""),
                     e.get("iv_pre"), e.get("hv_pre"), e.get("iv_crush"),
                     e.get("move_d0"), e.get("move_d1"), e.get("move_d3"),
                     e.get("move_d4"), e.get("move_d5"),
                     e.get("eps_actual"), e.get("eps_est"), e.get("eps_surprise"),
                     e.get("rev_actual"), e.get("rev_est"), e.get("rev_surprise"))
                    for e in evs if e.get("period") and e.get("filed")]
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO earnings_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
            con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,now())",
                        [ticker, len(rows), "ok"])
            ok += 1
            total_ev += len(rows)
            if i % 10 == 0 or i == len(todo):
                el = time.time() - t0
                rate = i / el * 60
                print(f"  [{i}/{len(todo)}] {bare:6} 累计事件 {total_ev}  "
                      f"({rate:.0f} 只/分, 剩 {(len(todo)-i)/max(rate,1):.0f} 分)", flush=True)
    finally:
        client.close()
    print(f"\n✅ 完成 {ok} 只 / 失败 {fail},新增事件 {total_ev}")
    return 0


def cmd_stats(args, con) -> int:
    n_t, n_e = con.execute(
        "SELECT count(DISTINCT ticker), count(*) FROM earnings_events").fetchone()
    print(f"=== 事件库 {n_t} 只标的 / {n_e} 个财报事件 ===\n")
    if not n_e:
        return 0
    q = """
    WITH ev AS (
      SELECT *, iv_pre/100*sqrt(4.0/252)*100 AS implied5, abs(move_d4) AS realised5
      FROM earnings_events
      WHERE iv_pre IS NOT NULL AND move_d4 IS NOT NULL AND iv_pre BETWEEN 5 AND 300
    )
    SELECT count(*) n,
           round(avg(implied5),2) imp_avg, round(median(implied5),2) imp_med,
           round(avg(realised5),2) act_avg, round(median(realised5),2) act_med,
           round(100.0*sum(CASE WHEN realised5 > implied5 THEN 1 ELSE 0 END)/count(*),1) rich_pct
    FROM ev"""
    n, ia, im, aa, am, rich = con.execute(q).fetchone()
    print(f"隐含 vs 实际(财报后4日,已做时段对齐,剔除 IV 异常值,n={n}):")
    print(f"  隐含 均值 {ia}%  中位数 {im}%")
    print(f"  实际 均值 {aa}%  中位数 {am}%")
    print(f"  实际超过隐含的比例 {rich}%   →  {'买方' if rich>50 else '卖方'}占优")
    print(f"  比值 均值 {round(aa/ia,3)}  中位数 {round(am/im,3)}\n")

    print("EPS 意外 vs 财报当日反应:")
    for row in con.execute("""
        SELECT eps_surprise, count(*) n,
               round(avg(move_d0),2) d0_avg, round(median(move_d0),2) d0_med,
               round(100.0*sum(CASE WHEN move_d0>0 THEN 1 ELSE 0 END)/count(*),1) up_pct,
               round(avg(move_d5),2) d5_avg
        FROM earnings_events
        WHERE eps_surprise IS NOT NULL AND move_d0 IS NOT NULL
        GROUP BY 1 ORDER BY n DESC""").fetchall():
        s, n2, a, m, up, d5 = row
        print(f"  {s:7} n={n2:<6} 当日均值 {a:+6}%  中位 {m:+6}%  上涨 {up:>5}%  +5日均值 {d5:+6}%")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["universe", "events", "stats"])
    ap.add_argument("--weeks", type=int, default=8)
    ap.add_argument("--min-cap", type=float, default=2e9)
    ap.add_argument("--min-opt-vol", type=float, default=1000)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--per-ticker", type=int, default=12)
    ap.add_argument("--stale-days", type=int, default=80,
                    help="最新事件早于 N 天的票视为过期,重新抓取(0=不重抓)")
    ap.add_argument("--fast", action="store_true", help="跳过 EPS 意外(只取波动/价格反应),快一个数量级")
    args = ap.parse_args()
    con = connect()
    try:
        return {"universe": cmd_universe, "events": cmd_events,
                "stats": cmd_stats}[args.cmd](args, con)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
