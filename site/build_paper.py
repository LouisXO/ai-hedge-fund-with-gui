#!/usr/bin/env python
"""Public home page = the paper portfolio: site/public/index.html (+ paper.html), bilingual zh / en.

What goes public (decided 2026-09-23 by the user): the Alpaca PAPER account only — simulated
money, $100k — its three books, each from the close before its first fill vs SPY and QQQ on the
same days (agent/evaluate.baselines, S48), holdings, closed trades, fills vs the opening cross,
the pre-registered backtests and evaluation points. The user's REAL moomoo account never
appears here; this script does not read any acct_* table.

Usage: python site/build_paper.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from agent import evaluate, ledger  # noqa: E402
from agent.auction_basis import OUT as AUCTION_JSON  # noqa: E402
from hedge_fund.features.panel import PanelStore  # noqa: E402

OUT = os.path.join(ROOT, "site", "public")
VALID = os.path.join(ROOT, "site-data", "validation")
THEME = "/Users/louis/optradar/bin/theme.css"
PRIVATE_OUT = "/Users/louis/optradar/out"
sys.path.insert(0, "/Users/louis/optradar/bin")
BOOKS = {"long": ("长线综合因子", "Long composite"), "insider": ("内部人短线", "Insider short-term"), "core": ("SPY 核心仓", "SPY core")}


def T(zh: str, en: str) -> str:
    return f"<span class='zh'>{zh}</span><span class='en'>{en}</span>"


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def pc(v, nd=2) -> str:
    if v is None:
        return "—"
    return f"<span class='{'pos' if v > 0 else 'neg' if v < 0 else ''}'>{v:+.{nd}f}%</span>"


def book_gap(fills: list[dict], book: str) -> tuple[float | None, int]:
    """(mean gap to the opening cross in % per side, number of fills) for one book; None before any comparable fill."""
    g = [f["gap"] for f in fills if f["book"] == book and f["gap"] is not None]
    return (sum(g) / len(g) if g else None), len(g)


def collect() -> dict:
    con = ledger.connect(read_only=True)
    try:
        nav = con.execute("SELECT as_of, book, equity_usd, cash_usd, n_positions FROM agent_book_nav ORDER BY as_of, book").fetchall()
        alloc = dict(con.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
        lots = con.execute("""SELECT book, ticker, qty, entry_day, entry_px, entry_model_px, hold_until FROM agent_lots
                              WHERE status = 'open' ORDER BY book, entry_day, ticker""").fetchall()
        closed = con.execute("""SELECT book, ticker, entry_day, entry_px, exit_day, exit_px, ret_pct FROM agent_lots
                                WHERE status = 'closed' ORDER BY exit_day DESC LIMIT 50""").fetchall()
        fills = con.execute("""SELECT book, ticker, side, CAST(filled_at AS DATE), filled_at, filled_avg_px, model_px, order_type, tif
                               FROM agent_orders WHERE filled_qty > 0 AND dry_run = FALSE ORDER BY filled_at""").fetchall()
        with PanelStore(read_only=True) as store:
            base = evaluate.page_baselines(con, store)
            bt = evaluate.backtest_bench(store, VALID)
            tick = sorted({l[1] for l in lots})
            last = {}
            if tick:
                q = f"""SELECT ticker, last(close ORDER BY trade_date), max(trade_date) FROM bars
                        WHERE ticker IN ({','.join('?' * len(tick))}) AND trade_date >= ? GROUP BY 1"""
                last = {t: (c, d) for t, c, d in store.con.execute(q, tick + [dt.date.today() - dt.timedelta(days=10)]).fetchall()}
    finally:
        con.close()
    try:
        auction_rows = json.load(open(AUCTION_JSON)).get("fill_rows", [])
    except Exception:
        auction_rows = []
    days = sorted({r[0] for r in nav})
    by = {(r[0], r[1]): r for r in nav}
    comb = base.get("combined") or {}
    ser = comb.get("series", {})
    blank = [None] * len(comb.get("days", []))
    d = {"days": comb.get("days", []), "long": (ser.get("long") or {}).get("sim_tr", blank), "insider": (ser.get("insider") or {}).get("sim_tr", blank),
         "core": (ser.get("core") or {}).get("sim_tr", blank), "total": (comb.get("index") or {}).get("sim_tr", []),
         "total_auction": (comb.get("index") or {}).get("auction_tr", []), "spy": (comb.get("bench") or {}).get("SPY", []),
         "qqq": (comb.get("bench") or {}).get("QQQ", [])}
    latest = {b: by[(days[-1], b)] for b in alloc if days and (days[-1], b) in by}
    equity = {b: latest[b][2] for b in latest}
    holdings = []
    for b, t, q, eday, epx, empx, hu in lots:
        c = last.get(t, (None, None))[0]
        holdings.append({"book": b, "ticker": t, "qty": q, "entry_day": str(eday), "entry_px": epx, "last": c,
                         "ret": (c / epx - 1) * 100 if c and epx else None,
                         "weight": (q * c / equity[b] * 100) if c and equity.get(b) else None, "hold_until": str(hu) if hu else None})
    holdings.sort(key=lambda h: (h["book"], -(h["weight"] or 0)))
    picks = []
    try:
        import re as _re
        con = ledger.connect(read_only=True)
        rows = con.execute("""SELECT ticker, rank, value, gate_reason, as_of FROM agent_picks WHERE signal_name = 'composite_long'
                              AND as_of = (SELECT max(as_of) FROM agent_picks WHERE signal_name = 'composite_long') ORDER BY rank""").fetchall()
        con.close()
        for t, rk, v, g, asof in rows:
            fam = {k: (None if x == "nan" else float(x)) for k, x in _re.findall(r"(v|q|m|lv)([+-](?:[0-9.]+|nan))", g or "")}
            fam = {k: (None if x is None or x != x else x) for k, x in fam.items()}
            picks.append({"ticker": t, "rank": rk, "composite": v, "value": fam.get("v"), "quality": fam.get("q"),
                          "momentum": fam.get("m"), "lowvol": fam.get("lv"), "as_of": str(asof)})
    except Exception:
        picks = []
    try:
        import yaml
        ev = yaml.safe_load(open(os.path.join(ROOT, "agent", "config.yaml")))["evaluate_at"]
    except Exception:
        ev = {}
    return {"nav": d, "alloc": alloc, "latest": {b: {"equity": v[2], "cash": v[3], "n": v[4]} for b, v in latest.items()},
            "holdings": holdings, "closed": closed, "fills": evaluate.fill_gaps(fills, auction_rows), "bt": bt, "eval": ev, "base": base,
            "as_of": days[-1].isoformat() if days else None, "since": comb.get("base_day"), "picks": picks}


CSS_EXTRA = """
[data-lang=en] .zh{display:none}[data-lang=zh] .en{display:none}
.lang{display:inline-flex;border:1px solid var(--line-2);border-radius:999px;overflow:hidden;font-size:12px}
.lang button{background:none;border:0;color:var(--tx-2);padding:3px 10px;cursor:pointer;font:inherit}
.lang button.on{background:var(--acc);color:var(--acc-ink)}
.lang button:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
.lede{color:var(--tx-2);max-width:68ch;line-height:1.65}
.note{color:var(--dim);font-size:12px;max-width:80ch}
"""

JS = r"""
const css = getComputedStyle(document.documentElement), C = k => css.getPropertyValue(k).trim();
const D = window.PAPER; let charts = [];
function lang() { try { return localStorage.getItem('lang') } catch (e) { return null } }
function setLang(l) {
  document.documentElement.dataset.lang = l; document.documentElement.lang = l === 'en' ? 'en' : 'zh';
  document.querySelectorAll('.lang button').forEach(b => b.classList.toggle('on', b.dataset.l === l));
  try { localStorage.setItem('lang', l) } catch (e) {}
  draw(l);
}
function draw(l) {
  charts.forEach(c => c.destroy()); charts = [];
  const L = (zh, en) => l === 'en' ? en : zh;
  Chart.defaults.font.family = C('--font'); Chart.defaults.color = C('--dim'); Chart.defaults.borderColor = C('--line');
  Object.assign(Chart.defaults.plugins.legend.labels, {boxWidth: 10, boxHeight: 10, usePointStyle: true});
  Object.assign(Chart.defaults.plugins.tooltip, {backgroundColor: C('--surface-2'), titleColor: C('--tx'), bodyColor: C('--tx-2'),
    borderColor: C('--line-2'), borderWidth: 1, padding: 8, bodyFont: {family: C('--mono')}});
  const grid = {color: C('--line'), drawTicks: false}, pct = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  const line = (label, data, color, dash) => ({label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
    pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: C('--surface'), tension: .15, spanGaps: true, borderDash: dash || []});
  const bar = (label, data, color) => ({label, data, backgroundColor: color, borderColor: C('--surface'), borderWidth: 1, borderRadius: 4,
    borderSkipped: 'start', maxBarThickness: 30});
  const cross = {id: 'x', afterDraw(ch) { const a = ch.tooltip?.getActiveElements?.() || []; if (!a.length) return;
    const x = a[0].element.x, y = ch.chartArea, g = ch.ctx; g.save(); g.strokeStyle = C('--line-2'); g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(x, y.top); g.lineTo(x, y.bottom); g.stroke(); g.restore(); }};
  const n = D.nav;
  if (n.days.length) charts.push(new Chart(document.getElementById('c_nav'), {type: 'line', plugins: [cross],
    data: {labels: n.days, datasets: [line(L('长线', 'Long'), n.long, C('--s1')), line(L('内部人', 'Insider'), n.insider, C('--s2')),
      line(L('SPY 核心仓', 'SPY core'), n.core, C('--s4')),
      line(L('合计(含分红)', 'Total (with dividends)'), n.total, C('--tx'), [2, 3]), line(L('合计(竞价口径)', 'Total (auction prices)'), n.total_auction, C('--s3'), [2, 3]),
      line('SPY', n.spy, C('--bench'), [6, 4]), line('QQQ', n.qqq, C('--s7'), [3, 3])]},
    options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
      plugins: {legend: {position: 'top', align: 'end'}, tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(2)}  (${c.parsed.y == null ? '—' : pct(c.parsed.y - 100)})`}}},
      scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8}}, y: {grid, title: {display: true, text: L('指数(首笔成交前一个收盘 = 100)', 'Index (close before the first fill = 100)')}}}}}));
  if (D.bt) charts.push(new Chart(document.getElementById('c_bt'), {type: 'bar',
    data: {labels: D.bt.years, datasets: [bar(L('长线', 'Long'), D.bt.long, C('--s1')), bar(L('内部人', 'Insider'), D.bt.insider, C('--s2')), bar('SPY', D.bt.spy, C('--bench')),
      bar('QQQ', D.bt.qqq, C('--s7'))]},
    options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
      tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${pct(c.parsed.y)}`}}},
      scales: {x: {grid: {display: false}}, y: {grid, ticks: {callback: v => v + '%'}}}}}));
  if (D.picks && D.picks.length && document.getElementById('c_fam')) {
    const P = D.picks, fams = [['value', L('价值', 'Value'), '--s3'], ['quality', L('质量', 'Quality'), '--s4'], ['momentum', L('动量', 'Momentum'), '--s5'], ['lowvol', L('低波动', 'Low volatility'), '--s7']];
    charts.push(new Chart(document.getElementById('c_fam'), {type: 'bar',
      data: {labels: P.map(p => p.rank + '  ' + p.ticker), datasets: fams.map(([k, lab, col]) => ({label: lab, data: P.map(p => p[k] == null ? 0 : p[k] / 4),
        backgroundColor: C(col), borderColor: C('--surface'), borderWidth: 1, borderSkipped: false, maxBarThickness: 14}))},
      options: {indexAxis: 'y', maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
        plugins: {legend: {position: 'top', align: 'end'}, tooltip: {callbacks: {
          title: i => P[i[0].dataIndex].ticker + ' · ' + L('综合 ', 'composite ') + P[i[0].dataIndex].composite.toFixed(2),
          label: c => ` ${c.dataset.label}  z ${P[c.dataIndex][fams[c.datasetIndex][0]] == null ? '—' : P[c.dataIndex][fams[c.datasetIndex][0]].toFixed(2)}  → ${c.parsed.x >= 0 ? '+' : ''}${c.parsed.x.toFixed(2)}`}}},
        scales: {x: {stacked: true, grid, title: {display: true, text: L('对综合分的贡献', 'contribution to composite')}},
                 y: {stacked: true, grid: {display: false}, ticks: {font: {family: C('--mono'), size: 11}, autoSkip: false}}}}}));
  }
}
const pref = lang() || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
document.querySelectorAll('.lang button').forEach(b => b.addEventListener('click', () => setLang(b.dataset.l)));
setLang(pref);
"""


FAMILIES = [("value", "价值", "Value", "--s3"), ("quality", "质量", "Quality", "--s4"), ("momentum", "动量", "Momentum", "--s5"), ("lowvol", "低波动", "Low volatility", "--s7")]


def long_sections(d: dict) -> tuple[str, str]:
    """The short card in 'The two books' and the long, detailed explanation with today's scores."""
    card = (f"<div class='card'><div class='k'>{T('长线综合因子 · $60,000 · 30 仓', 'Long composite · $60,000 · 30 slots')}</div>"
            f"<p>{T('每天给全市场打分,四个经典因子等权合成,排名进前 30 买入、跌出前 60 卖出。怎么打分见下面「长线书怎么选股」。',
                    'Scores the whole market daily on four classic factors, equal weight; buys the top 30 and sells below 60. The full method is under “How the long book picks stocks” below.')}</p>"
            f"<p><a href='#long-method'>{T('看详细方法 ↓', 'Read the method ↓')}</a></p></div>")
    steps = [
        (T("1 · 股票池", "1 · Universe"),
         T("当天在交易的美股,过去 20 个交易日平均成交额至少 500 万美元。成交太少的股票进出成本太高,不进池。",
           "US stocks listed that day with at least $5M average daily dollar volume over the last 20 sessions. Thinner names cost too much to trade and are left out.")),
        (T("2 · 基本面过滤", "2 · Fundamentals filter"),
         T("只用打分当天之前已经向 SEC 申报的财报(时点正确,不偷看未来),而且最近一份申报必须在 200 天以内。再排除三类会让比率失真的公司:市值低于 1 亿美元、总资产低于 1,000 万美元、股东权益不到总资产的 5%(权益接近零或为负时,ROE 和账面市值比都没有意义)。",
           "Only filings already public with the SEC on the scoring day are used (point in time, no look-ahead), and the latest must be under 200 days old. Three kinds of company are removed because their ratios break: market cap under $100M, total assets under $10M, and equity under 5% of assets (near-zero or negative equity makes ROE and book-to-market meaningless).")),
        (T("3 · 八个原始指标,分成四族", "3 · Eight raw measures in four families"), None),
        (T("4 · 标准化", "4 · Standardize"),
         T("每个指标当天在全池里先截掉最高和最低 1%,再减均值、除以标准差,变成 z 分。一个族的分数是族内指标的平均。",
           "Each measure is clipped at the day's 1st and 99th percentiles, then turned into a z-score across the universe. A family's score is the mean of its measures.")),
        (T("5 · 合成", "5 · Combine"),
         T("综合分 = 四个族分数的简单平均,不按历史收益拟合权重。至少要有三个族有分数才参与排名。",
           "Composite = the plain average of the four family scores. No weights are fitted to past returns. A stock needs scores in at least three families to be ranked.")),
        (T("6 · 交易", "6 · Trade"),
         T("每天收盘后按综合分排名。排名进前 30 且有空槽位就买入,每个槽位 = 书净值 ÷ 30;已持有的股票只要还在前 60 就继续拿,掉出前 60 才卖。前 30 和前 60 之间的缓冲区是为了减少来回换手。订单在次日开盘成交;空着的槽位留现金,不分给其他股票。没有止损(回测里止损降低了收益)。",
           "After each close the market is ranked by composite. A name in the top 30 is bought if a slot is free; each slot is book equity ÷ 30. A held name is kept while it stays in the top 60 and sold when it drops out; the gap between 30 and 60 cuts churn. Orders fill at the next open. Empty slots stay in cash. There is no stop-loss (stops lowered returns in the backtest).")),
    ]
    fam_rows = [
        (T("价值", "Value"), T("账面市值比", "Book-to-market"), T("股东权益 ÷ 市值", "equity ÷ market cap"), T("越高越好", "higher is better"), T("买得便宜", "cheap relative to net assets")),
        ("", T("盈利收益率", "Earnings yield"), T("近 12 个月净利润 ÷ 市值", "trailing-12-month net income ÷ market cap"), T("越高越好", "higher is better"), T("每一块钱市值对应的利润", "profit per dollar of price")),
        (T("质量", "Quality"), T("毛利 ÷ 总资产", "Gross profit ÷ assets"), T("近 12 个月毛利 ÷ 总资产", "TTM gross profit ÷ total assets"), T("越高越好", "higher is better"), T("资产赚钱的效率(Novy-Marx)", "how productive the assets are (Novy-Marx)")),
        ("", "ROE", T("近 12 个月净利润 ÷ 股东权益", "TTM net income ÷ equity"), T("越高越好", "higher is better"), T("股东的钱的回报", "return on shareholders’ money")),
        ("", T("应计项", "Accruals"), T("(净利润 − 经营现金流)÷ 总资产", "(net income − operating cash flow) ÷ assets"), T("越低越好", "lower is better"), T("利润里现金越多、会计估计越少越可信", "earnings backed by cash are more reliable")),
        ("", T("资产增长", "Asset growth"), T("总资产一年增长率", "one-year growth in total assets"), T("越低越好", "lower is better"), T("扩张太快的公司之后往往跑输", "fast-expanding firms tend to lag later")),
        (T("动量", "Momentum"), T("12-1 月动量", "12-1 month momentum"), T("过去 12 个月涨幅,跳过最近 1 个月", "return over the past 12 months, skipping the latest month"), T("越高越好", "higher is better"), T("强者恒强;跳过最近一个月是为了避开短期反转", "winners keep winning; the last month is skipped to avoid short-term reversal")),
        (T("低波动", "Low volatility"), T("252 日波动率", "252-day volatility"), T("过去一年日收益的标准差", "standard deviation of daily returns over a year"), T("越低越好", "lower is better"), T("低波动股票的风险调整后收益更高", "calmer stocks earn more per unit of risk")),
    ]
    frows = "".join(f"<tr><td class='l'><b>{a}</b></td><td class='l'>{b}</td><td class='l'>{c}</td><td class='l'>{d_}</td><td class='l muted'>{e_}</td></tr>" for a, b, c, d_, e_ in fam_rows)
    fam_table = ("<div class='tbl'><table><tr><th class='l'>" + T("族", "Family") + "</th><th class='l'>" + T("指标", "Measure") + "</th><th class='l'>"
                 + T("怎么算", "Formula") + "</th><th class='l'>" + T("方向", "Direction") + "</th><th class='l'>" + T("为什么", "Why") + f"</th></tr>{frows}</table></div>")
    step_html = ""
    for title, body in steps:
        step_html += f"<h3>{title}</h3>" + (fam_table if body is None else f"<p class='lede'>{body}</p>")

    picks = d.get("picks") or []
    live = ""
    if picks:
        def z(v):
            return "—" if v is None else f"<span class='{'pos' if v > 0 else 'neg' if v < 0 else ''}'>{v:+.2f}</span>"
        held = {h["ticker"] for h in d["holdings"] if h["book"] == "long"}
        prow = "".join(f"<tr><td>{p['rank']}</td><td><b>{e(p['ticker'])}</b>{' <span class=pill>' + T('持有', 'held') + '</span>' if p['ticker'] in held else ''}</td>"
                       f"<td>{z(p['value'])}</td><td>{z(p['quality'])}</td><td>{z(p['momentum'])}</td><td>{z(p['lowvol'])}</td><td><b>{p['composite']:+.2f}</b></td></tr>"
                       for p in picks[:30])
        top = picks[:30]
        mom_cap = sum(1 for p in top if (p["momentum"] or 0) >= 3)
        deep_val = sum(1 for p in top if (p["value"] or 0) >= 1.5)
        live = (f"<h3>{T('今天的打分', 'Today’s scores')} · {picks[0]['as_of']}</h3>"
                f"<p class='st'>{T('前 30 名每只股票的四个族分数(z 分)和综合分。图里每一段是一个族对综合分的贡献(族分数 ÷ 4),悬停看数值。',
                                   'The top 30 with their four family z-scores and the composite. Each bar segment is one family’s contribution to the composite (family score ÷ 4); hover for values.')}</p>"
                f"<div class='chart tall'><div class='cv' style='height:{max(360, 18 * len(top) + 60)}px'><canvas id='c_fam'></canvas></div></div>"
                "<details><summary>" + T("数据表", "Table") + "</summary><div class='tbl'><table><tr><th>" + T("排名", "Rank") + "</th><th>" + T("代码", "Ticker") + "</th><th>"
                + T("价值", "Value") + "</th><th>" + T("质量", "Quality") + "</th><th>" + T("动量", "Momentum") + "</th><th>" + T("低波动", "Low vol") + "</th><th>"
                + T("综合", "Composite") + f"</th></tr>{prow}</table></div></details>"
                f"<h3>{T('这本书实际买到了什么', 'What the book actually ends up owning')}</h3>"
                f"<p class='lede'>{T(f'四个族等权,但实际持仓往往分成两群。一群是动量极强的股票:动量 z 分能到 4 以上,而其他族很少超过 2,所以一只股票只要涨得足够多,就能靠动量一项排进前 30(今天前 30 里有 {mom_cap} 只动量分 ≥ 3)。另一群是深度价值股,常见的是商业发展公司(BDC)和抵押贷款 REIT,账面市值比和盈利收益率都很高(今天有 {deep_val} 只价值分 ≥ 1.5)。基本面过滤的作用是把动量尾部里没有盈利、没有资产的公司挡在外面。回测检验过:把这两群拆开、改成排名标准化、或只做纯动量,效果都更差(S29、S30),所以规则保持原样。',
                                  f'The four families are equal-weighted, but the holdings usually split into two groups. One is extreme momentum: momentum z-scores can exceed 4 while the other families rarely pass 2, so a stock that has risen enough can reach the top 30 on momentum alone ({mom_cap} of today’s top 30 have a momentum score of 3 or more). The other is deep value, often business development companies and mortgage REITs with high book-to-market and earnings yield ({deep_val} today have a value score of 1.5 or more). The fundamentals filter keeps unprofitable, asset-light names out of the momentum tail. Rank-based scaling, splitting the two groups, and a pure-momentum book were all tested and did worse (S29, S30), so the rule stays as it is.')}</p>")
    lt_ = ((d.get("bt") or {}).get("long_meta") or {}).get("active_t_nw") or 0.92      # S33 report; 0.92 if it is missing
    extra = (f"<h3>{T('已知的弱点', 'Known weaknesses')}</h3><ul class='plain'>"
             f"<li>{T(f'回测对 SPY 的超额收益不显著(主动收益 t {lt_:.2f}),这正是要用模拟盘攒记录的原因。', f'The backtested excess return over SPY is not significant (active t {lt_:.2f}), which is why the paper record exists.')}</li>"
             f"<li>{T('行业不做中性化,会集中在某几个行业;测试过行业中性版本,alpha 几乎归零(S24)。', 'It is not sector-neutral and can concentrate in a few industries; a sector-neutral version was tested and lost almost all its alpha (S24).')}</li>"
             f"<li>{T('动量在熊市反弹时会大幅回撤。一个预注册的影子版本(SPY 比两年高点低 20% 以上时停用动量)每天并排记录,不交易,评估点之后再决定。', 'Momentum crashes in bear-market rebounds. A pre-registered shadow version that drops momentum when SPY is more than 20% below its two-year high is recorded daily beside it, untraded, to be judged at the evaluation point.')}</li>"
             f"<li>{T('模拟盘成交价是模拟器给的,小盘股的真实开盘冲击测不出来。', 'Paper fills come from a simulator; the real opening-auction impact on small caps is not measured.')}</li></ul>")
    detail = (f"<h2 id='long-method'>{T('长线书怎么选股', 'How the long book picks stocks')}</h2>"
              f"<p class='lede'>{T('完全是规则,没有人工判断,也没有机器学习拟合。四个因子都是学术文献里存在了几十年的经典定义,参数在上线前写死。',
                                   'It is entirely rule-based: no discretion and no machine-learned weights. All four factors use textbook definitions that have been in the academic literature for decades, and every parameter was fixed before launch.')}</p>"
              + step_html + live + extra)
    return card, detail


def page(d: dict, private: bool = False) -> str:
    n, lt, bt = d["nav"], d["latest"], d["bt"]
    theme = open(THEME).read()
    long_card, long_detail = long_sections(d)
    lang_btn = "<span class='lang' role='group' aria-label='Language'><button type='button' data-l='zh'>中文</button><button type='button' data-l='en'>EN</button></span>"
    if private:                      # same page inside the private site: its nav bar, plus the language switch
        import site_theme
        nav_slot = site_theme.nav("paper").replace("<span class='sp'></span>", "<span class='sp'></span>" + lang_btn, 1)
    else:
        nav_slot = (f"<nav class='nav'><span class='brand'>AI Hedge Fund</span><a href='index.html' class='on'>{T('模拟盘', 'Paper portfolio')}</a>"
                    f"<a href='masters/index.html'>{T('大师信号(已停止)', 'Master signals (retired)')}</a><span class='sp'></span>{lang_btn}</nav>")
    cards = []
    base = d.get("base") or {}
    for b, (zh, en) in BOOKS.items():
        x = (base.get("books") or {}).get(b)
        if not x or b not in lt:
            continue
        r, bm = x["ret"], x["bench"]
        ex = "—" if x["exposure_avg_pct"] is None else f"{x['exposure_avg_pct']:.0f}%"
        mg, ng = book_gap(d["fills"], b)
        drag = (T(f"模拟器按开盘后卖一成交(这本书实测 {'还没有' if mg is None else f'{mg:+.2f}%/边,{ng} 笔,样本很小'}),这本书约 5 个交易日换一次仓,模拟器口径很可能大幅低于竞价口径(S45 按买入一侧实测估计每年约 20–31 个百分点;卖出一侧还没有实测)。",
                  f"The simulator fills at the first ask after the open (this book measured {'nothing yet' if mg is None else f'{mg:+.2f}% per side over {ng} fills, a small sample'}) and this book turns over every five sessions, so its simulator NAV very likely runs far below the auction basis (S45 estimate from the buy side only: about 20–31 points a year).")
                if b == "insider" else "")
        cards.append(f"<div class='card'><div class='k'>{T(zh, en)} · {T('自', 'since')} {x['base_day']} {T('收盘', 'close')}</div><div class='v'>{pc(r['sim_tr'])}</div>"
                     f"<div class='s'>{T('模拟器口径,含分红', 'simulator basis, with dividends')} · {T('竞价口径', 'auction basis')} {pc(r['auction_tr'])}</div>"
                     f"<div class='s'>{T('同期', 'same days')} SPY {pc(bm['SPY'])} · QQQ {pc(bm['QQQ'])}</div>"
                     f"<div class='s'>{T('平均仓位', 'avg. invested')} {ex} · ${lt[b]['equity']:,.0f} · {lt[b]['n']} {T('仓', 'positions')} · {T('分配', 'allocated')} ${d['alloc'][b]:,.0f}</div>"
                     + (f"<div class='s'>{drag}</div>" if drag else "") + "</div>")
    c = base.get("combined")
    if c:
        ex = "—" if c["exposure_avg_pct"] is None else f"{c['exposure_avg_pct']:.0f}%"
        cards.append(f"<div class='card'><div class='k'>{T('合计', 'Total')} · {T('自', 'since')} {c['base_day']} {T('收盘', 'close')}</div><div class='v'>{pc(c['ret']['sim_tr'])}</div>"
                     f"<div class='s'>{T('按日收益连乘,模拟器口径含分红', 'daily returns chained, simulator basis with dividends')} · {T('竞价口径', 'auction basis')} {pc(c['ret']['auction_tr'])}</div>"
                     f"<div class='s'>{T('同期', 'same days')} SPY {pc(c['bench_ret']['SPY'])} · QQQ {pc(c['bench_ret']['QQQ'])} · {T('平均仓位', 'avg. invested')} {ex}</div></div>")

    hrows = []
    for h in d["holdings"]:
        last = "—" if h["last"] is None else f"{h['last']:.2f}"
        w = "—" if h["weight"] is None else f"{h['weight']:.1f}%"
        until = h["hold_until"] or T("排名跌出前 60", "until rank > 60")
        hrows.append(f"<tr><td class='l'>{T(*BOOKS[h['book']])}</td><td><b>{e(h['ticker'])}</b></td><td>{h['entry_day']}</td>"
                     f"<td>{h['entry_px']:.2f}</td><td>{last}</td><td>{pc(h['ret'], 1)}</td><td>{w}</td><td>{until}</td></tr>")
    crows = [f"<tr><td class='l'>{T(*BOOKS[b])}</td><td><b>{e(t)}</b></td><td>{ed}</td><td>{ep:.2f}</td><td>{xd}</td><td>{xp:.2f}</td><td>{pc(r, 1)}</td></tr>"
             for b, t, ed, ep, xd, xp, r in d["closed"]]

    fl = d["fills"]
    ok = [f for f in fl if f["gap"] is not None]
    if ok:
        mean = sum(f["gap"] for f in ok) / len(ok)
        since = min(f["day"] for f in fl)
        kinds = {k: [f["gap"] for f in ok if f["kind"] == k] for k in ("opg", "day_market", "day_limit")}
        zh_k = ";".join(f"{z} {len(g)} 笔 {sum(g) / len(g):+.2f}%" for k, z in (("opg", "开盘竞价单"), ("day_market", "DAY 市价单"), ("day_limit", "DAY 限价单")) if (g := kinds[k]))
        en_k = "; ".join(f"{z} {len(g)} at {sum(g) / len(g):+.2f}%" for k, z in (("opg", "opening-auction orders"), ("day_market", "DAY market"), ("day_limit", "DAY limit")) if (g := kinds[k]))
        gap_txt = T(f"自 {since} 起 {len(ok)} 笔成交,成交价对当天开盘竞价价平均 {mean:+.2f}%/边(正数 = 比竞价价差);{zh_k}。只有开盘竞价单在竞价里成交,DAY 单由模拟器在开盘后逐单撮合,那部分偏差不代表真实账户的成本。",
                    f"{len(ok)} fills since {since}, mean gap to the day's opening cross {mean:+.2f}% per side (positive = worse than the cross); {en_k}. "
                    "Only opening-auction orders fill in the cross; DAY orders are filled one by one by the simulator after the open, so that part of the gap is not a real account's cost.")
    else:
        gap_txt = T("还没有成交。", "No fills yet.")

    bt_js, bt_html = "null", ""
    if bt:
        lm, im = bt["long_meta"], bt["insider_meta"]
        bt_js = json.dumps({k: bt[k] for k in ("years", "long", "insider", "spy", "qqq")})
        q = "—" if bt.get("qqq_cagr_pct") is None else f"{bt['qqq_cagr_pct']:+.1f}%"
        zh = (f"{bt['start']} → {bt['end']},次日开盘入场,每边扣半个报价价差,闲置资金放 SPY(模拟盘的闲置资金是现金)。长线 CAGR {lm['cagr_pct']:+.1f}%,"
              f"同期 SPY {lm['spy_cagr_pct']:+.1f}%、QQQ {q};对 SPY 超额 {lm['excess_cagr_pct']:+.1f}%/年(主动收益 t {lm['active_t_nw']:.2f})。"
              f"内部人 CAGR {im['cagr_pct']:+.1f}%,对 SPY 超额 {im['excess_cagr_pct']:+.1f}%/年(t {im['active_t_nw']:.2f})。都不显著,所以要用模拟盘攒记录。")
        en = (f"{bt['start']} → {bt['end']}, entry at the next open, half the quoted spread per side, idle cash in SPY (the paper books hold idle cash as cash). "
              f"Long book CAGR {lm['cagr_pct']:+.1f}% against SPY {lm['spy_cagr_pct']:+.1f}% and QQQ {q}; excess over SPY {lm['excess_cagr_pct']:+.1f}%/yr (active t {lm['active_t_nw']:.2f}). "
              f"Insider line CAGR {im['cagr_pct']:+.1f}%, excess over SPY {im['excess_cagr_pct']:+.1f}%/yr (t {im['active_t_nw']:.2f}). Neither is significant, which is why the paper record exists.")
        bt_html = (f"<div class='chart tall'><p class='t'>{T('回测 · 按年收益 vs SPY、QQQ', 'Backtest · calendar-year returns vs SPY and QQQ')}</p>"
                   f"<p class='st'>{T(zh, en)}</p><div class='cv'><canvas id='c_bt'></canvas></div></div>")

    ev = d["eval"]
    ev_items = ""
    for b, x in ev.items():
        if b in BOOKS and isinstance(x, dict):
            k, dte = x["n_closed_lots"], x["or_date"]
            ev_items += f"<li>{T(BOOKS[b][0], BOOKS[b][1])}: {T(f'{k} 笔平仓或 {dte},先到为准', f'{k} closed trades or {dte}, whichever comes first')}</li>"

    nav_js = json.dumps({"nav": n, "bt": json.loads(bt_js) if bt_js != "null" else None, "picks": (d.get("picks") or [])[:30]})
    ht = ("<tr><th class='l'>" + T("书", "Book") + "</th><th>" + T("代码", "Ticker") + "</th><th>" + T("入场日", "Entered") + "</th><th>"
          + T("入场价", "Entry") + "</th><th>" + T("最新", "Last") + "</th><th>" + T("收益", "Return") + "</th><th>" + T("权重", "Weight") + "</th><th>"
          + T("计划出场", "Planned exit") + "</th></tr>")
    ct = ("<tr><th class='l'>" + T("书", "Book") + "</th><th>" + T("代码", "Ticker") + "</th><th>" + T("入场日", "Entered") + "</th><th>"
          + T("入场价", "Entry") + "</th><th>" + T("出场日", "Exited") + "</th><th>" + T("出场价", "Exit") + "</th><th>" + T("收益", "Return") + "</th></tr>")
    return f"""<!doctype html><html lang="zh" data-lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Paper Portfolio · AI Hedge Fund</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{theme}{CSS_EXTRA}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script></head><body>
{nav_slot}
<h1>{T('模拟盘', 'Paper portfolio')}</h1>
<p class='lede'>{T('两本规则驱动的股票书和一个 SPY 核心仓在 Alpaca 模拟账户上实时交易,虚拟资金 $100,000。每天收盘后打分,次日开盘下单,没有人工干预,也没有 LLM 做方向判断。这是回测之后的前瞻记录。',
                   'Two rule-based stock books and an SPY core position trade live on an Alpaca paper account with $100,000 of simulated money. They are scored after each close and ordered for the next open, with no discretion and no LLM making directional calls. This is the out-of-sample record that follows the backtests.')}</p>
<p class='muted'>{T('数据截至', 'Data as of')} {d['as_of'] or '—'} · {T('模拟资金,非真实账户', 'simulated money, not a real account')}</p>
<div class='cards'>{''.join(cards)}</div>

<h2>{T('净值 vs SPY、QQQ', 'NAV vs SPY and QQQ')}</h2>
<div class='chart tall'><p class='t'>{T('三本书净值', 'Book NAV')}</p><p class='st'>{T(f"每本书从首笔成交前一个收盘起 = 100;SPY、QQQ 自 {d['since']} 收盘起 = 100,用复权价(含分红)。书的线是模拟器口径加上持仓应得的分红(模拟账户不发分红);合计按日收益连乘;竞价口径 = 每笔成交按当天开盘竞价价重记、扣监管费。",
    f"Each book = 100 at the close before its first fill; SPY and QQQ = 100 at the {d['since']} close, on adjusted prices (dividends included). Book lines are the simulator NAV plus the dividends the positions were entitled to (the paper account pays none); the total chains daily returns; the auction basis re-prices every fill at that day's opening cross, net of regulatory fees.")}</p>
<div class='cv'><canvas id='c_nav'></canvas></div></div>

<h2>{T('三本书的规则', 'The three books')}</h2>
<div class='grid2'>
{long_card}
<div class='card'><div class='k'>{T('内部人短线 · $30,000 · 20 仓', 'Insider short-term · $30,000 · 20 slots')}</div>
<p>{T('公司高管或董事在公开市场用自己的钱买入本公司股票(SEC Form 4)后,次日开盘买入,持有 5 个交易日卖出。入场用当天有效的限价单(前一天收盘价 + 3%),不用市价单:模拟器的市价单按开盘后的卖一成交,可能远高于开盘价;高开超过 3% 的不追,下一个交易日再试一次。',
      'When an officer or director buys their own company’s stock in the open market (SEC Form 4), the book buys at the next open and sells five sessions later. Entries are DAY limit orders at the previous close + 3%, not market orders: the simulator fills a market order at the first ask after the open, which can be far above the open; a name that opens more than 3% higher is not chased and is tried once more the next session.')}</p></div>
<div class='card'><div class='k'>{T('SPY 核心仓 · $10,000', 'SPY core · $10,000')}</div>
<p>{T('被动持有 SPY。大盘股选股的九个规则都没有找到边际(S37),所以这部分资金只拿指数,不按规则卖出。',
      'Holds SPY passively. Nine large-cap stock-picking rules found no edge (S37), so this money simply holds the index and is not sold by any rule.')}</p></div></div>

{long_detail}

<h2>{T('当前持仓', 'Holdings')} · {len(d['holdings'])}</h2>
<div class='tbl'><table>{ht}{''.join(hrows) or '<tr><td colspan=8>—</td></tr>'}</table></div>

<h2>{T('已平仓交易', 'Closed trades')}</h2>
{('<div class=tbl><table>' + ct + ''.join(crows) + '</table></div>') if crows else f"<p class='muted'>{T('还没有平仓的交易。', 'No closed trades yet.')}</p>"}

<h2>{T('执行', 'Execution')}</h2><p class='note'>{gap_txt}</p>

<h2>{T('回测对照', 'Backtest')}</h2>{bt_html or f"<p class='muted'>—</p>"}

<h2>{T('什么时候下结论', 'When we judge it')}</h2>
<p class='note'>{T('评估点在模拟盘开始之前就写死了。到点之前,不根据好坏改规则。', 'The evaluation points were fixed before the paper record started. Until then no rule changes on the strength of results.')}</p>
<ul class='plain'>{ev_items}</ul>

<footer>{T('<b>免责声明</b> 本页是个人研究项目的自动输出,资金为模拟资金,不构成投资建议。作者可能持有页内标的。',
           '<b>Disclaimer</b> Automated output of a personal research project. The money is simulated. Nothing here is investment advice; the author may hold names shown.')}
 · {T('生成于', 'Generated')} {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · Alpaca paper · SEC EDGAR</footer>
<script>window.PAPER = {nav_js};</script><script>{JS}</script></body></html>"""


def main() -> int:
    d = collect()
    os.makedirs(OUT, exist_ok=True)
    html_ = page(d)
    for name in ("index.html", "paper.html"):            # the home page; paper.html kept for links already shared
        with open(os.path.join(OUT, name), "w") as f:
            f.write(html_)
    # everything public is also on the private site: the same page, and the master-signal archive
    if os.path.isdir(PRIVATE_OUT):
        with open(os.path.join(PRIVATE_OUT, "paper.html"), "w") as f:
            f.write(page(d, private=True))
        src = os.path.join(OUT, "masters")
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(PRIVATE_OUT, "masters"), dirs_exist_ok=True)
    print(f"paper.html: {len(d['nav']['days'])} days, {len(d['holdings'])} holdings, {len(d['closed'])} closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
