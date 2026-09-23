# hedge-fund

规则驱动的股票研究和模拟盘交易系统。每个信号都先预注册、再检验,通过的才能交易。
两本通过检验的书在 Alpaca **模拟账户**($100,000 虚拟资金)上每天交易,公开记录在 https://hedge-fund.louisleng.com(中英文)。

**决策路径上没有 LLM。** LLM 只用来写展示文字(早报叙述、新闻标题翻译),走密封的单轮调用。

## 两本在跑的书

| 书 | 资金 | 规则 | 入场 | 出场 |
|---|---|---|---|---|
| 长线综合因子 | $60,000,30 仓 | 动量、价值、质量、低波四族等权打分,先过基本面过滤 | 排名进前 30,次日开盘 | 排名跌出前 60 |
| 内部人短线 | $30,000,20 仓 | 高管或董事公开市场买入(SEC Form 4) | 次日开盘市价 | 5 个交易日后 |

回测(2017 → 2026-08):长线两因子 alpha 约 +8%/年(t 1.46),内部人约 +8%/年(t 1.93),多重检验校正后都不显著,所以要靠模拟盘攒证据。
评估点已预注册(`agent/config.yaml`):长线 100 笔平仓、内部人 200 笔平仓,或 2027-06-30,先到为准。之前不根据好坏改规则。

## 每天做什么

| 时间(PT,工作日) | 任务 | 内容 |
|---|---|---|
| 06:00 | `com.louis.agent.form4` | SEC 每日索引 → 内部人交易 |
| 08:41 | optradar 早报任务调用 | agent 打分、早报 ⑨ 节、发布公开站(`site/publish.sh`) |
| 13:15 | `com.louis.agent.news` | Alpha Vantage 新闻情绪 |
| 13:25 | `agent/bin/postclose.sh` | 当日数据、成交同步、对账、记净值 → 实盘快照 → 关注名单 → **今日复盘** → 仪表盘 → 私有站 |
| 16:10 | `agent/bin/execute.sh` | 晚间 Form 4 / 13D / 新闻 → **计划并提交次日订单**(只在 `AGENT_EXEC=on` 时发送) |
| 周日 03:00 | `agent/bin/weekly_fundamentals.sh` | XBRL 基本面周更 + 七层数据审计 |

## 目录

| 路径 | 内容 |
|---|---|
| `agent/` | 系统本体:数据加载(`sources/`)、事件线(`events/`)、两本书(`books/`)、模拟盘执行(`execute.py`、`broker/alpaca.py`)、复盘(`review.py`)、仪表盘(`dashboard.py`)、关注名单(`watch.py`)、叙述和翻译,以及每个预注册实验(`sNN_*.py`) |
| `hedge_fund/` | 共享库:DuckDB 时点面板(`features/panel.py`)、因子打分、已实现波动率、检验统计、Holm 家族账本 |
| `integrations/` | 密封的 Claude Code LLM 客户端、只读的 moomoo 数据客户端 |
| `earnings/` | 财报事件库和统计基准(optradar 早报 ④ 波动率定价用) |
| `site/` | 公开站:首页 = 模拟盘(`build_paper.py`,中英文切换),`/masters/` = 已停止的大师信号存档 |
| `site-data/` | 每个实验的报告(`validation/`)、Holm 账本、财报事件数据、大师信号存档 |
| `docs/AGENT_PLAN.md` | 计划、§9 执行记录(S1–S35,每个实验一条)、§10 滚动待办 |
| `docs/notes/` | 个股分析结论(NEOV、RKLB、IONQ…) |
| `AGENT.md` | 每次工作开始前读:铁律、路径、任务、仪式 |

## 检验出来的结论(节选)

- 通过:内部人公开市场买入(S11),而且必须开盘就买,一半边际在第一天盘中(S31)。
- 否定:13D 举牌(S21)、小盘 PEAD(S22)、大动 ± 新闻做多(S25)、异常期权流做多(S32)、负面信号做入场否决(S33)、8-K 回购公告(S34)、期权便宜度门(S26)。
- 唯一 Holm 显著的是负面信号:大涨 + 新闻之后跑输(S25)。
- LLM 大师 persona:5 日方向命中 55%,低于"永远猜多数方向"的 70%,2026-09-23 退役(S35)。

完整记录在 `docs/AGENT_PLAN.md` §9,家族账本在 `site-data/validation/family_log.md`。

## 安装

```bash
python3.13 -m venv ~/.hedgefund-venv
~/.hedgefund-venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=. ~/.hedgefund-venv/bin/python -m pytest -q agent/tests hedge_fund
./agent/tests/selftest_agent.sh          # 隔离全链路:复制账本、模拟盘 dry run、早报,不碰真实数据
```

密钥只在 `~/.hedge-fund/.env`(chmod 600):`SEC_USER_AGENT`、`ALPACA_KEY_ID` / `ALPACA_SECRET`(模拟盘)、`AGENT_EXEC`。
券商客户端只接受 paper 地址、`PK` 开头的 key 和 `PA` 开头的账户,任何实盘凭据都会被拒绝。
launchd 的 plist 源文件在 `agent/launchd/`。

## 两个仓库

本仓库管信号、检验、模拟盘交易和公开站。姊妹仓库 [optradar](https://github.com/LouisXO/optradar) 管期权雷达早报、实盘只读镜像和私有站(Tailscale 内网)。
两边共用 `optradar.db`(模拟盘账本表由本仓库写入)和私有站的主题文件。

## 来历

本仓库最初 fork 自 [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)(MIT 许可,见 `LICENSE`)。
它的 LLM 投资大师 persona 在这里经过检验后于 2026-09-23 退役,上游模块同日删除;tag `pre-cleanup-2026-09-23` 是包含它们的最后状态。
