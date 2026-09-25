"""Dashboard: the long-run pictures, interactive (Chart.js), private site only.

  模拟盘   both books' NAV since inception vs SPY (indexed to 100), every fill's gap to the
           model open, today's positions by day return
  实盘     2026 monthly return vs SPY, daily NAV since the snapshots began
  回测     the two live books by calendar year vs SPY (the pre-registered backtests the paper
           record is being measured against)
  教训     30-day rule counter from the daily review

Writes out/dashboard.html and out/dashboard_data.json (the front page draws the NAV chart
from the same JSON). Runs from agent/bin/postclose.sh and execute.sh.

Usage: python -m agent.dashboard
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys

import duckdb

from agent import ledger
from hedge_fund.features.panel import PanelStore

sys.path.insert(0, "/Users/louis/optradar/bin")
import site_theme  # noqa: E402

OUT = "/Users/louis/optradar/out"
AGENT_OUT = os.path.join(OUT, "agent")
VALID = "/Users/louis/hedge-fund/site-data/validation"
BOOK_LABEL = {"long": "长线综合因子", "insider": "内部人短线", "core": "SPY 核心仓"}
RULE_NAMES = {"paper_unfilled": "模拟盘未成交", "paper_exec_gap": "执行偏差 > 0.5%", "paper_insider_limit": "内部人限价入场",
              "real_overtrading": "实盘当日 ≥ 5 笔", "real_buy_after_jump": "大动后追买", "real_short_dte": "买临期期权", "real_0dte": "买当日到期期权",
              "real_otm_lottery": "买深虚值", "real_buy_high": "买在日高附近", "real_sell_low": "卖在日低附近", "real_vs_insiders": "逆内部人买入",
              "real_add_same_day": "同一合约当日加仓"}


# ------------------------------------------------------------------ data ----
def collect() -> dict:
    d: dict = {"generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    con = ledger.connect(read_only=True)
    try:
        nav = con.execute("SELECT as_of, book, equity_usd, n_positions FROM agent_book_nav ORDER BY as_of, book").fetchall()
        alloc = dict(con.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
        fills = con.execute("""SELECT book, ticker, side, CAST(filled_at AS DATE), filled_at, filled_avg_px, model_px, order_type, tif
                               FROM agent_orders WHERE filled_qty > 0 AND model_px > 0 AND dry_run = FALSE ORDER BY filled_at""").fetchall()
        monthly = con.execute("SELECT month, pnl_usd, ret_pct FROM acct_monthly ORDER BY month").fetchall()
        rnav = con.execute("SELECT date, sum(total_assets), sum(cash) FROM acct_nav GROUP BY 1 ORDER BY 1").fetchall()
        closed = con.execute("SELECT book, count(*) FROM agent_lots WHERE status = 'closed' GROUP BY 1").fetchall()
        try:
            auc = {(r[0], r[1]): r[2] for r in con.execute("SELECT as_of, book, equity_auction FROM agent_auction_nav").fetchall()}
        except Exception:
            auc = {}
    finally:
        con.close()
    days = sorted({r[0] for r in nav})
    by = {(r[0], r[1]): r for r in nav}
    with PanelStore(read_only=True) as store:
        spy = {r[0]: r[1] for r in store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()}
        spy_m = store.con.execute("""SELECT strftime(trade_date, '%Y-%m'), last(adj_close ORDER BY trade_date) FROM index_daily
                                     WHERE symbol = 'SPY' AND trade_date >= '2025-12-01' GROUP BY 1 ORDER BY 1""").fetchall()
    # paper NAV indexed to 100 on the first recorded day; SPY on the same days
    base_total = sum(alloc.values()) if alloc else 1.0
    spy0 = next((spy[x] for x in days if x in spy), None)
    d["paper_nav"] = {"days": [x.isoformat() for x in days],
                      "long": [round(by[(x, "long")][2] / alloc.get("long", 1) * 100, 3) if (x, "long") in by else None for x in days],
                      "insider": [round(by[(x, "insider")][2] / alloc.get("insider", 1) * 100, 3) if (x, "insider") in by else None for x in days],
                      # books present that day only (the SPY core book starts 2026-09-24), so adding a book does not jump the index
                      "total": [round(sum(by[(x, b)][2] for b in alloc if (x, b) in by) / sum(alloc[b] for b in alloc if (x, b) in by) * 100, 3) for x in days],
                      "total_auction": [round(sum(auc.get((x, b), by[(x, b)][2]) for b in alloc if (x, b) in by)
                                              / sum(alloc[b] for b in alloc if (x, b) in by) * 100, 3) if auc else None for x in days],
                      "spy": [round(spy[x] / spy0 * 100, 3) if x in spy and spy0 else None for x in days],
                      "positions": {b: [by[(x, b)][3] if (x, b) in by else None for x in days] for b in alloc}}
    d["paper_latest"] = {b: {"equity": by[(days[-1], b)][2], "since_pct": (by[(days[-1], b)][2] / alloc[b] - 1) * 100, "n": by[(days[-1], b)][3]}
                         for b in alloc if days and (days[-1], b) in by}
    d["paper_since"] = days[0].isoformat() if days else None
    d["closed_lots"] = dict(closed)
    d["fills"] = [{"book": b, "ticker": t, "side": s, "day": str(day), "time": str(ts)[11:19], "gap": round((px / m - 1) * 100 * (1 if s == "buy" else -1), 3),
                   "type": ot, "tif": tif} for b, t, s, day, ts, px, m, ot, tif in fills]
    # real account
    prev, spy_by_m = None, {}
    for m, last in spy_m:
        if prev is not None:
            spy_by_m[m] = (last / prev - 1) * 100
        prev = last
    d["real_monthly"] = {"months": [m for m, _, _ in monthly], "mine": [round(r, 2) for _, _, r in monthly],
                         "spy": [round(spy_by_m.get(m, 0.0), 2) for m, _, _ in monthly], "pnl": [round(p, 0) for _, p, _ in monthly]}
    ytd = 1.0
    for _, _, r in monthly:
        ytd *= 1 + r / 100
    spy_ytd = 1.0
    for m, _, _ in monthly:
        spy_ytd *= 1 + spy_by_m.get(m, 0) / 100
    d["real_ytd"] = {"mine": (ytd - 1) * 100, "spy": (spy_ytd - 1) * 100}
    d["real_nav"] = {"days": [str(x) for x, _, _ in rnav], "total": [round(t, 2) for _, t, _ in rnav], "cash": [round(c, 2) for _, _, c in rnav]}
    # backtests by year (S33 report carries both live books on the current data)
    files = sorted(glob.glob(os.path.join(VALID, "s33_negative_filters_*.json")))
    if files:
        j = json.load(open(files[-1]))["books"]
        lb, ib = j["long_base"], j["insider_base"]
        years = sorted(lb["by_year"])
        d["backtest"] = {"years": years, "long": [round(lb["by_year"][y] * 100, 1) for y in years],
                         "insider": [round(ib["by_year"].get(y, 0) * 100, 1) for y in years],
                         "spy": [round(lb["spy_by_year"][y] * 100, 1) for y in years],
                         "long_meta": {k: lb[k] for k in ("cagr_pct", "alpha2_ann_pct", "alpha2_t_nw", "max_drawdown_pct")},
                         "insider_meta": {k: ib[k] for k in ("cagr_pct", "alpha2_ann_pct", "alpha2_t_nw", "max_drawdown_pct")},
                         "report": os.path.basename(files[-1])}
    # lessons (30 days)
    counts: dict[str, int] = {}
    lp = os.path.join(AGENT_OUT, "lessons.jsonl")
    if os.path.exists(lp):
        cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        for line in open(lp):
            if line.strip():
                r = json.loads(line)
                if r["date"] >= cutoff:
                    counts[r["rule"]] = counts.get(r["rule"], 0) + 1
    d["lessons"] = sorted(({"rule": RULE_NAMES.get(k, k), "n": v} for k, v in counts.items()), key=lambda x: -x["n"])
    # today's positions from the latest review
    rl = os.path.join(AGENT_OUT, "review_latest.json")
    if os.path.exists(rl):
        date = json.load(open(rl))["date"]
        rp = os.path.join(AGENT_OUT, f"review_{date}.json")
        if os.path.exists(rp):
            pos = [p for p in json.load(open(rp))["paper"]["positions"] if p.get("day_ret") is not None]
            pos.sort(key=lambda p: -p["day_ret"])
            d["positions_today"] = {"date": date, "tickers": [p["ticker"] for p in pos], "book": [p["book"] for p in pos],
                                    "ret": [round(p["day_ret"], 2) for p in pos], "since": [round(p["since_entry"], 2) if p.get("since_entry") is not None else None for p in pos]}
    return d


# ------------------------------------------------------------------ page ----
JS = r"""
const css = getComputedStyle(document.documentElement);
const C = k => css.getPropertyValue(k).trim();
const F = C('--font'), M = C('--mono');
Chart.defaults.font.family = F; Chart.defaults.font.size = 12; Chart.defaults.color = C('--dim');
Chart.defaults.borderColor = C('--line'); Chart.defaults.plugins.legend.labels.boxWidth = 10;
Chart.defaults.plugins.legend.labels.boxHeight = 10; Chart.defaults.plugins.legend.labels.usePointStyle = true;
Chart.defaults.plugins.tooltip.backgroundColor = C('--surface-2'); Chart.defaults.plugins.tooltip.titleColor = C('--tx');
Chart.defaults.plugins.tooltip.bodyColor = C('--tx-2'); Chart.defaults.plugins.tooltip.borderColor = C('--line-2');
Chart.defaults.plugins.tooltip.borderWidth = 1; Chart.defaults.plugins.tooltip.padding = 8; Chart.defaults.plugins.tooltip.bodyFont = {family: M};
Chart.defaults.plugins.tooltip.titleFont = {family: F, weight: '600'};
const crosshair = {id: 'crosshair', afterDraw(ch) {
  const a = ch.tooltip?.getActiveElements?.() || []; if (!a.length) return;
  const x = a[0].element.x, y = ch.chartArea, g = ch.ctx; g.save(); g.strokeStyle = C('--line-2'); g.lineWidth = 1;
  g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, y.top); g.lineTo(x, y.bottom); g.stroke(); g.restore(); }};
const pct = v => (v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%');
const grid = {color: C('--line'), drawTicks: false};
const line = (label, data, color, dash) => ({label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
  pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: C('--surface'), tension: 0.15, spanGaps: true, borderDash: dash || []});
const bar = (label, data, color) => ({label, data, backgroundColor: color, borderColor: C('--surface'), borderWidth: 1, borderRadius: 4,
  borderSkipped: 'start', maxBarThickness: 34});
function mk(id, cfg) { const el = document.getElementById(id); if (el) new Chart(el, cfg); }
const D = window.DASH;
// 1. paper NAV
if (D.paper_nav.days.length) mk('c_nav', {type: 'line', plugins: [crosshair],
  data: {labels: D.paper_nav.days, datasets: [line('长线', D.paper_nav.long, C('--s1')), line('内部人', D.paper_nav.insider, C('--s2')),
    line('合计(模拟器口径)', D.paper_nav.total, C('--tx'), [2, 3]), line('合计(竞价口径)', D.paper_nav.total_auction, C('--s3'), [2, 3]),
    line('SPY', D.paper_nav.spy, C('--bench'), [6, 4])]},
  options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
    tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(2)}  (${pct(c.parsed.y - 100)})`}}},
    scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8}}, y: {grid, ticks: {callback: v => v.toFixed(0)}, title: {display: true, text: '指数 (起始 = 100)'}}}}});
// 2. fills gap
if (D.fills.length) mk('c_gap', {type: 'bar',
  data: {labels: D.fills.map(f => f.ticker), datasets: [{label: '对开盘偏差 %', data: D.fills.map(f => f.gap),
    backgroundColor: D.fills.map(f => f.book === 'long' ? C('--s1') : C('--s2')), borderColor: C('--surface'), borderWidth: 1, borderRadius: 4, borderSkipped: false, maxBarThickness: 18}]},
  options: {maintainAspectRatio: false, plugins: {legend: {display: false}, tooltip: {callbacks: {
    title: i => `${D.fills[i[0].dataIndex].ticker} · ${D.fills[i[0].dataIndex].book} · ${D.fills[i[0].dataIndex].day} ${D.fills[i[0].dataIndex].time}`,
    label: c => ` ${pct(c.parsed.y)}  ${D.fills[c.dataIndex].type}/${D.fills[c.dataIndex].tif.toUpperCase()}`}}},
    scales: {x: {grid: {display: false}, ticks: {autoSkip: true, maxRotation: 0, font: {family: M, size: 10}}}, y: {grid, ticks: {callback: v => v + '%'}}}}});
// 3. today's positions
if (D.positions_today) mk('c_pos', {type: 'bar',
  data: {labels: D.positions_today.tickers, datasets: [{label: '当日 %', data: D.positions_today.ret,
    backgroundColor: D.positions_today.ret.map(v => v >= 0 ? C('--div-pos') : C('--div-neg')), borderColor: C('--surface'), borderWidth: 1, borderRadius: 4, borderSkipped: false, maxBarThickness: 14}]},
  options: {indexAxis: 'y', maintainAspectRatio: false, plugins: {legend: {display: false}, tooltip: {callbacks: {
    label: c => ` 当日 ${pct(c.parsed.x)} · 入场以来 ${pct(D.positions_today.since[c.dataIndex])} · ${D.positions_today.book[c.dataIndex]}`}}},
    scales: {x: {grid, ticks: {callback: v => v + '%'}}, y: {grid: {display: false}, ticks: {font: {family: M, size: 11}, autoSkip: false}}}}});
// 4. real monthly
if (D.real_monthly.months.length) mk('c_month', {type: 'bar',
  data: {labels: D.real_monthly.months.map(m => m.slice(5) + '月'), datasets: [bar('实盘', D.real_monthly.mine, C('--s3')), bar('SPY', D.real_monthly.spy, C('--bench'))]},
  options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
    tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${pct(c.parsed.y)}` + (c.datasetIndex === 0 ? `  ($${D.real_monthly.pnl[c.dataIndex].toLocaleString()})` : '')}}},
    scales: {x: {grid: {display: false}}, y: {grid, ticks: {callback: v => v + '%'}}}}});
// 5. real NAV
if (D.real_nav.days.length > 1) mk('c_rnav', {type: 'line', plugins: [crosshair],
  data: {labels: D.real_nav.days, datasets: [line('总资产', D.real_nav.total, C('--s3')), line('现金', D.real_nav.cash, C('--bench'), [4, 3])]},
  options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
    tooltip: {callbacks: {label: c => ` ${c.dataset.label}  $${c.parsed.y.toLocaleString()}`}}},
    scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8}}, y: {grid, ticks: {callback: v => '$' + (v / 1000).toFixed(1) + 'k'}}}}});
// 6. backtest by year
if (D.backtest) mk('c_bt', {type: 'bar',
  data: {labels: D.backtest.years, datasets: [bar('长线书', D.backtest.long, C('--s1')), bar('内部人线', D.backtest.insider, C('--s2')), bar('SPY', D.backtest.spy, C('--bench'))]},
  options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
    tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${pct(c.parsed.y)}`}}},
    scales: {x: {grid: {display: false}}, y: {grid, ticks: {callback: v => v + '%'}}}}});
// 7. lessons
if (D.lessons.length) mk('c_les', {type: 'bar',
  data: {labels: D.lessons.map(l => l.rule), datasets: [{label: '30 日内次数', data: D.lessons.map(l => l.n), backgroundColor: C('--s4'),
    borderColor: C('--surface'), borderWidth: 1, borderRadius: 4, borderSkipped: 'start', maxBarThickness: 18}]},
  options: {indexAxis: 'y', maintainAspectRatio: false, plugins: {legend: {display: false}},
    scales: {x: {grid, ticks: {stepSize: 1, precision: 0}}, y: {grid: {display: false}}}}});
"""


def _table(headers: list[str], rows: list[list]) -> str:
    h = "".join(f"<th>{x}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{'' if v is None else v}</td>" for v in r) + "</tr>" for r in rows)
    return f"<details><summary>数据表</summary><div class='tbl'><table><tr>{h}</tr>{b}</table></div></details>"


def render(d: dict) -> str:
    pl = d.get("paper_latest", {})
    pn = d["paper_nav"]
    cards = ""
    for b in ("long", "insider", "core"):
        if b in pl:
            x = pl[b]
            cards += (f"<div class='card'><div class='k'>模拟盘 · {BOOK_LABEL[b]}</div><div class='v {'pos' if x['since_pct'] >= 0 else 'neg'}'>{x['since_pct']:+.2f}%</div>"
                      f"<div class='s'>${x['equity']:,.0f} · {x['n']} 仓 · 已平 {d['closed_lots'].get(b, 0)} 笔</div></div>")
    if pn["days"]:
        spy_since = pn["spy"][-1] - 100 if pn["spy"][-1] is not None else None
        cards += (f"<div class='card'><div class='k'>SPY 同期(自 {d['paper_since']})</div><div class='v'>{spy_since:+.2f}%</div><div class='s'>{len(pn['days'])} 个交易日记录</div></div>"
                  if spy_since is not None else "")
    ry = d.get("real_ytd", {})
    if ry:
        cards += (f"<div class='card'><div class='k'>实盘 2026 至今</div><div class='v {'pos' if ry['mine'] >= 0 else 'neg'}'>{ry['mine']:+.1f}%</div>"
                  f"<div class='s'>SPY 同期 {ry['spy']:+.1f}% · 按月复合</div></div>")
    fills = d["fills"]
    gap_note = ""
    if fills:
        auction = [f["gap"] for f in fills if f["type"] == "market" or f["tif"] == "opg"]
        allg = [f["gap"] for f in fills]
        gap_note = (f"{len(fills)} 笔成交,平均 {sum(allg) / len(allg):+.2f}%/边;其中竞价成交(OPG/MOO){len(auction)} 笔"
                    + (f",平均 {sum(auction) / len(auction):+.2f}%" if auction else "") + "。DAY 单是模拟器逐单撮合,不算真实成本;S27 只统计竞价成交。")
    bt = d.get("backtest")
    bt_html = ""
    if bt:
        lm, im = bt["long_meta"], bt["insider_meta"]
        bt_html = (f"<div class='chart tall'><p class='t'>两本书回测 · 按年收益 vs SPY</p><p class='st'>2017 → 2026-08,半价差成本,次日开盘入场,闲置资金在 SPY。"
                   f"长线 CAGR {lm['cagr_pct']:+.1f}%,alpha2 {lm['alpha2_ann_pct']:+.1f}%/年(t {lm['alpha2_t_nw']:.2f}),最大回撤 {lm['max_drawdown_pct']:.0f}%;"
                   f"内部人 CAGR {im['cagr_pct']:+.1f}%,alpha2 {im['alpha2_ann_pct']:+.1f}%(t {im['alpha2_t_nw']:.2f}),回撤 {im['max_drawdown_pct']:.0f}%。来源 {bt['report']}</p>"
                   f"<div class='cv'><canvas id='c_bt'></canvas></div>"
                   + _table(["年", "长线 %", "内部人 %", "SPY %"], [[y, a, b, c] for y, a, b, c in zip(bt["years"], bt["long"], bt["insider"], bt["spy"])]) + "</div>")
    pos = d.get("positions_today")
    pos_html = (f"<div class='chart tall'><p class='t'>模拟盘持仓 · {pos['date']} 当日涨跌</p><p class='st'>{len(pos['tickers'])} 个持仓,按当日收益排序;蓝色为正,红色为负。悬停看入场以来。</p>"
                f"<div class='cv' style='height:{max(240, 16 * len(pos['tickers']) + 40)}px'><canvas id='c_pos'></canvas></div></div>") if pos else ""
    rm = d["real_monthly"]
    les = d["lessons"]
    page = (site_theme.head("仪表盘 · OptRadar", charts=True) + site_theme.nav("dashboard", date=pos["date"] if pos else None)
            + f"<h1>仪表盘</h1><p class='muted'>长期走势与对照 · 生成 {d['generated_at'][:16].replace('T', ' ')} · 图可悬停,数据表在每张图下面</p>"
            + f"<div class='cards'>{cards}</div>"
            + "<h2>模拟盘</h2>"
            + f"<div class='chart tall'><p class='t'>两本书净值 vs SPY</p><p class='st'>自 {d['paper_since'] or '—'} 起,各自起点 = 100;虚线为合计(模拟器口径和按开盘竞价价重记的竞价口径)和 SPY。评估点按竞价口径判,之前只看不判。</p><div class='cv'><canvas id='c_nav'></canvas></div>"
            + _table(["日", "长线", "内部人", "合计", "SPY", "持仓 长/内"], [[x, a, b, c, s, f"{(pn['positions'].get('long') or [None]*len(pn['days']))[i]}/{(pn['positions'].get('insider') or [None]*len(pn['days']))[i]}"]
                                                                          for i, (x, a, b, c, s) in enumerate(zip(pn["days"], pn["long"], pn["insider"], pn["total"], pn["spy"]))]) + "</div>"
            + f"<div class='grid2'><div class='chart'><p class='t'>每笔成交对开盘价的偏差</p><p class='st'>{gap_note or '还没有成交'}</p><div class='cv'><canvas id='c_gap'></canvas></div>"
            + "<p class='legend'><i style='background:var(--s1)'></i>长线 <i style='background:var(--s2)'></i>内部人</p></div>"
            + pos_html + "</div>"
            + "<h2>实盘(moomoo,只读)</h2>"
            + f"<div class='grid2'><div class='chart'><p class='t'>2026 月度收益 vs SPY</p><p class='st'>1–8 月来自 App 收益日历,当月按每日快照;入金未剔除。</p><div class='cv'><canvas id='c_month'></canvas></div>"
            + _table(["月", "实盘 %", "SPY %", "盈亏 $"], [[m, a, b, f"{p:+,.0f}"] for m, a, b, p in zip(rm["months"], rm["mine"], rm["spy"], rm["pnl"])]) + "</div>"
            + f"<div class='chart'><p class='t'>总资产(每日快照)</p><p class='st'>自 {d['real_nav']['days'][0] if d['real_nav']['days'] else '—'} 起,16:25 PT 快照。</p><div class='cv'><canvas id='c_rnav'></canvas>"
            + ("" if len(d['real_nav']['days']) > 1 else "<p class='muted'>快照满两天后出现折线</p>") + "</div></div></div>"
            + "<h2>回测(对照基准)</h2>" + (bt_html or "<p class='muted'>没有回测报告</p>")
            + "<h2>教训计数(30 日)</h2>"
            + (f"<div class='chart short'><p class='t'>触发的规则</p><p class='st'>来自每日复盘;同一条规则反复出现就是要改的地方。</p><div class='cv'><canvas id='c_les'></canvas></div></div>" if les
               else "<p class='muted'>30 日内没有触发过规则</p>")
            + f"<script>window.DASH = {json.dumps(d, ensure_ascii=False, default=str)};</script><script>{JS}</script>"
            + site_theme.FOOT)
    return page


def main() -> int:
    d = collect()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "dashboard_data.json"), "w") as f:
        json.dump(d, f, ensure_ascii=False, default=str)
    with open(os.path.join(OUT, "dashboard.html"), "w") as f:
        f.write(render(d))
    print(f"dashboard: {len(d['paper_nav']['days'])} nav days, {len(d['fills'])} fills, {len(d['real_monthly']['months'])} months, lessons {len(d['lessons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
