"""Dashboard: the long-run pictures, interactive (Chart.js), private site only.

  模拟盘   each book since the close before its first fill vs SPY and QQQ on the same days
           (agent/evaluate.baselines, adj_close, S48), the three bases labelled, the average
           invested share next to every return; every fill's gap to the opening cross (the
           auction basis, agent/auction_basis.py) by order type; today's positions by day return;
           the evaluation's progress as counts only (agent/evaluate.py; no alpha, no t)
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
import json
import os
import sys

from agent import evaluate, ledger
from agent.auction_basis import OUT as AUCTION_JSON
from hedge_fund.features.panel import PanelStore

sys.path.insert(0, "/Users/louis/optradar/bin")
import site_theme  # noqa: E402

OUT = "/Users/louis/optradar/out"
AGENT_OUT = os.path.join(OUT, "agent")
VALID = "/Users/louis/hedge-fund/site-data/validation"
EVAL_JSON = os.path.join(AGENT_OUT, "evaluate.json")
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
        fills = con.execute("""SELECT book, ticker, side, CAST(filled_at AS DATE), filled_at, filled_avg_px, model_px, order_type, tif
                               FROM agent_orders WHERE filled_qty > 0 AND dry_run = FALSE ORDER BY filled_at""").fetchall()
        monthly = con.execute("SELECT month, pnl_usd, ret_pct FROM acct_monthly ORDER BY month").fetchall()
        rnav = con.execute("SELECT date, sum(total_assets), sum(cash) FROM acct_nav GROUP BY 1 ORDER BY 1").fetchall()
        closed = con.execute("SELECT book, count(*) FROM agent_lots WHERE status = 'closed' GROUP BY 1").fetchall()
        with PanelStore(read_only=True) as store:
            base = evaluate.page_baselines(con, store)
            spy_m = store.con.execute("""SELECT strftime(trade_date, '%Y-%m'), last(adj_close ORDER BY trade_date) FROM index_daily
                                         WHERE symbol = 'SPY' AND trade_date >= '2025-12-01' GROUP BY 1 ORDER BY 1""").fetchall()
            bt_bench = evaluate.backtest_bench(store, VALID)
    finally:
        con.close()
    d.update(paper_block(base, nav))
    d["closed_lots"] = dict(closed)
    d["fills"] = evaluate.fill_gaps(fills, _auction_rows())
    d["progress"] = progress_counts()
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
    if bt_bench:
        d["backtest"] = bt_bench
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


# ------------------------------------------------------------------ helpers ----
def _auction_rows(path: str | None = None) -> list[dict]:
    try:
        return json.load(open(path or AUCTION_JSON)).get("fill_rows", [])
    except Exception:
        return []


def paper_block(base: dict, nav: list) -> dict:
    """The paper part of dashboard_data.json from agent.evaluate.baselines: each book from the close before its
    first fill, SPY / QQQ on adj_close from the same close; the combined index chains daily returns."""
    c = base.get("combined")
    if not c:
        return {"baselines": base, "paper_nav": {"days": [], "long": [], "insider": [], "core": [], "total": [], "total_auction": [],
                                                 "spy": [], "qqq": [], "positions": {}}, "paper_since": None}
    days = [dt.date.fromisoformat(x) for x in c["days"]]
    pos = {(r[0], r[1]): r[3] for r in nav}
    ser = c["series"]
    blank = [None] * len(days)
    return {"baselines": base, "paper_since": c["base_day"],
            "paper_nav": {"days": c["days"], "basis": "sim_tr",
                          "long": (ser.get("long") or {}).get("sim_tr", blank), "insider": (ser.get("insider") or {}).get("sim_tr", blank),
                          "core": (ser.get("core") or {}).get("sim_tr", blank),
                          "total": c["index"]["sim_tr"], "total_auction": c["index"]["auction_tr"],
                          "spy": c["bench"]["SPY"], "qqq": c["bench"]["QQQ"],
                          "positions": {b: [pos.get((x, b)) for x in days] for b in c["books"]}}}


def progress_counts(path: str | None = None) -> dict | None:
    """Counts from agent/evaluate.py's file: closed lots against the target. No alpha, no t (config.yaml: no verdict before the point)."""
    try:
        e = json.load(open(path or EVAL_JSON))
    except Exception:
        return None
    fr = e.get("fill_rate", {})
    return {"evaluate_from": e.get("evaluate_from"), "progress": e.get("progress", {}),
            "orders": {k: {"n": v["n"], "n_filled": v["n_filled"]} for k, v in fr.items()},
            "insider_lots": e.get("insider_entries", {}).get("lots", {})}


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
// 1. paper NAV: each book from the close before its first fill; SPY / QQQ adj_close from the first book's start
if (D.paper_nav.days.length) mk('c_nav', {type: 'line', plugins: [crosshair],
  data: {labels: D.paper_nav.days, datasets: [line('长线', D.paper_nav.long, C('--s1')), line('内部人', D.paper_nav.insider, C('--s2')),
    line('SPY 核心仓', D.paper_nav.core, C('--s4')),
    line('合计(模拟器口径含分红)', D.paper_nav.total, C('--tx'), [2, 3]), line('合计(竞价口径含分红)', D.paper_nav.total_auction, C('--s3'), [2, 3]),
    line('SPY', D.paper_nav.spy, C('--bench'), [6, 4]), line('QQQ', D.paper_nav.qqq, C('--s7'), [3, 3])]},
  options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false}, plugins: {legend: {position: 'top', align: 'end'},
    tooltip: {callbacks: {label: c => ` ${c.dataset.label}  ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(2)}  (${pct(c.parsed.y - 100)})`}}},
    scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8}}, y: {grid, ticks: {callback: v => v.toFixed(0)}, title: {display: true, text: '指数(各书首笔成交前一个收盘 = 100)'}}}}});
// 2. fills: gap to the opening cross (the auction basis), by book; fills without a cross comparison are left out
const FG = D.fills.filter(f => f.gap != null), KIND = {opg: '开盘竞价单', day_market: 'DAY 市价单', day_limit: 'DAY 限价单'};
const BC = {long: C('--s1'), insider: C('--s2'), core: C('--s4')};
if (FG.length) mk('c_gap', {type: 'bar',
  data: {labels: FG.map(f => f.ticker), datasets: [{label: '对开盘竞价价 %', data: FG.map(f => f.gap),
    backgroundColor: FG.map(f => BC[f.book] || C('--dim')), borderColor: C('--surface'), borderWidth: 1, borderRadius: 4, borderSkipped: false, maxBarThickness: 18}]},
  options: {maintainAspectRatio: false, plugins: {legend: {display: false}, tooltip: {callbacks: {
    title: i => `${FG[i[0].dataIndex].ticker} · ${FG[i[0].dataIndex].book} · ${FG[i[0].dataIndex].day} ${FG[i[0].dataIndex].time}`,
    label: c => ` ${pct(c.parsed.y)}  ${FG[c.dataIndex].side === 'buy' ? '买' : '卖'} · ${KIND[FG[c.dataIndex].kind]}`}}},
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
  data: {labels: D.backtest.years, datasets: [bar('长线书', D.backtest.long, C('--s1')), bar('内部人线', D.backtest.insider, C('--s2')), bar('SPY', D.backtest.spy, C('--bench')),
    bar('QQQ', D.backtest.qqq, C('--s7'))]},
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


def _p(v, nd=2) -> str:
    return "—" if v is None else f"<span class='{'pos' if v >= 0 else 'neg'}'>{v:+.{nd}f}%</span>"


def book_cards(base: dict, closed: dict) -> str:
    """One card per book and one for the total: the return since the close before the first fill on the simulator basis
    with dividends, and on the same line the auction basis, SPY, QQQ over the same days and the average invested share."""
    cards = ""
    for b, x in base.get("books", {}).items():
        r = x["ret"]
        cards += (f"<div class='card'><div class='k'>模拟盘 · {x['label']} · 自 {x['base_day']} 收盘</div>"
                  f"<div class='v {'pos' if (r['sim_tr'] or 0) >= 0 else 'neg'}'>{r['sim_tr']:+.2f}%</div>"
                  f"<div class='s'>模拟器口径含分红;竞价口径含分红 {_p(r['auction_tr'])}</div>"
                  f"<div class='s'>同期 SPY {_p(x['bench']['SPY'])} · QQQ {_p(x['bench']['QQQ'])}(adj_close,含分红)</div>"
                  f"<div class='s'>平均仓位 {'—' if x['exposure_avg_pct'] is None else format(x['exposure_avg_pct'], '.0f') + '%'} · "
                  f"首笔成交 {x['first_fill']} · {x['n_sessions']} 个交易日 · 已平 {closed.get(b, 0)} 笔</div></div>")
    c = base.get("combined")
    if c:
        cards += (f"<div class='card'><div class='k'>{len(c['books'])} 本书合计 · 自 {c['base_day']} 收盘</div>"
                  f"<div class='v {'pos' if c['ret']['sim_tr'] >= 0 else 'neg'}'>{c['ret']['sim_tr']:+.2f}%</div>"
                  f"<div class='s'>按日收益连乘,模拟器口径含分红;竞价口径含分红 {_p(c['ret']['auction_tr'])}</div>"
                  f"<div class='s'>同期 SPY {_p(c['bench_ret']['SPY'])} · QQQ {_p(c['bench_ret']['QQQ'])}</div>"
                  f"<div class='s'>平均仓位 {'—' if c['exposure_avg_pct'] is None else format(c['exposure_avg_pct'], '.0f') + '%'}</div></div>")
    return cards


def gap_summary(fills: list[dict]) -> str:
    """Gap to the opening cross, split by order kind: only the OPG orders fill in the cross; DAY orders are the simulator's."""
    if not fills:
        return ""
    ok = [f for f in fills if f["gap"] is not None]
    since = min(f["day"] for f in fills)
    parts = []
    for k, zh in (("opg", "开盘竞价单(OPG)"), ("day_market", "DAY 市价单"), ("day_limit", "DAY 限价单")):
        g = [f["gap"] for f in ok if f["kind"] == k]
        if g:
            parts.append(f"{zh} {len(g)} 笔 {sum(g) / len(g):+.2f}%")
    n_gf = sum(1 for f in fills if f["gap_fill"])
    n_nc = len(fills) - len(ok) - n_gf
    return (f"对开盘竞价价(评估用的定义):自 {since} 起 {len(ok)} 笔,平均 {sum(f['gap'] for f in ok) / len(ok):+.2f}%/边;" if ok else "")\
        + ";".join(parts) + "。" \
        + (f"开盘价高于限价、盘中回落才成交的 {n_gf} 笔不比较。" if n_gf else "") \
        + (f"没有竞价价的 {n_nc} 笔不比较。" if n_nc else "") \
        + "只有 OPG 单在开盘竞价成交;DAY 单是模拟器在开盘后逐单撮合的。"


def progress_html(p: dict | None) -> str:
    """Counts only: config.yaml says no verdict before the evaluation point, so no return, alpha or t here."""
    if not p:
        return ""
    items = []
    for b, x in p.get("progress", {}).items():
        extra = f",其中按时入场 {x['on_time'] or 0}" if b == "insider" else ""
        items.append(f"{BOOK_LABEL.get(b, b)} 平仓 {x['closed']}/{x['target']} 笔{extra}(或 {x['or_date']})")
    fr = p.get("orders", {})
    if fr:
        short = {"long": "长线", "insider": "内部人", "core": "核心仓"}
        items.append("订单成交 " + ",".join(f"{short.get(k.split('/')[0], k.split('/')[0])} {'买' if k.endswith('/buy') else '卖'} {v['n_filled']}/{v['n']}"
                                         for k, v in fr.items()))
    return (f"<p class='muted'>评估进度(自 {p.get('evaluate_from')} 起下单的入场;只计数,评估点之前不下判决):"
            + " · ".join(items) + "</p>")


def render(d: dict) -> str:
    pn = d["paper_nav"]
    base = d.get("baselines", {})
    cards = book_cards(base, d.get("closed_lots", {}))
    ry = d.get("real_ytd", {})
    if ry:
        cards += (f"<div class='card'><div class='k'>实盘 2026 至今</div><div class='v {'pos' if ry['mine'] >= 0 else 'neg'}'>{ry['mine']:+.1f}%</div>"
                  f"<div class='s'>SPY 同期 {ry['spy']:+.1f}% · 按月复合</div></div>")
    gap_note = gap_summary(d["fills"])
    bt = d.get("backtest")
    bt_html = ""
    if bt:
        lm, im = bt["long_meta"], bt["insider_meta"]
        q = "—" if bt.get("qqq_cagr_pct") is None else f"{bt['qqq_cagr_pct']:+.1f}%"
        bt_html = (f"<div class='chart tall'><p class='t'>两本书回测 · 按年收益 vs SPY、QQQ</p><p class='st'>{bt['start']} → {bt['end']},半价差成本,次日开盘入场,闲置资金在 SPY(模拟盘的闲置资金是现金)。"
                   f"长线 CAGR {lm['cagr_pct']:+.1f}%,SPY {lm['spy_cagr_pct']:+.1f}%,QQQ {q};对 SPY 超额 {lm['excess_cagr_pct']:+.1f}%/年(主动收益 t {lm['active_t_nw']:.2f}),"
                   f"夏普 {lm['sharpe']:.2f},最大回撤 {lm['max_drawdown_pct']:.0f}%;"
                   f"内部人 CAGR {im['cagr_pct']:+.1f}%,对 SPY 超额 {im['excess_cagr_pct']:+.1f}%/年(t {im['active_t_nw']:.2f}),回撤 {im['max_drawdown_pct']:.0f}%。"
                   f"QQQ 为同窗口 adj_close。来源 {bt['report']}</p>"
                   f"<div class='cv'><canvas id='c_bt'></canvas></div>"
                   + _table(["年", "长线 %", "内部人 %", "SPY %", "QQQ %"], [[y, a, b, c, q_] for y, a, b, c, q_ in zip(bt["years"], bt["long"], bt["insider"], bt["spy"], bt["qqq"])]) + "</div>")
    pos = d.get("positions_today")
    pos_html = (f"<div class='chart tall'><p class='t'>模拟盘持仓 · {pos['date']} 当日涨跌</p><p class='st'>{len(pos['tickers'])} 个持仓,按当日收益排序;蓝色为正,红色为负。悬停看入场以来。</p>"
                f"<div class='cv' style='height:{max(240, 16 * len(pos['tickers']) + 40)}px'><canvas id='c_pos'></canvas></div></div>") if pos else ""
    rm = d["real_monthly"]
    les = d["lessons"]
    page = (site_theme.head("仪表盘 · OptRadar", charts=True) + site_theme.nav("dashboard", date=pos["date"] if pos else None)
            + f"<h1>仪表盘</h1><p class='muted'>长期走势与对照 · 生成 {d['generated_at'][:16].replace('T', ' ')} · 图可悬停,数据表在每张图下面</p>"
            + f"<div class='cards'>{cards}</div>"
            + "<h2>模拟盘</h2>"
            + progress_html(d.get("progress"))
            + f"<div class='chart tall'><p class='t'>三本书净值 vs SPY、QQQ</p><p class='st'>每本书从首笔成交前一个收盘起 = 100(长线、内部人、核心仓各自的起点见上面的卡片);"
              f"SPY、QQQ 自 {d['paper_since'] or '—'} 收盘起 = 100,用 adj_close(含分红)。书的线是模拟器口径含分红(模拟盘不发分红,按持仓补记,不进现金);"
              "虚线合计按日收益连乘,另一条是竞价口径(每笔成交按当天开盘竞价价重记、扣监管费、含分红)。评估点按竞价口径判,之前只看不判。</p><div class='cv'><canvas id='c_nav'></canvas></div>"
            + _table(["日", "长线", "内部人", "核心仓", "合计", "合计(竞价)", "SPY", "QQQ"],
                     [list(r) for r in zip(pn["days"], pn["long"], pn["insider"], pn["core"], pn["total"], pn["total_auction"], pn["spy"], pn["qqq"])]) + "</div>"
            + f"<div class='grid2'><div class='chart'><p class='t'>每笔成交对开盘竞价价的偏差</p><p class='st'>{gap_note or '还没有成交'}</p><div class='cv'><canvas id='c_gap'></canvas></div>"
            + "<p class='legend'><i style='background:var(--s1)'></i>长线 <i style='background:var(--s2)'></i>内部人 <i style='background:var(--s4)'></i>SPY 核心仓</p></div>"
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
