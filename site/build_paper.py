#!/usr/bin/env python
"""Public home page = the paper portfolio: site/public/index.html (+ paper.html), bilingual zh / en.

What goes public (decided 2026-09-23 by the user): the Alpaca PAPER account only — simulated
money, $100k — its two books, NAV vs SPY, holdings, closed trades, fills vs the model open,
the pre-registered backtests and evaluation points. The user's REAL moomoo account never
appears here; this script does not read any acct_* table.

Usage: python site/build_paper.py
"""
from __future__ import annotations

import datetime as dt
import glob
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from agent import ledger  # noqa: E402
from hedge_fund.features.panel import PanelStore  # noqa: E402

OUT = os.path.join(ROOT, "site", "public")
VALID = os.path.join(ROOT, "site-data", "validation")
THEME = "/Users/louis/optradar/bin/theme.css"
BOOKS = {"long": ("长线综合因子", "Long composite"), "insider": ("内部人短线", "Insider short-term")}


def T(zh: str, en: str) -> str:
    return f"<span class='zh'>{zh}</span><span class='en'>{en}</span>"


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def pc(v, nd=2) -> str:
    if v is None:
        return "—"
    return f"<span class='{'pos' if v > 0 else 'neg' if v < 0 else ''}'>{v:+.{nd}f}%</span>"


def collect() -> dict:
    con = ledger.connect(read_only=True)
    try:
        nav = con.execute("SELECT as_of, book, equity_usd, cash_usd, n_positions FROM agent_book_nav ORDER BY as_of, book").fetchall()
        alloc = dict(con.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
        lots = con.execute("""SELECT book, ticker, qty, entry_day, entry_px, entry_model_px, hold_until FROM agent_lots
                              WHERE status = 'open' ORDER BY book, entry_day, ticker""").fetchall()
        closed = con.execute("""SELECT book, ticker, entry_day, entry_px, exit_day, exit_px, ret_pct FROM agent_lots
                                WHERE status = 'closed' ORDER BY exit_day DESC LIMIT 50""").fetchall()
        fills = con.execute("""SELECT book, ticker, side, CAST(filled_at AS DATE), filled_avg_px, model_px, order_type, tif
                               FROM agent_orders WHERE filled_qty > 0 AND model_px > 0 AND dry_run = FALSE ORDER BY filled_at""").fetchall()
    finally:
        con.close()
    days = sorted({r[0] for r in nav})
    by = {(r[0], r[1]): r for r in nav}
    with PanelStore(read_only=True) as store:
        spy = dict(store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'SPY'").fetchall())
        tick = sorted({l[1] for l in lots})
        last = {}
        if tick:
            q = f"""SELECT ticker, last(close ORDER BY trade_date), max(trade_date) FROM bars
                    WHERE ticker IN ({','.join('?' * len(tick))}) AND trade_date >= ? GROUP BY 1"""
            last = {t: (c, d) for t, c, d in store.con.execute(q, tick + [dt.date.today() - dt.timedelta(days=10)]).fetchall()}
    spy0 = next((spy[x] for x in days if x in spy), None)
    total_alloc = sum(alloc.values())
    d = {"days": [x.isoformat() for x in days],
         "long": [round(by[(x, "long")][2] / alloc["long"] * 100, 3) if (x, "long") in by else None for x in days],
         "insider": [round(by[(x, "insider")][2] / alloc["insider"] * 100, 3) if (x, "insider") in by else None for x in days],
         "total": [round(sum(by[(x, b)][2] for b in alloc if (x, b) in by) / total_alloc * 100, 3) for x in days],
         "spy": [round(spy[x] / spy0 * 100, 3) if x in spy and spy0 else None for x in days]}
    latest = {b: by[(days[-1], b)] for b in alloc if days and (days[-1], b) in by}
    equity = {b: latest[b][2] for b in latest}
    holdings = []
    for b, t, q, eday, epx, empx, hu in lots:
        c = last.get(t, (None, None))[0]
        holdings.append({"book": b, "ticker": t, "qty": q, "entry_day": str(eday), "entry_px": epx, "last": c,
                         "ret": (c / epx - 1) * 100 if c and epx else None,
                         "weight": (q * c / equity[b] * 100) if c and equity.get(b) else None, "hold_until": str(hu) if hu else None})
    holdings.sort(key=lambda h: (h["book"], -(h["weight"] or 0)))
    bt = None
    files = sorted(glob.glob(os.path.join(VALID, "s33_negative_filters_*.json")))
    if files:
        j = json.load(open(files[-1]))["books"]
        bt = {"years": sorted(j["long_base"]["by_year"]),
              "long": j["long_base"], "insider": j["insider_base"]}
    try:
        import yaml
        ev = yaml.safe_load(open(os.path.join(ROOT, "agent", "config.yaml")))["evaluate_at"]
    except Exception:
        ev = {}
    return {"nav": d, "alloc": alloc, "latest": {b: {"equity": v[2], "cash": v[3], "n": v[4]} for b, v in latest.items()},
            "holdings": holdings, "closed": closed, "fills": fills, "bt": bt, "eval": ev,
            "as_of": d["days"][-1] if d["days"] else None, "since": d["days"][0] if d["days"] else None}


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
      line(L('合计', 'Total'), n.total, C('--tx'), [2, 3]), line('SPY', n.spy, C('--bench'), [6, 4])]},
    options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
      plugins: {legend: {position: 'top', align: 'end'}, tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(2)}  (${c.parsed.y == null ? '—' : pct(c.parsed.y - 100)})`}}},
      scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8}}, y: {grid, title: {display: true, text: L('指数(起点 = 100)', 'Index (start = 100)')}}}}}));
  if (D.bt) charts.push(new Chart(document.getElementById('c_bt'), {type: 'bar',
    data: {labels: D.bt.years, datasets: [bar(L('长线', 'Long'), D.bt.long, C('--s1')), bar(L('内部人', 'Insider'), D.bt.insider, C('--s2')), bar('SPY', D.bt.spy, C('--bench'))]},
    options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
      tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${pct(c.parsed.y)}`}}},
      scales: {x: {grid: {display: false}}, y: {grid, ticks: {callback: v => v + '%'}}}}}));
}
const pref = lang() || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
document.querySelectorAll('.lang button').forEach(b => b.addEventListener('click', () => setLang(b.dataset.l)));
setLang(pref);
"""


def page(d: dict) -> str:
    n, lt, bt = d["nav"], d["latest"], d["bt"]
    theme = open(THEME).read()
    cards = []
    for b, (zh, en) in BOOKS.items():
        if b in lt:
            x = lt[b]
            r = (x["equity"] / d["alloc"][b] - 1) * 100
            cards.append(f"<div class='card'><div class='k'>{T(zh, en)}</div><div class='v'>{pc(r)}</div>"
                         f"<div class='s'>${x['equity']:,.0f} · {x['n']} {T('仓', 'positions')} · {T('分配', 'allocated')} ${d['alloc'][b]:,.0f}</div></div>")
    if n["days"]:
        tot = n["total"][-1] - 100
        spy = n["spy"][-1] - 100 if n["spy"][-1] is not None else None
        cards.append(f"<div class='card'><div class='k'>{T('两本合计', 'Both books')}</div><div class='v'>{pc(tot)}</div>"
                     f"<div class='s'>{T('自', 'since')} {d['since']}</div></div>")
        cards.append(f"<div class='card'><div class='k'>{T('SPY 同期', 'SPY, same period')}</div><div class='v'>{pc(spy)}</div>"
                     f"<div class='s'>{len(n['days'])} {T('个交易日', 'sessions')}</div></div>")

    hrows = []
    for h in d["holdings"]:
        last = "—" if h["last"] is None else f"{h['last']:.2f}"
        w = "—" if h["weight"] is None else f"{h['weight']:.1f}%"
        until = h["hold_until"] or T("排名跌出前 60", "until rank > 60")
        hrows.append(f"<tr><td class='l'>{T(*BOOKS[h['book']])}</td><td><b>{e(h['ticker'])}</b></td><td>{h['entry_day']}</td>"
                     f"<td>{h['entry_px']:.2f}</td><td>{last}</td><td>{pc(h['ret'], 1)}</td><td>{w}</td><td>{until}</td></tr>")
    crows = [f"<tr><td class='l'>{T(*BOOKS[b])}</td><td><b>{e(t)}</b></td><td>{ed}</td><td>{ep:.2f}</td><td>{xd}</td><td>{xp:.2f}</td><td>{pc(r, 1)}</td></tr>"
             for b, t, ed, ep, xd, xp, r in d["closed"]]

    gaps = [((px / m - 1) * 100 * (1 if s == "buy" else -1), ot, tif) for _, _, s, _, px, m, ot, tif in d["fills"]]
    if gaps:
        mean = sum(g for g, _, _ in gaps) / len(gaps)
        auc = [g for g, ot, tif in gaps if ot == "market" or tif == "opg"]
        am = f"{sum(auc) / len(auc):+.2f}%" if auc else "—"
        gap_txt = T(f"{len(gaps)} 笔成交,对模型开盘价平均偏差 {mean:+.2f}%/边;其中开盘竞价成交 {len(auc)} 笔,平均 {am}。"
                    "模拟器对非竞价单在开盘后逐笔撮合,那部分偏差不代表真实成本。",
                    f"{len(gaps)} fills, mean gap to the model's open {mean:+.2f}% per side; {len(auc)} opening-auction fills averaging {am}. "
                    "The simulator fills non-auction orders one by one after the open, so that part of the gap is not a real cost.")
    else:
        gap_txt = T("还没有成交。", "No fills yet.")

    bt_js, bt_html = "null", ""
    if bt:
        lm, im = bt["long"], bt["insider"]
        years = bt["years"]
        bt_js = json.dumps({"years": years, "long": [round(lm["by_year"][y] * 100, 1) for y in years],
                            "insider": [round(im["by_year"].get(y, 0) * 100, 1) for y in years],
                            "spy": [round(lm["spy_by_year"][y] * 100, 1) for y in years]})
        zh = (f"2017 → 2026-08,次日开盘入场,每边扣半个报价价差,闲置资金放 SPY。长线 CAGR {lm['cagr_pct']:+.1f}%,"
              f"两因子 alpha {lm['alpha2_ann_pct']:+.1f}%/年(t {lm['alpha2_t_nw']:.2f});内部人 CAGR {im['cagr_pct']:+.1f}%,"
              f"alpha {im['alpha2_ann_pct']:+.1f}%/年(t {im['alpha2_t_nw']:.2f})。多重检验校正后都不显著,所以要用模拟盘攒证据。")
        en = (f"2017 → 2026-08, entry at the next open, half the quoted spread per side, idle cash in SPY. Long book CAGR {lm['cagr_pct']:+.1f}%, "
              f"two-factor alpha {lm['alpha2_ann_pct']:+.1f}%/yr (t {lm['alpha2_t_nw']:.2f}); insider line CAGR {im['cagr_pct']:+.1f}%, "
              f"alpha {im['alpha2_ann_pct']:+.1f}%/yr (t {im['alpha2_t_nw']:.2f}). Neither survives a multiple-testing correction, which is why the paper record exists.")
        bt_html = (f"<div class='chart tall'><p class='t'>{T('回测 · 按年收益 vs SPY', 'Backtest · calendar-year returns vs SPY')}</p>"
                   f"<p class='st'>{T(zh, en)}</p><div class='cv'><canvas id='c_bt'></canvas></div></div>")

    ev = d["eval"]
    ev_items = ""
    for b, x in ev.items():
        if b in BOOKS and isinstance(x, dict):
            k, dte = x["n_closed_lots"], x["or_date"]
            ev_items += f"<li>{T(BOOKS[b][0], BOOKS[b][1])}: {T(f'{k} 笔平仓或 {dte},先到为准', f'{k} closed trades or {dte}, whichever comes first')}</li>"

    nav_js = json.dumps({"nav": n, "bt": json.loads(bt_js) if bt_js != "null" else None})
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
<nav class='nav'><span class='brand'>AI Hedge Fund</span><a href='index.html' class='on'>{T('模拟盘', 'Paper portfolio')}</a>
<a href='masters/index.html'>{T('大师信号(已停止)', 'Master signals (retired)')}</a><span class='sp'></span>
<span class='lang' role='group' aria-label='Language'><button type='button' data-l='zh'>中文</button><button type='button' data-l='en'>EN</button></span></nav>
<h1>{T('模拟盘', 'Paper portfolio')}</h1>
<p class='lede'>{T('两本规则驱动的股票书在 Alpaca 模拟账户上实时交易,虚拟资金 $100,000。每天收盘后打分,次日开盘下单,没有人工干预,也没有 LLM 做方向判断。这是回测之后的前瞻记录。',
                   'Two rule-based stock books trade live on an Alpaca paper account with $100,000 of simulated money. They are scored after each close and ordered for the next open, with no discretion and no LLM making directional calls. This is the out-of-sample record that follows the backtests.')}</p>
<p class='muted'>{T('数据截至', 'Data as of')} {d['as_of'] or '—'} · {T('模拟资金,非真实账户', 'simulated money, not a real account')}</p>
<div class='cards'>{''.join(cards)}</div>

<h2>{T('净值 vs SPY', 'NAV vs SPY')}</h2>
<div class='chart tall'><p class='t'>{T('两本书净值', 'Book NAV')}</p><p class='st'>{T(f"自 {d['since']} 起,各自起点 = 100;虚线为合计与 SPY。", f"Since {d['since']}, each book indexed to 100; dashed lines are the total and SPY.")}</p>
<div class='cv'><canvas id='c_nav'></canvas></div></div>

<h2>{T('两本书的规则', 'The two books')}</h2>
<div class='grid2'>
<div class='card'><div class='k'>{T('长线综合因子 · $60,000 · 30 仓', 'Long composite · $60,000 · 30 slots')}</div>
<p>{T('每天给全市场打分:动量、价值、质量、低波动四个因子等权合成,先过基本面过滤(近 200 天有申报、市值超过 1 亿美元、权益超过资产 5%)。排名进前 30 买入,跌出前 60 卖出,等权。',
      'Every day the whole market is scored on four equal-weighted factor families (momentum, value, quality, low volatility) after a fundamentals filter (a filing in the last 200 days, market cap above $100M, equity above 5% of assets). Names enter in the top 30 and leave when they fall out of the top 60, equal weight.')}</p></div>
<div class='card'><div class='k'>{T('内部人短线 · $30,000 · 20 仓', 'Insider short-term · $30,000 · 20 slots')}</div>
<p>{T('公司高管或董事在公开市场用自己的钱买入本公司股票(SEC Form 4)后,次日开盘买入,持有 5 个交易日卖出。回测里一半的超额收益出现在入场第一天,所以只用开盘市价单。',
      'When an officer or director buys their own company’s stock in the open market (SEC Form 4), the book buys at the next open and sells five sessions later. Half of the backtested edge arrives on the first day, so entries are market-on-open only.')}</p></div></div>

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
    print(f"paper.html: {len(d['nav']['days'])} days, {len(d['holdings'])} holdings, {len(d['closed'])} closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
