#!/usr/bin/env python
"""大师信号存档页(2026-09-23 停止更新)— site/public/masters/。公开站首页是模拟盘(build_paper.py)。

铁律:不写入任何持仓、金额、盈亏或具体期权结构(那些只进私有层)。
输入: site-data/masters/<date>.json (大师信号)
      /Users/louis/optradar/out/<date>.json (仅取 anomalies 的市场事实)
输出: site/public/index.html + site/public/<date>.html + archive 索引
"""
from __future__ import annotations

import datetime as dt
import glob
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTERS = os.path.join(ROOT, "site-data", "masters")
RADAR = "/Users/louis/optradar/out"
OUT = os.path.join(ROOT, "site", "public", "masters")   # archive; the public home page is the paper portfolio (build_paper.py)

PERSONA_CN = {"buffett": "Buffett", "munger": "Munger", "graham": "Graham",
              "lynch": "Lynch", "druckenmiller": "Druckenmiller"}

CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--tx:#e6e8ec;--dim:#9aa3b2;
--bull:#3fb950;--bear:#f85149;--neut:#8b949e;--acc:#58a6ff}
*{box-sizing:border-box;min-width:0}
html,body{overflow-x:hidden;max-width:100%}
body{margin:0;background:var(--bg);color:var(--tx);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--dim);font-size:13px}
h2{font-size:16px;margin:36px 0 14px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:16px;background:var(--acc);border-radius:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin-bottom:10px;overflow:hidden}
.row{display:flex;align-items:center;gap:14px;padding:14px 16px;cursor:pointer;
user-select:none}
.row:hover{background:#1c2029}
.tk{font-weight:600;font-size:16px;min-width:64px;letter-spacing:-.01em}
.bar{flex:1;height:6px;background:#21262d;border-radius:3px;position:relative;
min-width:80px}
.bar i{position:absolute;top:0;height:6px;border-radius:3px}
.bar u{position:absolute;left:50%;top:-3px;width:1px;height:12px;background:#3d444d}
.val{font-variant-numeric:tabular-nums;font-weight:600;min-width:56px;text-align:right}
.tag{font-size:11px;color:var(--dim);border:1px solid var(--line);border-radius:4px;
padding:2px 6px;white-space:nowrap}
.votes{font-size:12px;color:var(--dim);min-width:74px;text-align:right;
font-variant-numeric:tabular-nums}
.detail{display:none;border-top:1px solid var(--line);padding:4px 16px 14px;
background:#13161c}
.card.open .detail{display:block}
.op{padding:12px 0;border-bottom:1px solid #1e222a}
.op:last-child{border-bottom:0}
.oph{display:flex;gap:10px;align-items:center;margin-bottom:5px}
.nm{font-weight:600;font-size:13px}
.sc{font-size:12px;font-variant-numeric:tabular-nums}
.rz{color:var(--dim);font-size:13px;line-height:1.65}
.bull{color:var(--bull)}.bear{color:var(--bear)}.neut{color:var(--neut)}
.bull-bg{background:var(--bull)}.bear-bg{background:var(--bear)}.neut-bg{background:var(--neut)}
ul.an{list-style:none;padding:0;margin:0}
ul.an li{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:11px 14px;margin-bottom:8px;font-size:14px}
ul.an li b{color:var(--acc);font-weight:600;font-size:12px;margin-right:8px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px;line-height:1.8}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.arch{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.arch a{font-size:12px;border:1px solid var(--line);border-radius:5px;padding:4px 9px;
color:var(--dim)}
.arch a:hover{color:var(--tx);border-color:var(--acc);text-decoration:none}
.empty{color:var(--dim);font-size:14px}
svg.nav{width:100%;height:150px;display:block;margin:6px 0 2px}
svg.nav .zero{stroke:#30363d;stroke-dasharray:3 3;stroke-width:1}
.card.bt .row:hover{background:none}
.hist{padding:10px 0 4px;border-bottom:1px solid #1e222a;margin-bottom:4px}
.hl{font-size:11px;color:var(--dim);margin-bottom:2px}
svg.spark{width:100%;height:56px;display:block}
svg.spark .zero{stroke:#30363d;stroke-dasharray:3 3;stroke-width:1}
.chart{margin-top:2px}
.axl{display:flex;justify-content:space-between;font-size:11px;color:#6e7681;
margin-top:2px;font-variant-numeric:tabular-nums}
.axl .bull{color:var(--bull)}.axl .bear{color:var(--bear)}.axl .neut{color:var(--neut)}
.empty2{display:none}
@media(max-width:560px){
 .wrap{padding:20px 12px 60px;max-width:100%}
 h1{font-size:19px}
 .sub{font-size:12px}
 .row{flex-wrap:wrap;gap:6px 10px;padding:12px 13px}
 .tk{min-width:0;flex:1 1 auto;font-size:15px}
 .val{min-width:0;flex:0 0 auto;font-size:15px}
 .bar{order:5;flex:1 0 100%;min-width:0;margin:2px 0}
 .votes{order:6;min-width:0;flex:1 1 auto;text-align:left;font-size:11px}
 .tag{order:7;flex:0 0 auto;font-size:10px}
 .detail{padding:4px 13px 12px}
 .rz{font-size:13.5px}
 .oph{flex-wrap:wrap}
}
"""

JS = """
document.querySelectorAll('.row').forEach(r=>r.addEventListener('click',
 ()=>r.parentElement.classList.toggle('open')));
"""


def load_backtests(limit: int = 6) -> list:
    """Archived backtest results, newest file first."""
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, "site-data", "backtests", "*.json")),
                    key=os.path.getmtime, reverse=True)[:limit]:
        try:
            d = json.load(open(f))
        except (ValueError, OSError):
            continue
        base = os.path.basename(f)[:-5].split("_")
        d["personas"] = base[1].replace("-", " + ") if len(base) > 1 else "?"
        out.append(d)
    return out


def history_series() -> dict:
    """{ticker: [(date, consensus, disagreement)]} across every archived day."""
    series: dict[str, list] = {}
    for f in sorted(glob.glob(os.path.join(MASTERS, "*.json"))):
        d = os.path.basename(f)[:10]
        try:
            data = json.load(open(f))
        except (ValueError, OSError):
            continue
        for t in data.get("tickers", []):
            if t.get("consensus") is None:
                continue
            series.setdefault(t["ticker"], []).append(
                (d, t["consensus"], t.get("disagreement") or 0.0))
    return series


def spark(points: list, w: int = 300, h: int = 56) -> str:
    """Consensus-over-time line. The SVG stretches to the container width, so
    all text lives in HTML around it — text inside a non-uniformly scaled
    viewBox gets distorted."""
    if len(points) < 2:
        return ""
    pad = 4
    inner_w, inner_h = w - pad * 2, h - pad * 2
    n = len(points)

    def xy(i, v):
        x = pad + (inner_w * i / (n - 1))
        y = pad + inner_h * (1 - (max(-1.0, min(1.0, v)) + 1) / 2)
        return x, y

    coords = [xy(i, v) for i, (_, v, _) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                    for i, (x, y) in enumerate(coords))
    zero_y = pad + inner_h / 2
    last_v = points[-1][1]
    dots = "".join(
        f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3' fill='var(--{cls(points[i][1])})'"
        f" vector-effect='non-scaling-stroke'/>"
        for i, (x, y) in enumerate(coords))
    svg = (f"<svg class='spark' viewBox='0 0 {w} {h}' preserveAspectRatio='none'>"
           f"<line x1='{pad}' y1='{zero_y}' x2='{pad + inner_w}' y2='{zero_y}' class='zero'/>"
           f"<path d='{path}' fill='none' stroke='var(--{cls(last_v)})' stroke-width='2'"
           f" stroke-linejoin='round' vector-effect='non-scaling-stroke'/>{dots}</svg>")
    return (f"<div class='chart'>{svg}"
            f"<div class='axl'><span>{points[0][0][5:]}</span>"
            f"<span class='{cls(last_v)}'>最新 {last_v:+.2f}</span>"
            f"<span>{points[-1][0][5:]}</span></div></div>")


def nav_chart(dates: list, nav: list, bench: list, w: int = 600, h: int = 150) -> str:
    """Fund NAV vs benchmark, normalised to 100. Same hand-drawn approach as
    the sparkline: text stays in HTML so nothing distorts when it stretches."""
    if len(nav) < 2:
        return ""
    base_n, base_b = nav[0] or 1, (bench[0] if bench else 1) or 1
    ns = [v / base_n * 100 for v in nav]
    bs = [v / base_b * 100 for v in (bench or [])] or None
    pool = ns + (bs or [])
    lo, hi = min(pool), max(pool)
    span = (hi - lo) or 1
    pad = 8
    iw, ih = w - pad * 2, h - pad * 2

    def path_of(vals):
        pts = []
        n = len(vals)
        for i, v in enumerate(vals):
            x = pad + iw * i / (n - 1)
            y = pad + ih * (1 - (v - lo) / span)
            pts.append(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}")
        return " ".join(pts)

    hundred_y = pad + ih * (1 - (100 - lo) / span)
    out = [f"<svg class='nav' viewBox='0 0 {w} {h}' preserveAspectRatio='none'>",
           f"<line x1='{pad}' y1='{hundred_y:.1f}' x2='{pad+iw}' y2='{hundred_y:.1f}' class='zero'/>"]
    if bs:
        out.append(f"<path d='{path_of(bs)}' fill='none' stroke='#6e7681' "
                   f"stroke-width='1.5' stroke-dasharray='4 3' vector-effect='non-scaling-stroke'/>")
    final = ns[-1]
    color = "bull" if final >= 100 else "bear"
    out.append(f"<path d='{path_of(ns)}' fill='none' stroke='var(--{color})' "
               f"stroke-width='2' vector-effect='non-scaling-stroke'/></svg>")
    return "".join(out)


def e(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def cls(v: float) -> str:
    return "bull" if v > 0.15 else "bear" if v < -0.15 else "neut"


def bar(v: float) -> str:
    """Conviction bar centred at zero, -1..+1."""
    pct = max(-1.0, min(1.0, v)) * 50
    if v >= 0:
        style = f"left:50%;width:{pct:.1f}%"
    else:
        style = f"left:{50 + pct:.1f}%;width:{-pct:.1f}%"
    return f'<div class="bar"><u></u><i class="{cls(v)}-bg" style="{style}"></i></div>'


def render(date: str, masters: dict | None, anomalies: list,
           dates: list[str], series: dict, backtests: list) -> str:
    p = [f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<meta name='robots' content='noindex'>",
         f"<title>AI 对冲基金 · 大师信号 {date}</title>",
         f"<style>{CSS}</style></head><body><div class='wrap'>",
         "<header><p style='margin:0 0 10px'><a href='../index.html'>← 模拟盘 / Paper portfolio</a></p>",
         "<div style='border:1px solid #d29922;background:rgba(210,153,34,.10);border-radius:8px;padding:10px 14px;margin:0 0 16px;font-size:13px;line-height:1.6'>"
         "<b>已停止 · Discontinued 2026-09-23.</b> 这组 LLM 大师信号经检验没有预测力:5 日方向命中 55%,低于“永远猜多数方向”的 70%;"
         "五位大师的误差几乎完全相关,等于一个人。页面只作存档。<br>"
         "These LLM persona signals had no predictive value: 55% directional hits at 5 days versus 70% for always guessing the majority direction, "
         "and the five personas' errors were almost perfectly correlated. Kept as an archive only.</div>",
         "<h1>AI 对冲基金 · 大师信号(存档)</h1>",
         f"<div class='sub'>{date} · 5 位投资大师 persona 独立评估 · "
         "数据 moomoo OpenD,判断由 Claude 生成</div></header>"]

    p.append("<h2>共识与分歧</h2>")
    if masters and masters.get("tickers"):
        p.append("<p class='sub' style='margin:-6px 0 14px'>"
                 "共识 = 五家平均 conviction(−1 空 ↔ +1 多);按分歧从大到小排,"
                 "<b>分歧越大越值得读</b>。点击展开各家理由。</p>")
        for t in masters["tickers"]:
            c = t.get("consensus")
            if c is None:
                continue
            votes = [s for s in t["signals"] if not s.get("abstained")]
            p.append("<div class='card'><div class='row'>")
            p.append(f"<span class='tk'>{e(t['ticker'])}</span>")
            p.append(bar(c))
            p.append(f"<span class='val {cls(c)}'>{c:+.2f}</span>")
            p.append(f"<span class='votes'>{t['bulls']}多 {t['bears']}空</span>")
            p.append(f"<span class='tag'>分歧 {t['disagreement']:.2f}</span>")
            p.append("</div><div class='detail'>")
            hist = series.get(t["ticker"], [])
            if len(hist) >= 2:
                p.append(f"<div class='hist'><div class='hl'>共识走势 "
                         f"({len(hist)} 日)</div>{spark(hist)}</div>")
            for s in sorted(votes, key=lambda x: -x["value"]):
                cn, en = s.get("reasoning_cn"), s.get("reasoning") or ""
                body = f"<div class='rz'>{e(cn or en)}</div>"
                if cn and en:  # keep the original one click away
                    body += (f"<details class='orig'><summary>原文</summary>"
                             f"<div class='rz en'>{e(en)}</div></details>")
                p.append("<div class='op'><div class='oph'>"
                         f"<span class='nm'>{PERSONA_CN.get(s['persona'], s['persona'])}</span>"
                         f"<span class='sc {cls(s['value'])}'>{s['value']:+.2f}</span></div>"
                         f"{body}</div>")
            p.append("</div></div>")
    else:
        p.append("<p class='empty'>今日无信号。</p>")

    p.append("<h2>市场异动</h2>")
    if anomalies:
        p.append("<ul class='an'>")
        for a in anomalies:
            p.append(f"<li><b>{e(a.get('kind', ''))}</b>{e(a.get('msg', ''))}</li>")
        p.append("</ul>")
    else:
        p.append("<p class='empty'>今日无触发。</p>")

    if backtests:
        p.append("<h2>回测</h2>")
        p.append("<p class='sub' style='margin:-6px 0 14px'>同一条 run_cycle 在历史上"
                 "回放,数据为 point-in-time(不含未来财报)。实线 = 策略净值,"
                 "虚线 = 基准。</p>")
        for b in backtests:
            m = b["metrics"]
            tot, exc = m["total_return_pct"], m["excess_return_pct"]
            p.append("<div class='card bt'><div class='row' style='cursor:default'>"
                     f"<span class='tk'>{e('+'.join(b['universe']))}</span>"
                     f"<span class='tag'>{e(b['personas'])}</span>"
                     f"<span class='val {cls(tot)}'>{tot:+.1%}</span>"
                     f"<span class='votes'>基准 {m['benchmark_return_pct']:+.1%}</span>"
                     f"<span class='tag'>超额 {exc:+.1%}</span></div>"
                     "<div class='detail' style='display:block'>"
                     f"{nav_chart(b['dates'], b['nav'], b.get('benchmark_nav') or [])}"
                     f"<div class='axl'><span>{e(b['start'])}</span>"
                     f"<span>回撤 {m['max_drawdown_pct']:.1%} · 夏普 "
                     f"{m['sharpe_ratio']:.2f} · {m['n_cycles']} 周期</span>"
                     f"<span>{e(b['end'])}</span></div></div></div>")

    if dates:
        p.append("<h2>历史</h2><div class='arch'>")
        for d in dates[:30]:
            p.append(f"<a href='{d}.html'>{d}</a>")
        p.append("</div>")

    p.append(
        "<footer><b>免责声明</b><br>"
        "本页为个人研究工具的自动输出,由 LLM 扮演投资大师风格生成,"
        "<b>不构成投资建议</b>,不代表任何真实人物的观点。"
        "作者可能持有页内标的。据此操作风险自担。<br>"
        f"生成于 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        "行情数据 moomoo OpenD</footer>")
    p.append(f"</div><script>{JS}</script></body></html>")
    return "".join(p)


def main() -> int:
    """Build a page for every date that has master data, plus index.html.

    Archive links must resolve — generating only today's page left every
    historical link 404ing.
    """
    want = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()

    files = sorted(glob.glob(os.path.join(MASTERS, "*.json")), reverse=True)
    if not files:
        print("no master data — nothing to build")
        return 1
    dates = [os.path.basename(f)[:10] for f in files]

    series = history_series()
    backtests = load_backtests()
    os.makedirs(OUT, exist_ok=True)
    newest = dates[0]
    latest_date = want if want in dates else newest

    built = 0
    for f, d in zip(files, dates):
        masters = json.load(open(f))
        radar_path = os.path.join(RADAR, f"{d}.json")
        anomalies = []
        if os.path.exists(radar_path):
            try:
                anomalies = json.load(open(radar_path)).get("anomalies", [])
            except (ValueError, OSError):
                anomalies = []
        page = render(d, masters, anomalies, dates, series, backtests)
        with open(os.path.join(OUT, f"{d}.html"), "w") as fh:
            fh.write(page)
        if d == latest_date:
            with open(os.path.join(OUT, "index.html"), "w") as fh:
                fh.write(page)
        built += 1

    n = len(json.load(open(files[0]))["tickers"])
    print(f"built {built} page(s) in {OUT}  (index -> {latest_date}, {n} tickers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
