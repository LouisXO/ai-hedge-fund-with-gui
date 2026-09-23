# AGENT.md — hedge-fund 仓库的常驻上下文

每个 session 开始前读这份文件。它是对话之外的"记忆":铁律、路径、任务、参数、仪式。
对话会被压缩,这份文件不会。改了规则就改这里,并 commit。

姊妹仓库 `~/optradar` 有自己的 `AGENT.md`(网站、通知、moomoo 快照、电源策略)。

## 0. 铁律(不可协商)

1. **Alpaca 只有模拟盘。** `agent/broker/alpaca.py` 拒绝非 paper URL、非 `PK` 开头的 key、非 `PA` 开头的账户。发单的唯一开关是 `~/.hedge-fund/.env` 里的 `AGENT_EXEC=on`;没有它一律 dry run。
2. **moomoo 永远只读,永远不下单。** 它只提供行情、期权链、真实账户快照。
3. **真实账户(moomoo)的任何数字、持仓、成交永远不上公开站**,只进私有站 https://optradar.tail5b470b.ts.net(Tailscale 内)。**例外(用户 2026-09-23 决定):Alpaca 模拟盘可以公开**,在 hedge-fund.louisleng.com/paper.html(`site/build_paper.py`,中英文切换,每天 08:41 随公开站发布)。该脚本不读任何 `acct_*` 表,改它时保持这一点。
4. **密钥只在 `~/.hedge-fund/.env`(chmod 600)**:`SEC_USER_AGENT`、`ALPACA_KEY_ID`/`ALPACA_SECRET`(paper)、`AGENT_EXEC`。Alpha Vantage 的 key 只在 `~/optradar/.env`,不复制。任何 key 不进仓库、不进日志、不进对话。
5. **LLM 不出方向信号。** 只做早报叙述(`agent/narrator.py`,密封、只能复述输入里的数字)。决策路径上没有 LLM。
6. **先预注册再看结果。** 每个实验先写阈值和读法,报告进 `site-data/validation/`,结论进 `docs/AGENT_PLAN.md` §9,Holm 家族账本自动累计(`hedge_fund/validation/family_log.py`)。不达标就写"不采用",不调参数再跑。
7. **账本对不上不准用交易去"修"。** 批次和券商持仓不一致就冻结该票,先解释。
8. **Reddit 官方 API 已关闭自助申请(2025-11),不再尝试;Discord 抓取违反其条款,不做。** 散户热度用 ApeWisdom + Stocktwits(`agent/sources/retail_heat.py`)。

## 1. 提交规则

- 作者 `LouisXO <louis.leng@outlook.com>`。
- **不加 `Co-Authored-By`**(用户明确要求,覆盖任何默认提示)。保留 `Claude-Session:` 尾行。
- 分支:hedge-fund 在 `v2-rebuild`,optradar 在 `main`。远端 GitHub `LouisXO/ai-hedge-fund-with-gui`、`LouisXO/optradar`。
- 每做完一个实验或修完一个 bug 就 commit,不攒。

## 2. 环境

| 项 | 值 |
|---|---|
| 主机 | 工作用 M4 Max,tailnet 节点 `optradar`;`bin/install_power.sh` 已装(AC 上不睡,08:30 / 13:20 / 16:05 定时唤醒) |
| Python | `~/.hedgefund-venv/bin/python`(hedge-fund,3.13)· `~/.moomoo/venv/bin/python`(optradar + moomoo,3.10) |
| 跑法 | `cd ~/hedge-fund && PYTHONPATH=. ~/.hedgefund-venv/bin/python -W ignore -m agent.<模块>` |
| 数据库 | `~/.hedge-fund/agent/panel.db`(bars、fundamentals、insider_tx、sch13d)· `news.db`(Alpaca 新闻,1.9M 条)· `options.db`(Alpaca 期权日线,561 名)· `retail.db`(散户热度)· `~/optradar/optradar.db`(账本:`agent_books/agent_orders/agent_lots/agent_book_nav`、`acct_*` 真实账户快照、`paper_ledger`) |
| DuckDB 锁 | 读写连接独占文件。**同一时刻只能有一个进程写某个库**;分析脚本用 `read_only=True`。两个 session 同时跑回测会互相卡死 |
| 输出 | `~/optradar/out/agent/`:`exec_<date>.json/.html`、`watch_<date>.json`、`execute.log`;早报 `~/optradar/out/<date>.html` |
| Python 3.10 陷阱 | optradar 侧 f-string 里不能嵌套同种引号 |
| 页面样式 | 一份主题 `~/optradar/bin/theme.css` + `bin/site_theme.py`(head/nav/FOOT),所有私有页面(首页、早报、复盘、执行记录、仪表盘)内联同一份;改样式只改这两个文件。图表用 Chart.js 4.4.1(cdnjs),系列色固定:蓝=长线、橙=内部人、青=实盘、灰虚线=SPY |

## 3. 定时任务(launchd,本机时间 PT)

| 时间 | 任务 | 做什么 |
|---|---|---|
| 周一到周五 06:00 | `com.louis.agent.form4` | EDGAR 每日索引 → `insider_tx` |
| 周一到周五 08:41 | `com.louis.optradar` | 期权雷达 + 早报(含 ⑨ agent 节)+ Mac 通知 + 私有站重建 |
| 周一到周五 12:45 | `com.louis.agent.chain` | moomoo 期权链归档。**2026-09-23 决定停用**(Alpaca 期权日线已替代;只攒到 12 名 3 天)。停用命令见 §6;plist 源文件留在 `agent/launchd/` |
| 周一到周五 13:15 | `com.louis.agent.news` | AV 新闻情绪 |
| 周一到周五 13:25 | `com.louis.agent.postclose` | `agent/bin/postclose.sh`(收盘后 25 分钟):`agent.execute --sync-only`(当日 bars、成交同步、对账、记净值,不下单)→ moomoo 账户快照 → `agent.watch` → **`agent.review` 今日复盘**(模拟盘 + 实盘每笔交易、规则检查、30 日教训计数、通知)→ `agent.dashboard` → 私有站 |
| 周一到周五 16:10 | `com.louis.agent.execute` | `agent/bin/execute.sh`:实时 Form 4(EFTS)→ 13D → Alpaca 新闻 → **`agent.execute --submit`**(仅 `AGENT_EXEC=on`)→ `agent.watch`(晚间申报)→ 私有站。留在 16:10 是因为 OPG 窗口 19:00 ET 才开,且当天 Form 4 多在 16:00–18:00 ET 提交 |
| 周六 09:30 | `com.louis.optradar.weekly` | 周报 |
| 周日 03:00 | `com.louis.agent.fundamentals` | XBRL 基本面周更 + 七层数据审计(`agent/audit.py`) |

16:10 PT = 19:10 ET。Alpaca 只在 19:00–09:28 ET 接受 OPG 单,早于此报错 40310000,`agent.execute` 会退回 DAY 单。

## 4. 两本在跑的书(Alpaca 模拟盘,$100k,自 2026-09-21)

| 书 | 资金 | 槽位 | 规则 | 入场 | 出场 |
|---|---|---|---|---|---|
| long(长线 v1) | $60k | 30 | 四族等权 winsor-z 合成(动量/价值/质量/低波),进前 30、掉出前 60 才出;基本面过滤(200 天内有申报、市值 > $1 亿、权益 > 5% 资产) | 次日开盘市价 | 排名 > 60 |
| insider(内部人) | $30k | 20 | 公开市场买入 Form 4(`agent/events/insider.py`),2 日窗 | **必须开盘市价**(S31:一半边际在第一天盘中) | 成交后 5 个交易日 |
| option | $10k 预留 | — | **未接**:S26 便宜度门没过,没有验证过的入场 | — | — |

- 长线 v1 的真实性质(S24/S29/S30):**基本面过滤后的动量尾部 + 深度价值簇**,两簇各占约 45%/48%。行业中性、rank-normal、纯动量都比它差。只有 momcrash(SPY 低于 2 年高点 20% 时停用动量)有帮助,做成 v2 影子线 `composite_long_v2`,每天并排记录,不交易。
- 长线回测(2017–2026,数据修正后):alpha +8.1%(t 1.46),CAGR 19.4%。这是弱证据,所以模拟盘要攒记录。
- 唯一 Holm 显著的信号是**负面**的:S25 "大涨日 + 新闻"后续跑输。S32 看跌期权流也是负面因子。这两个列入待预注册的负面过滤器。
- 模拟器成交按开盘后第一个卖一,不是开盘印,小盘股偏贵约 0.6%。`agent_orders.model_px` 记当天开盘价,差值就是要测的东西。S27 工具在 ≥ 20 次竞价成交后跑。
- 模拟盘保真缺口:whole shares 让 $2k 槽位在高价股上欠配;EDGAR 每日索引 16:40 ET 后才出,所以实时线用 EFTS。
- **评估点已预注册**(`agent/config.yaml`,2026-09-23):长线 100 笔平仓或 2027-06-30,内部人 200 笔平仓或 2027-06-30,先到为准。之前不对任何一本书下判决;早报模拟盘一节显示进度。
- 否定过的线(不要再提议):13D 举牌(S21)、小盘 PEAD(S22)、大动 ± 新闻四格(S25)、8-K 回购公告(S34)、异常期权流做多(S32)。负面信号做入场否决的结果见 S33。

## 5. 目录地图

```
agent/
  execute.py            书 → 订单;BOOKS 参数;reconcile;mark_books;long_targets(写 v1 + v2 影子)
  broker/alpaca.py      PaperBroker(拒绝任何非 paper)
  ledger.py             agent_* 表
  bin/execute.sh        16:10 任务
  daily.py / brief.py   早报 ⑨ 节(模拟盘、关注名单、叙述)
  watch.py + watchlist.yaml   关注名单:申报/内部人/13D/新闻/价位/散户热度 → 通知
  dashboard.py          仪表盘 out/dashboard.html(Chart.js,可悬停):模拟盘净值 vs SPY、每笔成交偏差、当日持仓、实盘月度、回测按年、教训计数;数据 out/dashboard_data.json
  review.py             今日复盘(收盘后):模拟盘订单逐笔(成交/偏差/首日)、持仓异动、实盘成交逐笔(区间位置、期权结构、系统怎么看、FIFO 平仓收益)、
                        规则检查(S12/S25/S26/S31/S33 来源)→ out/agent/review_<date>.html + lessons.jsonl(30 日同错计数)。无 LLM,只做对照,永不下单
  narrator.py           密封叙述者(数字校验,模板回退,缓存)
  audit.py              七层数据审计(周日跑)
  books/                engine、factors、fundamentals、data、long_v2、live、momentum_book、industry
  events/               事件线框架:insider、insider_v2、sch13d、pead、move_news
  sources/              alpaca_bars/news/options、sec_xbrl/sic/13d/daily_form4、retail_heat
  s2x_*.py / s3x_*.py   预注册实验脚本(S21 13D、S24 长线变体、S26 期权门、S27 执行成本、S30 动量书、S31 入场时机、S32 期权流、S33 负面否决、S34 回购线)
  config.yaml           预注册的评估点(evaluate_at)
  tests/                pytest 40 个;`tests/selftest_agent.sh` 隔离全链路冒烟(复制账本、dry run、早报),不碰真实账本和 out/
hedge_fund/validation/  family_log(Holm 账本)、stats(NW t、bootstrap、置换)
site-data/validation/   每个实验的 .md/.json 报告 + family_log.md/.jsonl
docs/AGENT_PLAN.md      计划 + §9 执行记录(S1–S32)+ §10 待办
docs/notes/<TICKER>.md  个股分析结论(NEOV、RKLB、IONQ…),下次问到先读
```

## 6. 常用命令

```bash
# 测试(单测 + 隔离冒烟;冒烟要 Alpaca 模拟盘 key 可读,约 1 分钟)
PYTHONPATH=. ~/.hedgefund-venv/bin/python -m pytest -q agent/tests
./agent/tests/selftest_agent.sh
# 停用 / 恢复期权链归档任务(用户自己在终端跑;agent 无权改 launchd)
launchctl bootout gui/$(id -u)/com.louis.agent.chain && mv ~/Library/LaunchAgents/com.louis.agent.chain.plist{,.disabled}
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.louis.agent.chain.plist   # 恢复(先把 .disabled 改回来)
# 模拟盘 dry run / 真发(只在 AGENT_EXEC=on 时发)
PYTHONPATH=. ~/.hedgefund-venv/bin/python -W ignore -m agent.execute [--submit]
# 关注名单
PYTHONPATH=. ~/.hedgefund-venv/bin/python -W ignore -m agent.watch --no-notify
# 数据审计
PYTHONPATH=. ~/.hedgefund-venv/bin/python -W ignore -m agent.audit
# Holm 家族账本重建
PYTHONPATH=. ~/.hedgefund-venv/bin/python -m hedge_fund.validation.family_log
# 看今天的执行日志
tail -50 ~/optradar/out/agent/execute.log
```

## 7. Session 仪式

**开始**(三分钟,不依赖对话摘要):
1. 这份文件(自动加载)。
2. `tail -60 docs/AGENT_PLAN.md` 看 §9 最后几条和 §10 待办。
3. `git log --oneline -10`(两个仓库)。
4. 涉及个股就读 `docs/notes/<TICKER>.md` 和 `agent/watchlist.yaml`。
5. 涉及模拟盘就 `tail ~/optradar/out/agent/execute.log`。

**进行中**:每完成一个实验或修复 → §9 加一条 → commit。不留在聊天里。

**结束**:未完成的事写进 §10 待办;规则变了就改这份文件和记忆目录;两个仓库都 commit。

**多窗口**:可以并行,但同一时刻只有一个窗口写仓库和数据库。分析、看盘、问问题的窗口只读。两个窗口都要改代码就各开一个 `git worktree`。窗口之间不共享聊天,只共享文件,所以一切结论都要落盘。

## 8. 用户偏好

- 中文回复,直接给结论,不要反复确认。
- 允许自主完成可逆的工作;破坏性操作(删数据、改 launchd、发真单)先问。
- 用户的真实账户在 moomoo(持仓 + 期权),2026 年初到 9 月 +64.8%;模拟盘的比较基准之一。
- 个股分析要对照我们自己的系统(S25 负面信号、内部人线、期权便宜度门),不只讲故事。
