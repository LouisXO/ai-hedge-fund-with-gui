# AGENT.md — hedge-fund 仓库的常驻上下文

每个 session 开始前读这份文件。它是对话之外的"记忆":铁律、路径、任务、参数、仪式。
对话会被压缩,这份文件不会。改了规则就改这里,并 commit。

姊妹仓库 `~/optradar` 有自己的 `AGENT.md`(网站、通知、moomoo 快照、电源策略)。

## 0. 铁律(不可协商)

1. **Alpaca 只有模拟盘。** `agent/broker/alpaca.py` 拒绝非 paper URL、非 `PK` 开头的 key、非 `PA` 开头的账户。发单的唯一开关是 `~/.hedge-fund/.env` 里的 `AGENT_EXEC=on`;没有它一律 dry run。
2. **moomoo 永远只读,永远不下单。** 它只提供行情、期权链、真实账户快照。
3. **真实账户(moomoo)的任何数字、持仓、成交永远不上公开站,也不进本仓库(本仓库公开,2026-09-28 审计发现并移出)**,只进私有站 https://optradar.tail5b470b.ts.net(Tailscale 内)。**例外(用户 2026-09-23 决定):Alpaca 模拟盘可以公开**,在 hedge-fund.louisleng.com/paper.html(`site/build_paper.py`,中英文切换,每天 08:41 随公开站发布)。该脚本不读任何 `acct_*` 表,改它时保持这一点。**公开站有的信息私有站必须都有**:`build_paper.py` 同时写 `optradar/out/paper.html`(私有导航)并把 `/masters/` 存档复制进私有站;13:25 和 16:10 任务都会重建。以后公开站加任何内容,都要从同一个生成器同时写私有版本。
4. **密钥只在 `~/.hedge-fund/.env`(chmod 600)**:`SEC_USER_AGENT`、`ALPACA_KEY_ID`/`ALPACA_SECRET`(paper)、`AGENT_EXEC`。Alpha Vantage 的 key 只在 `~/optradar/.env`,不复制。任何 key 不进仓库、不进日志、不进对话。
5. **LLM 不出方向信号。** 只做早报叙述(`agent/narrator.py`,密封、只能复述输入里的数字)和新闻标题翻译(`agent/translate.py`)。决策路径上没有 LLM。大师信号(5 个 persona)2026-09-23 退役并删除;上游 virattt 代码同日删除,tag `pre-cleanup-2026-09-23` 可恢复。依赖清单是 `requirements.txt`(没有 pyproject)。
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
| 周一到周五 13:25 | `com.louis.agent.postclose` | `agent/bin/postclose.sh`(收盘后 25 分钟):`agent.execute --sync-only`(当日 bars、成交同步、对账、记净值,不下单)→ moomoo 账户快照 → moomoo 股数核对(`agent.sources.moomoo_shares`,只读行情接口,S47b)→ `agent.watch` → **`agent.review` 今日复盘**(模拟盘 + 实盘每笔交易、规则检查、30 日教训计数、通知)→ `agent.dashboard` → 私有站 |
| (13:25 任务末尾) | 数据存档 | `sec_forms update`(424B5/S-3/144)、`av_estimates --max 20`(每天 20 家盈利预测,最大市值优先)、`daily_archive`(moomoo IV/HV、目标价共识、券商评级明细;Alpaca 难借券标记)。盘中 K 线 `alpaca_intraday`(30 分钟全池 2019 起、5 分钟期权名 2023-12 起,只存正常交易时段)。时点数据库:`filings.db`、`estimates.db`、`archive.db`、`auctions.db`、`short.db`、`intraday.db` |
| (13:25 任务最后;周日 03:00) | 备份 + 健康检查 | `agent.backup`:每日档(optradar.db、archive/estimates/retail/auctions/filings、状态文件、out/agent;留 14 份),周日 `--weekly`(panel/options/news/short/intraday;留 2 份)→ iCloud Drive `OptRadarBackup/`。复制时持只读连接、复制后做恢复测试、zstd 压缩;被写入占用的库跳过并在健康页标出。**密钥不备份到云**。恢复:`zstd -d x.db.zst -o x.db`。`agent.health`:每个定时任务结束时写 `out/health.json`(launchd 任务是否加载/按时跑、18 个数据集的新鲜度、OpenD、Alpaca、Claude 登录、电源、磁盘、AV 配额、备份),私有站首页顶部显示,出现新的故障时推送通知 |
| 周一到周五 16:10 | `com.louis.agent.execute` | `agent/bin/execute.sh`:实时 Form 4(EFTS)→ 13D → Alpaca 新闻 → **`agent.execute --submit`**(仅 `AGENT_EXEC=on`)→ `agent.watch`(晚间申报)→ 私有站。留在 16:10 是因为 OPG 窗口 19:00 ET 才开,且当天 Form 4 多在 16:00–18:00 ET 提交 |
| 周一到周五 19:15 | `com.louis.agent.late_insider` | `agent/bin/late_insider.sh`(S47b):EDGAR 22:00 ET 停止受理后再抓一次当天 Form 4,只给内部人书下单;同一信号日的订单号相同,不会重复。**要同时有 AGENT_EXEC=on 和 `~/.hedge-fund/agent/late_insider.ok` 才发单**,否则只打印计划;日志 `out/agent/late_insider.log`。plist 在 `agent/launchd/`,由用户安装 |
| 周六 09:30 | `com.louis.optradar.weekly` | 周报 |
| 周日 03:00 | `com.louis.agent.fundamentals` | XBRL 基本面周更 + 七层数据审计(`agent/audit.py`) |

16:10 PT = 19:10 ET。Alpaca 只在 19:00–09:28 ET 接受 OPG 单,早于此报错 40310000,`agent.execute` 会退回 DAY 单。

## 4. 两本在跑的书(Alpaca 模拟盘,$100k,自 2026-09-21)

| 书 | 资金 | 槽位 | 规则 | 入场 | 出场 |
|---|---|---|---|---|---|
| long(长线 v1) | $60k | 30 | 四族等权 winsor-z 合成(动量/价值/质量/低波),进前 30、掉出前 60 才出;基本面过滤(200 天内有申报、市值 > $1 亿、权益 > 5% 资产) | 次日 DAY 限价单(前收盘 + 3%) | 排名 > 60 |
| insider(内部人) | $30k | 20 | 公开市场买入 Form 4(`agent/events/insider.py`,实盘与回测同一代码):按单个申报日判定;单笔超过 $5000 万不计;买入价须在当天价格区间内(0.8×最低 到 1.25×最高,挡住折价认购和外币申报);流动性和上市掩码按申报日判定;窗口是最近 2 个交易日,未成交的重试名字回看 3 个;类别股代码暂不买入(S47,2026-09-29) | DAY 限价单(前收盘 + 3%);**不用市价单**:市价单按卖一成交,2026-09-28 INBX 买在 105.33 而开盘竞价 98.69、全天最高不到 105。高开超过 3% 的不追,次日重试一次 | 成交后 5 个交易日 |
| core(SPY 核心仓) | $10k | 1 | 被动持有 SPY(S37:大盘选股没有边际);2026-09-24 起,原期权预留 | 首次 DAY 限价 +1% | 不按规则卖出 |

- 影子线(只记录不交易):`composite_long_v2`(动量崩盘过滤)、`largecap_qlv`(S37 大盘质量+低波,市值 ≥ $100 亿)。
- **v2 规则包(S44,2026-09-26 固定,之后的新规则只能进 v3)**:定义在 `agent/books/long_v2_bundle.py`。六条影子线(v1c 对照、floor2、jump5、cap20、clusters、bundle)由 `agent/shadow_v2.py` 每天在记录的名单上重放,自 9/25 名单起。**回测里 bundle 没过标准 (a)**(alpha2 +0.0,平均仓位只有 73%),所以到评估点不能采用;影子线继续记,给 v3 用。
- **每周学习回路**:`agent/attribution.py`(大盘 / 风格 / 行业 / 个股 / 执行五层归因,`out/agent/attribution.jsonl` 每周一行)→ `docs/HYPOTHESES.md`(观察 → 假设 → 排除当周数据的检验)→ 预注册 → 规则包。`agent/drift.py` 每天检查模拟盘是否和规则重放一致、是否在回测区间内。**单周数字不能用来改规则。**
- **数据修正(S47,2026-09-29 生效)**:基本面、上市名单、日线、内部人候选都已恢复成回测的定义;评估从 `evaluate_from: 2026-09-29` 起算。重述后长线 v1 alpha2 +8.37%/年(t 1.40),内部人 v1 +8.80%/年(t 2.05)。每周基本面刷新现在会真的重取;打开周日任务的刷新要创建 `~/.hedge-fund/agent/refresh_fundamentals.ok`。
- **数据修正 S47b(2026-09-29 当天生效,评估起点不变)**:重复使用或重新上市的代码拆成伪代码(`SE@2007`,`agent/books/segments.py`;伪代码永远不下单);数据商名单缺的普通股进 `agent/listing_supplement.yaml`(目前只有 NRG);股数为空或被标记的行用 moomoo 行情快照的股数覆盖(申报后 120 天内最早的一次)。重述:长线 v1 alpha2 +8.55%/年(t 1.44),内部人 v1 +9.05%/年(t 2.11)。
- **模拟盘能回答什么(S45,2026-09-28)**:它分辨不出策略有没有超额(9 个月时 alpha 标准误约 20%/年)。它能测准的是三样:对规则重放的跟踪差、执行偏差、成交率。页面头条不放收益对比的结论;搬不搬真钱不能靠模拟盘的收益数字。
- **下单保护(S46)**:对账不符的股票买卖都不发单;只在美东 16:05–09:25 之间提交;日线日期必须等于上一个交易日。
- 回测口径:带进场否决的变体必须用实盘口径(`s44_v2_bundle.entry_zone`,只从前 N 名进场);S33/S40 之前的回测是宽松口径。
- 竞价口径(S27b 后续):`agent/auction_basis.py` 把每笔模拟成交按当天开盘竞价价重记,`agent_auction_nav` 表;没成交的内部人入场按竞价价做影子记录。**评估点按竞价口径判**,模拟器口径是保守下限。
- 长线 v1 的真实性质(S24/S29/S30):**基本面过滤后的动量尾部 + 深度价值簇**,两簇各占约 45%/48%。行业中性、rank-normal、纯动量都比它差。只有 momcrash(SPY 低于 2 年高点 20% 时停用动量)有帮助,做成 v2 影子线 `composite_long_v2`,每天并排记录,不交易。
- 长线回测(2017–2026-08,S47b 重述):alpha2 +8.55%/年(t 1.44),CAGR 20.0%。加入动量、价值、质量、低波 ETF 后还剩 +7.2%/年(t 1.5),动量暴露 +1.08(S45 D3)。这是弱证据,所以模拟盘要攒记录。
- 唯一 Holm 显著的信号是**负面**的:S25 "大涨日 + 新闻"后续跑输。S32 看跌期权流也是负面因子。这两个列入待预注册的负面过滤器。
- 模拟器成交按开盘后第一个卖一,不是开盘印,小盘股偏贵约 0.6%。`agent_orders.model_px` 记当天开盘价,差值就是要测的东西。S27 工具在 ≥ 20 次竞价成交后跑。
- 模拟盘保真缺口:whole shares 让 $2k 槽位在高价股上欠配;EDGAR 每日索引 16:40 ET 后才出,所以实时线用 EFTS。
- **评估点已预注册**(`agent/config.yaml`,2026-09-23):长线 100 笔平仓或 2027-06-30,内部人 200 笔平仓或 2027-06-30,先到为准。之前不对任何一本书下判决;早报模拟盘一节显示进度。
- 否定过的线(不要再提议):13D 举牌(S21)、小盘 PEAD(S22)、大动 ± 新闻四格(S25)、8-K 回购公告(S34)、异常期权流做多(S32)、大盘选股九个规则(S37)、空头持仓否决(S38)、增发/货架/Form 144 否决(S39)、内部人当天盘中入场(S41)、期权便宜度门含 5 分钟 RV 版本(S26/S42:比率门的显著性是陈旧成交价伪影,VWAP 计价后为零)。
- **期权收益的检验规矩**:用日线最后成交价算的期权收益,必须同时报告 VWAP 计价和成交量 ≥ 50 张的子样本,否则不作数(S42b)。
- 执行成本(S27/S27b):真实开盘竞价完全吃得下两本书的量(中位 0.4–0.5%);模拟器按开盘后卖一成交,约 +0.6%/边,会把长线 alpha 从 +8.1% 压到 +3.6%。评估模拟盘时必须按同一口径比较。数据源清单见 `docs/DATA_SOURCES.md`。负面信号做入场否决的结果见 S33。

## 5. 目录地图

```
agent/
  execute.py            书 → 订单;BOOKS 参数;reconcile;mark_books;long_targets(写 v1 + v2 影子)
  broker/alpaca.py      PaperBroker(拒绝任何非 paper)
  ledger.py             agent_* 表
  bin/execute.sh        16:10 任务;bin/postclose.sh 13:25;bin/late_insider.sh 19:15(S47b)
  dividends.py / evaluate.py   分红计入净值(不计入现金);评估指标(只写 out/agent/evaluate.json,不上页面头条)
  daily.py / brief.py   早报 ⑨ 节(模拟盘、关注名单、叙述)
  watch.py + watchlist.yaml   关注名单:申报/内部人/13D/新闻/价位/散户热度/分析师共识/期权大单 → 通知
  balder_log.py         Balder(X 订阅)帖子 → 跟单记分:用户把帖子文本放进 iCloud Drive/Balder,收盘后密封 LLM 提取交易、按次日开盘记 1/5/20 日收益。**不抓 X,不上公开站,不进信号**
  watch_reminders.py    把关注名单价位同步成 moomoo App 价格提醒(只动 note 以 agent 开头的,用户自己设的不碰;只用行情接口,不开交易接口)
  dashboard.py          仪表盘 out/dashboard.html(Chart.js,可悬停):模拟盘净值 vs SPY、每笔成交偏差、当日持仓、实盘月度、回测按年、教训计数;数据 out/dashboard_data.json
  review.py             今日复盘(收盘后):模拟盘订单逐笔(成交/偏差/首日)、持仓异动、实盘成交逐笔(区间位置、期权结构、系统怎么看、FIFO 平仓收益)、
                        规则检查(S12/S25/S26/S31/S33 来源)→ out/agent/review_<date>.html + lessons.jsonl(30 日同错计数)。无 LLM,只做对照,永不下单
  narrator.py           密封叙述者(数字校验,模板回退,缓存)
  audit.py              七层数据审计(周日跑)
  books/                engine、factors、fundamentals、data、long_v2、live、momentum_book、industry、segments(重复代码拆分)
  listing_supplement.yaml  数据商名单缺的普通股(每行写来源和核对日期)
  events/               事件线框架:insider、insider_v2、sch13d、pead、move_news
  sources/              alpaca_bars/news/options/auctions、sec_xbrl/sic/13d/daily_form4/8k_buyback、finra_short、retail_heat、moomoo_shares(股数核对,只读)
  s2x_*.py / s3x_*.py   预注册实验脚本(S21 13D、S24 长线变体、S26 期权门、S27 执行成本、S30 动量书、S31 入场时机、S32 期权流、S33 负面否决、S34 回购线)
  config.yaml           预注册的评估点(evaluate_at)
  tests/                pytest 40 个;`tests/selftest_agent.sh` 隔离全链路冒烟(复制账本、dry run、早报),不碰真实账本和 out/
hedge_fund/             共享库:features/(panel、factors、rv)、validation/(stats、tearsheet、family_log Holm 账本)、paths.py
integrations/           claude_code_llm.py(密封 LLM,只做叙述和翻译)、moomoo_client.py + moomoo_models.py(只读)
earnings/               build_event_db.py(每周一刷新 site-data/events.db)、event_stats.py(optradar ④ 波动率定价的基准)
site/                   公开站:build_paper.py(首页 = 模拟盘,中英文)、build_site.py(大师信号存档 /masters/,已停止)、publish.sh
site-data/validation/   每个实验的 .md/.json 报告 + family_log.md/.jsonl
docs/AGENT_PLAN.md      计划 + §9 执行记录(S1–S32)+ §10 待办
docs/notes/<TICKER>.md  个股分析结论(NEOV、RKLB、IONQ…),下次问到先读
docs/RUNBOOK.md         应急手册(下单失败、对账不符、券商故障、坏数据日、全部平仓、电脑丢失、恢复备份)
docs/LIVE_MIGRATION.md  上实盘的设计(公开部分;涉及真实账户的决策框架在 optradar/docs/LIVE_MIGRATION_PRIVATE.md)
```

## 6. 常用命令

```bash
# 测试(单测 + 隔离冒烟;冒烟要 Alpaca 模拟盘 key 可读,约 1 分钟)
PYTHONPATH=. ~/.hedgefund-venv/bin/python -m pytest -q agent/tests hedge_fund
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
- 用户的真实账户在 moomoo。**本仓库是公开的(GitHub 公开 fork):真实账户的任何数字、持仓、成交都不写进本仓库的任何文件**,包括文档、笔记、关注名单备注和代码里的文字;这类内容只放私有仓库 `optradar/docs/PRIVATE.md` 和私有站。
- 个股分析要对照我们自己的系统(S25 负面信号、内部人线、期权便宜度门),不只讲故事。
