"""Section ⑨ of the morning brief — the agent's shadow record, numbers only.

No LLM here: the narrator is deferred until something is validated
(docs/AGENT_PLAN.md §9). Prints a one-line summary for the push text, or
splices an HTML block into the day's report with --append-html.

Usage:
  python -m agent.brief <date>                     # one-line summary
  python -m agent.brief <date> --append-html FILE
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

from agent import ledger

AGENT_OUT = "/Users/louis/optradar/out/agent"


def load(date: str) -> dict | None:
    path = os.path.join(AGENT_OUT, f"{date}.json")
    if not os.path.exists(path):
        cands = sorted(f for f in os.listdir(AGENT_OUT) if f.endswith(".json")) if os.path.isdir(AGENT_OUT) else []
        if not cands:
            return None
        path = os.path.join(AGENT_OUT, cands[-1])
    return json.load(open(path))


def one_line(d: dict, live: dict) -> str:
    sides = {r["side"]: r for r in live.get("by_side", [])}
    hit = ""
    for side in ("C", "P"):
        r = sides.get(side)
        if r and r["n"]:
            hit += f" {side}{r['n']}:{(r['dir_hit'] or 0):.0%}"
    n_stock = sum(1 for p in d["picks"] if p.get("instrument") == "stock")
    return (f"Agent[{d['mode']}] 便宜门 {d['gate']['passed']}/{d['gate']['of']} · "
            f"期权候选 {len(d['picks']) - n_stock} · 内部人股票 {n_stock}" + (f" · 10日方向命中{hit}" if hit else ""))


def _stock_rows(d: dict) -> str:
    """The insider stock candidates get their own table: limit price and expected net matter."""
    rows = []
    for p in [x for x in d["picks"] if x.get("instrument") == "stock"][:8]:
        net = p.get("expected_net_pct")
        rows.append(f"<tr><td>{p['ticker']}</td><td>{p.get('gate_reason','')}</td>"
                    f"<td>{p.get('limit_ref','—')}</td><td>{(p.get('spread_pct') or 0):.2f}%</td>"
                    f"<td>{net:+.2f}%</td></tr>" if net is not None else "")
    if not rows:
        return ""
    return ("<h3>内部人买入(股票,限价单)</h3><table>"
            "<tr><th>标的</th><th>类型</th><th>限价参考</th><th>报价价差</th><th>预期净(半价差)</th></tr>"
            + "".join(rows) + "</table>"
            "<p class='muted'>只做小盘/中盘;微盘价差 1.41% 吞掉边际,大盘边际仅 0.07%。"
            "必须挂限价,吃满价差则期望为零(S13)。</p>")


def _long_rows(d: dict, live_db) -> str:
    """Composite long book: the current month's 30 names (from the DB, since picks are monthly)."""
    try:
        rows = live_db.execute("""SELECT ticker, rank, value, gate_reason FROM agent_picks
                                  WHERE signal_name = 'composite_long'
                                    AND as_of = (SELECT max(as_of) FROM agent_picks WHERE signal_name = 'composite_long')
                                  ORDER BY rank LIMIT 30""").fetchall()
        as_of = live_db.execute("SELECT max(as_of) FROM agent_picks WHERE signal_name='composite_long'").fetchone()[0]
    except Exception:
        rows, as_of = [], None
    if not rows:
        return ""
    body = "".join(f"<tr><td>{t}</td><td>{r}</td><td>{v:+.2f}</td><td class='muted'>{g}</td></tr>" for t, r, v, g in rows)
    return (f"<h3>长线综合因子书(月度,{as_of})</h3><table><tr><th>标的</th><th>排名</th><th>综合分</th>"
            f"<th>价值/质量/动量/低波 z</th></tr>{body}</table>"
            "<p class='muted'>价值+质量+动量+低波等权 z 分,每日评分:进前 30 买入、跌出前 60 卖出,持有期由信号决定。"
            "回测(2017–2026)alpha2 +12.6%/年(t 2.25),13 个变体校正后未达显著,影子记录中。</p>")


def _paper_rows(live_db) -> str:
    """Alpaca paper account (agent/execute.py): per-book equity, fills of the last session, model-vs-paper gap."""
    try:
        nav = live_db.execute("""SELECT book, equity_usd, cash_usd, n_positions, as_of FROM agent_book_nav
                                 WHERE as_of = (SELECT max(as_of) FROM agent_book_nav) ORDER BY book""").fetchall()
        alloc = dict(live_db.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
        fills = live_db.execute("""SELECT book, ticker, side, filled_qty, filled_avg_px, model_px, reason FROM agent_orders
                                   WHERE filled_qty > 0 AND dry_run = FALSE
                                     AND CAST(filled_at AS DATE) = (SELECT max(CAST(filled_at AS DATE)) FROM agent_orders WHERE filled_qty > 0)
                                   ORDER BY book, side, ticker""").fetchall()
        pend = live_db.execute("""SELECT count(*) FROM agent_orders WHERE dry_run = FALSE
                                  AND status NOT IN ('filled','canceled','expired','rejected','done_for_day','replaced')""").fetchone()[0]
        gap = live_db.execute("""SELECT avg(CASE WHEN side='buy' THEN (filled_avg_px/model_px-1) ELSE (model_px/filled_avg_px-1) END)*100,
                                        count(*) FROM agent_orders WHERE filled_qty > 0 AND model_px > 0 AND dry_run = FALSE""").fetchone()
    except Exception:
        return ""
    if not nav:
        return ""
    head = " · ".join(f"{b} ${e:,.0f}({(e / alloc.get(b, e) - 1) * 100:+.1f}%,{n}仓)" for b, e, c, n, _ in nav)
    rows = "".join(f"<tr><td>{b}</td><td>{t}</td><td>{'买' if sd == 'buy' else '卖'}</td><td>{q:g}</td><td>{px:.2f}</td>"
                   f"<td>{(m or 0):.2f}</td><td>{((px / m - 1) * 100 if m else 0):+.2f}%</td><td class='muted'>{r}</td></tr>"
                   for b, t, sd, q, px, m, r in fills)
    table = (f"<table><tr><th>书</th><th>标的</th><th>方向</th><th>股数</th><th>成交价</th><th>模型价(开盘)</th><th>偏差</th><th>原因</th></tr>{rows}</table>"
             if rows else "<p class='muted'>上一交易日无成交</p>")
    cost = f",平均执行成本 {gap[0]:+.2f}%/边({gap[1]} 笔)" if gap and gap[1] else ""
    return (f"<h3>Alpaca 模拟盘({nav[0][4]})</h3><p>{head} · 挂单 {pend}{cost}</p>{table}"
            "<p class='muted'>收盘后打分,次日开盘竞价成交(买:限价开盘单,卖:市价开盘单)。偏差 = 模拟成交价 vs 回测假设的开盘价。</p>")


def html(d: dict, live: dict) -> str:
    rows = []
    for p in [x for x in d["picks"] if x.get("instrument") != "stock"][:10]:
        be = f"{p['breakeven_pct']:.2f}%" if p.get("breakeven_pct") is not None else "—"
        rows.append(f"<tr><td>{p['ticker']}</td><td>{p['signal_name']}</td><td>{'看涨' if p['side']=='C' else '看跌'}</td>"
                    f"<td>{p['rank']}</td><td>{p['value']:+.2f}</td><td>{p['iv']:.1f}%</td><td>{be}</td></tr>")
    score = ""
    for r in live.get("by_side", []):
        score += (f"<li>{'看涨' if r['side']=='C' else '看跌'}候选 {int(r['n'])} 个:"
                  f"{live['horizon']} 平均 {r['avg_ret']:+.2f}%,方向命中 {(r['dir_hit'] or 0):.0%}</li>")
    return f"""<h2>⑨ Agent(影子模式)</h2>
<p class="muted">{d['as_of']} · universe {d['universe_n']} · VIX {d['vix']:.1f}({d['regime']}) ·
便宜门通过 {d['gate']['passed']}/{d['gate']['of']} · 已验证信号 {len(d['validated_signals'])} 个 ·
<b>不开仓</b>,只记录(S3:无信号通过阈值)</p>
<table><tr><th>标的</th><th>信号</th><th>方向</th><th>排名</th><th>分数</th><th>IV</th><th>回本门槛(14天)</th></tr>
{''.join(rows) or '<tr><td colspan="7">今日无期权候选</td></tr>'}</table>
{_stock_rows(d)}
{live.get('_long_html', '')}
{live.get('_paper_html', '')}
<ul class="muted">{score or '<li>还没有满 10 天的记录</li>'}
<li>累计 {live.get('n_days', 0)} 天 / {live.get('n_signal_rows', 0)} 条信号记录</li></ul>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--append-html", default=None)
    ap.add_argument("--optradar-db", default=ledger.OPTRADAR_DB)
    args = ap.parse_args()

    d = load(args.date)
    if not d:
        return 1
    try:
        con = ledger.connect(args.optradar_db, read_only=True)
        try:
            live = ledger.live_summary(con)
            live["_long_html"] = _long_rows(d, con)
            live["_paper_html"] = _paper_rows(con)
        finally:
            con.close()
    except Exception:
        live = {}

    if args.append_html:
        if not os.path.exists(args.append_html):
            return 1
        page = open(args.append_html).read()
        if "⑨ Agent" in page:
            return 0
        block = html(d, live)
        open(args.append_html, "w").write(page.replace("<footer>", block + "<footer>", 1)
                                          if "<footer>" in page else page + block)
        return 0
    print(one_line(d, live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
