"""Week-one summary (2026-09-21 → 09-25) for the private site: out/weekly/<date>-第一周总结.html.

Numbers come from the ledger and the panel at run time; the experiment table and the lessons are
the week's record (docs/AGENT_PLAN.md §9 S27–S42b). Private only: it shows the real account.

Usage: python -m agent.week_summary [--date 2026-09-26]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import duckdb

from hedge_fund.paths import AGENT_DIR

sys.path.insert(0, "/Users/louis/optradar/bin")
import site_theme  # noqa: E402

OUT = "/Users/louis/optradar/out"
DB = "/Users/louis/optradar/optradar.db"
START, END, PREV = "2026-09-21", "2026-09-25", "2026-09-18"

EXPERIMENTS = [  # id, question, result, verdict (ok = adopted, no = rejected, wait = deferred)
    ("S33", "负面事件(大跌、增发等)做入场否决", "被否决的名字没有显著跑输;长线「5 日内大动不进」有点用但没过直接检验", "no", "不采用"),
    ("S34", "8-K 回购公告做一条事件线", "只有公告当天 +0.12%,次日开盘进场吃不到", "no", "否定"),
    ("S36", "Alpaca 模拟盘的开盘竞价单(OPG)", "模拟盘不模拟竞价,OPG 单大量过期", "ok", "改用 DAY 单"),
    ("S36b", "没成交的内部人入场单", "「下过单就跳过」把从未成交的也挡住了", "ok", "重试一次"),
    ("S37", "大盘股选股(9 个规则)", "最好的 lc_mom t = 0.87,都不显著", "no", "不采用;lc_qlv 影子记录"),
    ("S27", "执行成本(模拟器口径 0.6%/边)", "长线 alpha 从 +8.1%/年 降到 +3.6%/年", "ok", "评估改用竞价口径"),
    ("S27b", "开盘竞价能不能吃下我们的量", "我们的单占开盘竞价成交量中位数 0.4–0.5%", "ok", "容量没问题"),
    ("S38", "高空头持仓做入场否决", "符号反了:这几年高空头的反而跑赢", "no", "不采用"),
    ("S39", "增发 / 货架注册 / Form 144 做否决", "全市场增发后 20 日 −1.65%(t −4.31),但在我们的书里否决不加分", "no", "不采用(结论留作背景)"),
    ("S40", "最低股价下限", "长线 $2 下限过了预注册标准;内部人 $1–2 档反而 +3.13%(t 2.98)", "wait", "长线进 v2 规则包;内部人不设"),
    ("S41", "内部人线当天盘中入场", "+0.19%(t 0.98);多数 Form 4 在收盘后申报", "no", "不采用"),
    ("S42", "5 分钟已实现波动率做期权便宜度门", "t 5.8 看似通过;S42b 用 VWAP 复核 +0.05%(t 0.14),是陈旧成交价的伪影", "no", "不采用;新规则:期权收益必须用 VWAP 且成交量 ≥ 50"),
]
INFRA = [
    ("执行", "Alpaca 模拟盘三本书上线(长线 $60k / 内部人 $30k / SPY 核心 $10k);DAY 单;未成交重试;竞价口径净值;错过交易的影子账"),
    ("复盘", "每天 13:25 收盘后复盘:逐笔订单、实盘成交配对、规则检查、教训计数;周报加了 agent 部分"),
    ("网站", "全部私有页面统一主题和交互图表;公开站改为模拟盘(中英文);关注名单卡片、中文新闻带链接;价位同步到 moomoo App 提醒"),
    ("数据", "新接入:FINRA 空头持仓、SEC 424B5/S-3/144、Alpaca 开盘竞价、Alpha Vantage 盈利预测、moomoo IV/共识/评级每日存档、Alpaca 30 分钟和 5 分钟线"),
    ("仓库", "删除上游 virattt 代码和大师信号;两个仓库 README 重写;AGENT.md 作为跨 session 的持久上下文"),
    ("运维", "iCloud 备份(每日 + 每周,带恢复测试);系统健康页(定时任务、数据新鲜度、登录、备份)"),
]
LESSONS = [
    ("先验证执行环境,再相信回测假设", "OPG 单在模拟盘不按竞价成交,头两天内部人书 14 张单过期。上线前应该先用 1 股测试单验证每种单型。"),
    ("「跳过」规则要区分「下过单」和「成交过」", "从未成交的名字被当成已经买过。规则的每个条件都要写清楚它依赖的状态。"),
    ("小样本的结论不要说出口", "131 笔的探查和 S41 全样本结论相反;S42 的 t = 5.8 被稳健性检验推翻。先预注册,跑全样本,再做一次换口径的复核。"),
    ("期权回测的价格必须是能成交的价格", "最后成交价可能是几小时前的。以后一律 VWAP + 成交量下限。"),
    ("模拟盘的成交价不等于真实成本", "模拟盘按开盘后第一个卖一成交,平均比开盘竞价贵 0.6%。评估用竞价口径,两个口径都记。"),
    ("静默失败比报错更危险", "这一周有四次是偶然发现的:任务没加载、Claude 登录过期、电脑睡眠中断加载、API 配额用完。所以做了健康页。"),
    ("实盘:0DTE 和同日加仓", "9/23 SPXW 当日到期合约买了两次。复盘规则已经把这两条列为每日检查项。"),
]
OPEN = [
    "评估点之前不改实盘规则:长线 100 笔平仓 / 内部人 200 笔 / 最晚 2027-06-30",
    "v2 规则包(评估点一起上):长线 $2 下限、5 日大动不进",
    "等数据:Alpha Vantage 盈利预测修正(每天 20 只,大盘选股的下一个假设)、moomoo IV 存档、30 分钟线回填",
    "等你决定:Alpha Vantage 付费档(约 $50/月);并购套利现金收购线",
]


def pct(x: float) -> str:
    return f"<span class='{'pos' if x >= 0 else 'neg'}'>{x:+.2f}%</span>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-26")
    args = ap.parse_args()
    con = duckdb.connect(str(AGENT_DIR / "panel.db"), read_only=True)
    con.execute(f"ATTACH '{DB}' AS o (READ_ONLY)")
    idx = {(s, str(d)): c for s, d, c in con.execute("SELECT symbol, trade_date, close FROM index_daily WHERE trade_date >= ? AND symbol IN ('SPY','IWM')", [PREV]).fetchall()}
    nav = {(b, str(d)): e for b, d, e in con.execute("SELECT book, as_of, equity_usd FROM o.agent_book_nav").fetchall()}
    lots = con.execute("""SELECT l.ticker, l.qty, l.entry_px, b.close, (b.close / l.entry_px - 1) * 100, l.qty * (b.close - l.entry_px)
                          FROM o.agent_lots l JOIN bars b ON b.ticker = l.ticker AND b.trade_date = ?
                          WHERE l.book = 'long' AND l.status = 'open' ORDER BY 6""", [END]).fetchall()
    orders = con.execute("""SELECT book, CASE WHEN coalesce(filled_qty, 0) > 0 THEN 'filled' ELSE status END, count(*)
                            FROM o.agent_orders GROUP BY ALL""").fetchall()              # a filled order can read 'expired' after the day ends
    real = con.execute("SELECT date, max(total_assets) FROM o.acct_nav WHERE date >= ? GROUP BY 1 ORDER BY 1", [PREV]).fetchall()
    con.close()
    auct = json.load(open(os.path.join(OUT, "agent", "auction_basis.json")))["books"]
    alloc = {"long": 60000, "insider": 30000, "core": 10000}
    name = {"long": "长线综合因子", "insider": "内部人买入", "core": "SPY 核心"}
    spy_w = (idx[("SPY", END)] / idx[("SPY", PREV)] - 1) * 100
    iwm_w = (idx[("IWM", END)] / idx[("IWM", PREV)] - 1) * 100
    spy_h = (idx[("SPY", END)] / idx[("SPY", START)] - 1) * 100          # from the close before the long book's entries (09-22 open)
    iwm_h = (idx[("IWM", END)] / idx[("IWM", START)] - 1) * 100
    rows = ""
    tot_sim = tot_auc = 0.0
    for b in ("long", "insider", "core"):
        sim, auc = nav[(b, END)], auct[b]["equity_auction"]
        tot_sim += sim
        tot_auc += auc
        rows += (f"<tr><td>{name[b]}</td><td class='num'>${alloc[b]:,.0f}</td><td class='num'>${sim:,.0f}</td><td class='num'>{pct((sim / alloc[b] - 1) * 100)}</td>"
                 f"<td class='num'>${auc:,.0f}</td><td class='num'>{pct((auc / alloc[b] - 1) * 100)}</td><td class='num'>{auct[b]['n_fills']}</td></tr>")
    rows += (f"<tr><td><b>合计</b></td><td class='num'>$100,000</td><td class='num'>${tot_sim:,.0f}</td><td class='num'>{pct((tot_sim / 1e5 - 1) * 100)}</td>"
             f"<td class='num'>${tot_auc:,.0f}</td><td class='num'>{pct((tot_auc / 1e5 - 1) * 100)}</td><td></td></tr>")
    n_up = sum(1 for r in lots if r[4] > 0)
    pnl = sum(r[5] for r in lots)
    worst, best = lots[:5], lots[-5:][::-1]
    lot_tbl = lambda rs: "".join(f"<tr><td><b>{t}</b></td><td class='num'>{e:.2f}</td><td class='num'>{c:.2f}</td><td class='num'>{pct(r)}</td><td class='num'>{p:+,.0f}</td></tr>" for t, q, e, c, r, p in rs)  # noqa: E731
    head = "<tr><th>标的</th><th>买入价</th><th>周五收盘</th><th>涨跌</th><th>盈亏 $</th></tr>"
    ex_top = worst[0]
    od = {}
    for b, s, n in orders:
        od.setdefault(b, {})[s] = n
    ins = od.get("insider", {})
    real_rows = [(str(d), v) for d, v in real if v]
    real_txt = (f"moomoo 实盘 {real_rows[0][0]} ${real_rows[0][1]:,.0f} → {real_rows[-1][0]} ${real_rows[-1][1]:,.0f}"
                f"({pct((real_rows[-1][1] / real_rows[0][1] - 1) * 100)})") if len(real_rows) >= 2 else "实盘快照不足两天"
    vcls = {"ok": "flag ok", "no": "pill", "wait": "flag"}
    exp_rows = "".join(f"<tr><td class='mono'>{i}</td><td>{q}</td><td class='muted'>{r}</td><td><span class='{vcls[v]}'>{t}</span></td></tr>" for i, q, r, v, t in EXPERIMENTS)
    n_no = sum(1 for e in EXPERIMENTS if e[3] == "no")
    page = site_theme.head("第一周总结") + site_theme.nav("weekly", when=args.date) + f"""
<h1>第一周总结 · {START} → {END}</h1>
<p class='muted'>模拟盘上线第一周。私有页面:含实盘数字,不上公开站。</p>
<div class='cards'>
<div class='card'><span class='k'>模拟盘合计(模拟器口径)</span><span class='v'>{pct((tot_sim / 1e5 - 1) * 100)}</span><span class='s'>${tot_sim:,.0f}</span></div>
<div class='card'><span class='k'>模拟盘合计(竞价口径)</span><span class='v'>{pct((tot_auc / 1e5 - 1) * 100)}</span><span class='s'>${tot_auc:,.0f} · 评估用这个</span></div>
<div class='card'><span class='k'>SPY / IWM 本周</span><span class='v'>{pct(spy_w)}</span><span class='s'>IWM {iwm_w:+.2f}%</span></div>
<div class='card'><span class='k'>实验</span><span class='v'>{len(EXPERIMENTS)}</span><span class='s'>{n_no} 个否定 · 全部预注册</span></div>
</div>
<h2>一、模拟盘</h2>
<div class='tbl'><table><tr><th>书</th><th>起始</th><th>模拟器净值</th><th>自起始</th><th>竞价口径净值</th><th>自起始</th><th>成交笔数</th></tr>{rows}</table></div>
<p><b>长线书</b> 9/22 开盘一次建满 30 仓,到周五 {n_up} 涨 {len(lots) - n_up} 跌,持仓盈亏 ${pnl:+,.0f}。
同期(9/21 收盘起)SPY {spy_h:+.2f}%,IWM {iwm_h:+.2f}%。最大的一笔是 {ex_top[0]}({ex_top[4]:+.1f}%,${ex_top[5]:+,.0f}),占全部亏损的 {ex_top[5] / pnl * 100:.0f}%。</p>
<p class='muted'>怎么读:4 个交易日、30 只小盘股,一只股票就占了三成亏损,这个样本没有统计意义。
这一周不能证明策略有效,也不能证明无效;评估点是 100 笔平仓。规则不动。</p>
<div class='grid2'>
<div><p class='muted'>跌幅最大 5 只</p><div class='tbl'><table>{head}{lot_tbl(worst)}</table></div></div>
<div><p class='muted'>涨幅最大 5 只</p><div class='tbl'><table>{head}{lot_tbl(best)}</table></div></div>
</div>
<p><b>内部人书</b> 共下单 {sum(ins.values())} 张:成交 {ins.get('filled', 0)},未成交过期 {ins.get('expired', 0)},周一待成交 {ins.get('accepted', 0)}。
没成交的都是头两天的 OPG 单(S36);改 DAY 单之后 9/25 的两张(GSHD、OFIX)全部成交。<b>SPY 核心</b> 9/25 建仓,成交价比开盘竞价还低一点。</p>
<p><b>实盘</b> {real_txt}。实盘的逐笔分析在每天的复盘页。</p>
<h2>二、实验({len(EXPERIMENTS)} 个,全部先预注册再跑)</h2>
<div class='tbl'><table><tr><th>编号</th><th>问题</th><th>结果</th><th>结论</th></tr>{exp_rows}</table></div>
<p class='muted'>多重检验账本现在有 90 个变体。{n_no} 个否定结论同样有价值:每一个都是一条不用再试的路。完整记录在 docs/AGENT_PLAN.md §9。</p>
<h2>三、这一周建了什么</h2>
<div class='tbl'><table>{''.join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in INFRA)}</table></div>
<h2>四、教训</h2>
<div class='tbl'><table>{''.join(f"<tr><td style='white-space:nowrap'><b>{i + 1}</b></td><td><b>{k}</b><br><span class='muted'>{v}</span></td></tr>" for i, (k, v) in enumerate(LESSONS))}</table></div>
<h2>五、接下来</h2>
<ul class='list'>{''.join(f"<li>{x}</li>" for x in OPEN)}</ul>
<p class='muted'>生成 {args.date} · python -m agent.week_summary</p>""" + site_theme.FOOT
    os.makedirs(os.path.join(OUT, "weekly"), exist_ok=True)
    path = os.path.join(OUT, "weekly", f"{args.date}-第一周总结.html")
    with open(path, "w") as f:
        f.write(page)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
