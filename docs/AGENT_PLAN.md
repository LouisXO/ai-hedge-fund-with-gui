# 多信号验证型交易 Agent — 详细计划 v2

- v1:2026-09-18(调研开源框架 + 仓库探索 + 实现设计),原文见 `docs/AGENT_PLAN_v1.md`
- v2.1:2026-09-19 S1 完成后修正显著性检验,见 §9
- v2:2026-09-19(第二轮对比:独立 agent 按 Qlib/因子评测惯例、多重检验文献、期权收益文献逐条挑错,关键说法已实测核实)
- 仓库:`~/hedge-fund`(v2-rebuild)+ `~/optradar`(main)

---

## 0. Context

- **现状**:optradar 每天 08:41 出买方期权雷达。方向靠 SMA20 + 5 日动量门,合约结构靠 Black-Scholes 持有期定价。5 位 LLM 大师周一/周四打分。纸面账本记 budget/atm 两种合约,7/14 天结算。
- **已被证伪**(PLAN.md §7–§9):
  - LLM 方向预测:+5 日命中 55%,"永远猜多数方向"是 70%。
  - 5 人委员会 = 单人:误差完全相关。
  - 财报卖方"边际":是 30 日 IV 代理口径的产物。
  - §9 的结论:"没有记分牌,加任何东西之前先闭环"。
- **9/18 已完成**:LLM 调用密封、类型化合约、缓存键带真实模型 ID。
- **目标**:把各类信号整合成一个 agent。每个信号先过统一检验,每天的建议进纸面账本闭环。

### 已拍板的决定
| 项 | 决定 |
|---|---|
| LLM 角色 | 不出方向信号;只做早报叙述,以及(后置、可选)文本→结构化字段 |
| 数据源 | 主用 moomoo;验证/回填用 yfinance 免费日线;AV 留免费档;内部人用 SEC EDGAR;有必要再付费 |
| 大师 | 退役,不进决策路径;周一/四继续生成只供公开站 |
| 纸面账本 | 新 `source='agent'` 期权仓 + 股票级信号表 |
| 铁律 | 只读、永不下单;仓位不上公开站;LLM 调用全部密封 |

---

## 1. v1 → v2 改了什么(第二轮对比的结论)

| # | v1 的做法 | 问题(证据) | v2 的做法 |
|---|---|---|---|
| 1 | 用**股票收益截面 IC** 决定信号能不能用,然后去**买期权** | IC 衡量的是相对(去均值后)收益,而买 call 赚的是绝对收益,大盘一跌全亏。平值期权买方平均亏钱(Coval & Shumway:零 beta 平值跨式每周约 −3%)。粗算:37 天平值期权权利金约为现价的 3.4%;持有 7 天,VRP 损耗约 0.10%,往返价差 0.1–0.17%;按 delta 0.5 折算,股票要多涨约 0.5% 才回本。IC 0.02 在 top-5 上只能带来约 0.12%。 | **两道门**:① 方向分,检验"极端 5 名的原始 7/14 日收益"和"对照期权回本点的命中率";② **期权便宜门**,预测持有期 RV ≥ IV × k,且预期波动 ≥ 回本 + 价差。晋升标准改成**期权账本自身的期望收益**(预先定样本量),股票 IC 只作为前置筛选。`iv_rv`/`iv_rank` 从"方向信号"改为"选合约的门"。 |
| 2 | 阈值:rank-IC ≥ 0.02、日频 ICIR ≥ 0.30、NW t ≥ 2.5;安慰剂 `shifted(60)`/`date_shuffled` 必须约 0 | Qlib 的 LightGBM 在 158 个特征、CSI300 上 ICIR 也才 0.37,线性模型 0.30。单因子在美股大盘不可能达到,等于注定"没有信号通过"。慢信号(动量、价值、内部人)滞后 60 天后排名几乎不变,安慰剂会误杀好信号。按日打乱 ticker 的置换会破坏 IC 的自相关,p 值偏小。 | rank-IC ≥ 0.01(h = 实际持有期);ICIR 改用不重叠的周频 IC,门槛约 0.1;NW t:已发表因子复现 ≥ 2,新假设 / LLM 提出的 ≥ 3(Harvey–Liu–Zhu);置换改为**整个面板统一打乱 ticker**;bootstrap 改为 Politis–White 自动块长的平稳 bootstrap;`lookahead`/`planted` 只做工具自检;安慰剂改用"整面板置换零分布"。 |
| 3 | universe = 今天市值 ≥ $5B + 期权活跃,冻结后回填到 2014 | 这不只是幸存者偏差,还是**用今天的结果选样本**:留下的都是后来涨大、变热门的票,会系统性抬高动量 IC、压低反转和价值。 | 用**时点正确的 S&P 500 成分**(免费,github.com/fja05680/sp500),只保留"当日是成分股"的 (ticker, 日期);2019 年后再叠加当日的 ADV 过滤;报告同时给"当日成分"和"2026 冻结名单"两种 IC,差值就是偏差估计;yfinance 拿不到的退市股计数记录。 |
| 4 | 权重 = ICIR 加权,用同一段数据回放来"验证" | 同一数据拟合又打分,是循环论证;重叠标签没有 purge/embargo;每天只买 5 只,广度极小(IR ≈ 0.02 × √260 ≈ 0.3,还没扣期权成本);没有持仓黏性,会频繁换票;实盘单笔期权收益的标准差约为权利金的 50–80%,要验证 +3% 的边际需要约 1,600 笔独立交易,"3 个月 IC < 0 就退役"会被噪声触发。 | 默认**等权 z 分合成**;只有 purged walk-forward(扩展窗口、5 日 embargo)样本外胜过等权才改 ICIR 加权;对 `family_log` 里所有试过的变体报告 **deflated Sharpe / PBO**;加**持仓黏性**(排名掉出前 15 才换);**预先写死评估点**(N 笔已结算或某个日期),日常只显示滚动 CI,不看每天的命中率;退役窗口相应拉长。 |
| 5 | 阶段顺序:动量 → … → 期权信号最后(等 12 个月数据) | 12-1 动量是月频慢因子,和 7–14 天的期权持有期不匹配;原始 5 日反转在大盘股很弱,扣掉行业/市场后的残差反转利润约翻倍且在大盘股仍存在(Blitz et al.);PEAD 在大盘股 2006 年后基本为 0(Martineau 2022);≥$5B 名单里内部人集中买入很少;期权隐含类信号恰好在周频、期权流动好的名单上有文献支持(Cremers–Weinbaum:call 相对贵的票比 put 相对贵的每周多约 50bp,但样本期内在衰减;Xing–Zhang–Zhao:skew 最陡的年化跑输 10.9%)。 | **期权信号提前**:先探 DoltHub 免费期权库(`post-no-preference/options`,2019→),不行就考虑付费 EOD IV 历史;新主信号 `iv_spread`(同行权价 call−put IV),`skew_25d` 为次;`reversal_5` 换成**残差反转**;`mom_12_1` 只做对照/慢倾斜;PEAD、价值/质量降为"预期 FAIL、便宜的一次性检查";moomoo 夜间期权链归档第一天就开始攒实盘数据。 |

其他修正(每条一行):
- **AV 新闻多票查询是"同时提到所有票"**。实测:9/2 单查 AAPL 23 条、单查 RKLB 3 条,查 `RKLB,AAPL` 0 条。改成不带 ticker(或按 topic)、`limit=1000`、本地按 `ticker_sentiment` 过滤。
- **AV 新闻回填不是时点正确的**(历史文章的摘要和情绪是重新生成的),一律视为不可检验,只用实盘爬取的数据验证。
- **标签对齐执行时点**:信号来自前一日收盘,次日开盘附近成交,标签改为从实际入场时点起算;加 h = 10(约 14 自然日),主期限 = 实际持有期。
- **Regime 门**:VIX 高本身就意味着期权贵,已被 #1 的便宜门覆盖;VIX ≥ 30 只保留为熔断开关。
- **`min_conviction`**:按 |composite| 取 top-K 时它不起作用,改成"预期波动 / 回本点"的下限。
- **多重检验**:`family_log` 里每个 (信号, 变体, 期限) 都算一次尝试;P6 若让 LLM 提因子,加 AlphaAgent 式原创性(AST 相似度)惩罚。
- **yfinance 在 2025–26 常被限流封禁**:历史只抓一次存下来,之后只对账近期窗口,不做每周全量重抓;钉版本号,显式 `auto_adjust=False`。
- **moomoo 盘后期权链**:bid/ask 是陈旧的,改在收盘前 15:45 ET 快照,或保存 moomoo 自带的 IV 字段和时间戳;OI 每晚才公布一次,滞后 1 日使用。
- **价值/质量用当前股数**是 look-ahead:改用 SEC XBRL companyfacts 的 `shares_outstanding`(免费、按申报日期时点正确),或直接跳过 P5。
- **SEC insider 季度数据集**确认可用:注意 4/A 修订和重复持有人行,过滤 `TRANS_CODE='P'` 且 `TRANS_PRICEPERSHARE>0`。
- **工作量**:v1 在拿到任何收益证据前要 15–17 个 session,正好重复 §9 的教训;v2 先做 6–7 个 session 的最小闭环。

v1 里**保持不变**的部分:决策路径不放 LLM;预注册阈值带版本号、`family_log`、candidate → validated → retired 生命周期(只调整数字);信号全 universe 计算、`predict()` 查表;面板层保证时点正确;弃权 ≠ 中性;检验工具自检(planted 通过 / 噪声失败 / lookahead 被标出);影子模式是一等公民;固定 7/14 天结算;`optradar_rule` 和"永远猜多数"作为基准;从 agent 框架里只保留决策日志、逐笔归因、持久化回本点,不要多空辩论和 LLM 反思。

---

## 2. 开源框架对比(v1 调研,保留)

| 框架 | LLM 的位置 | 信号→仓位 | 风控 | 验证 | 独立评测结论 |
|---|---|---|---|---|---|
| TradingAgents(107k★) | 全部:提取+辩论+决定 | 文字辩论→买/卖/持,无仓位 | 口头"风险辩论" | 3 个月、3 只票、无成本、在训练窗内 | 77 篇审计最低可复现级;look-ahead 修复到 2026 才做 |
| FinMem / FinAgent | 决策者,带分层记忆 | 单股买/卖/持 | 无 | 6 个月、5 只票 | FINSABER 20 年×100+ 票重跑:Sharpe −0.23 / 0.24 vs 买入持有 0.70 |
| Trading-R1 | 4B 微调决策者 | 5 档标签 | 无 | 2024 年内测试 | 作者自认多头偏置;无代码 |
| FinRL-DeepSeek | 新闻打分×RL 动作 | RL 策略 | CVaR、turbulence 熔断 | NDX 2019–23 | 信息比率≈0;LLM 注入越多越差 |
| ai-hedge-fund v2(上游,63k★) | persona → [−1,1] | 加权均值→去均值→归一→硬 clamp | 每票/总敞口上限,审计事件 | 单一代码路径、PIT 快照 | 无公开业绩;我们 §7 证伪了 persona |
| Qlib + RD-Agent(Q) | 研究员:提假设、写因子代码 | Qlib ML + TopK-dropout | Qlib 回测约束 | CSI300 2017–20 | 唯一有正面机制证据的 LLM 用法;LLM 在实盘环路外 |
| FinRobot | 叙述者 | 不交易 | — | — | "数字代码算,文字 LLM 写,输出有来源" |
| nof1 Alpha Arena(真钱) | 唯一交易员 | 直接下单 | 杠杆上限 | 2 周真钱 | 中位模型亏损;手续费吃掉收益;可迁移:写计划并每次喂回 |
| Lean / freqtrade / Jesse | 无 | 显式规则/模型 | 显式 | walk-forward、look-ahead 检测、蒙特卡洛 | 行业骨架 |

反复出现的失败模式:① 模型知识窗内回测全污染,遮蔽 ticker/日期无效;② 玩具窗口和 universe;③ 无成本、无退市、无执行语义;④ 牛市胆小、熊市鲁莽;⑤ 推理能力 ≠ 交易能力;⑥ 相同输入不同交易;⑦ 口头风控不是风控;⑧ 星数与证据无关。

新闻情绪→次日收益(Lopez-Lira & Tang,JFE 2026):效应真实,但集中在小盘、负面新闻、隔夜;Sharpe 从 6.5(2021Q4)衰减到 1.2(2024);20bp 往返成本下归零;部分样本内效应是模型记忆。

**我们与所有 LLM-agent 框架的根本区别**:决策路径上没有 LLM。v2 进一步:**决策的最终裁判是期权账本的期望收益,不是股票 IC**。

---

## 3. 目标架构(v2)

```
数据层   moomoo(实盘行情/期权链/持仓) · yfinance(历史日线,一次抓取) · 时点 S&P 500 成分 · DoltHub 期权/IV 历史(待探) · AV 免费档(不带 ticker 的新闻 + 国会) · SEC EDGAR(insider、XBRL 股数) · optradar chain_raw
   │
面板层   ~/.hedge-fund/agent/panel.db:bars、index_daily(SPY/^VIX)、membership(PIT)、iv_daily、chain_daily、news_sentiment、insider_tx、earnings_events、signal_cache
   │
信号库   CrossSectionalModel(QuantModel):全 universe 按日计算 → Signal(value ∈ [−1,1], components)
   │
检验层   Alphalens 式 tearsheet(IC 分期限、五分位原始/超额收益、首尾 5 名原始收益、换手)
   │      + NW t + 平稳 bootstrap + purged walk-forward;后期再加 FWER / deflated Sharpe(置换仅作诊断,见 §9)
   │
决策层   门 ①:方向 = 等权 z 合成(持仓黏性:掉出前 15 才换)
   │      门 ②:期权便宜 = 预测 RV(持有期)≥ IV·k 且 预期波动 ≥ 回本 + 价差
   │      beta 处理:多头 call 配空头端 put,或 SPY 对冲腿;VIX ≥ 30 熔断
   │
输出层   agent_signals / agent_picks → horizon.pick 选合约(或借方价差)→ paper_ledger source='agent' → 7/14 天结算
   │      评估:预先写死 N 笔 / 日期,滚动 CI
   │
叙述层   (后置)密封 LLM 只复述数字
```

---

## 4. 最小闭环:6–7 个 session 拿到第一份真证据

| 步骤 | 内容 | 产出 / 停走标准 | 工作量 |
|---|---|---|---|
| **S1 面板 + PIT universe** | 装 `duckdb==1.5.5`、`yfinance`(钉版本)到 `~/.hedgefund-venv`,记在 `requirements-agent.txt`;`PanelStore` + DDL;时点 S&P 500 成分 ∩ 期权流动性;yfinance 日线一次抓取 2014→ + SPY/^VIX;最小 tearsheet(IC h = 1/5/10、五分位原始和超额收益、首尾 5 名原始收益、换手)+ 平稳 bootstrap + 整面板置换零分布;planted/noise 自检 | 回填缺失 < 2%;planted 通过、noise 失败;报告"当日成分 vs 冻结名单"的动量 IC 差 | 1 |
| **S2 期权边际测量(先于任何新信号)** | 用 optradar 纸面账本已有记录 + 探 DoltHub IV 历史:对每个历史 radar pick,算回本点 vs 实际波动、持有期 IV vs 实际 RV | 直接回答"任何方向分能否越过回本点",并**把需要的 IC 换算成期权口径**,作为 S3 的门槛;若 DoltHub 不可用,记录并决定是否付费 | 1 |
| **S3 三个信号** | 残差反转(对行业/市场回归后的 5 日残差)、`iv_spread` / `skew_25d`(DoltHub 历史)、`mom_12_1`(只做对照);等权 z 合成;purged walk-forward | 合成在样本外的首尾 5 名原始收益是否越过 S2 的门槛;不过 → 影子模式上线,照样记账 | 1 |
| **S4–S5 账本接线** | `agent_signals` / `agent_picks` 表;`source='agent'`;期权便宜门;beta 处理(配对 put 或 SPY 腿);持仓黏性;预先写死评估 N;optradar 3 处小改;早报 ⑨ 节(纯数字,无 LLM) | selftest 隔离跑绿;实盘第一周每天有 `agent_signals`,agent 行被 mark;以影子或实盘模式上线 | 1–2 |
| **S6 开始攒时点数据** | moomoo 夜间 / 15:45 ET 期权链归档;AV 不带 ticker 的新闻爬取(本地过滤) | 只攒数据,不参与决策;2 周后归档缺日率 < 5% | 1 |
| **之后(仅当 S3–S4 显示出东西)** | EDGAR insider 轨道;ICIR 加权(须胜过等权);regime 细化;叙述者;FWER / deflated Sharpe 全套;P6 LLM 研究员(带原创性惩罚);PEAD、价值/质量作为预期 FAIL 的一次性检查,不过就删 | — | 按需 |

### 生产部署前提
本机 optradar 是干净 clone,没有 db/out/.env/plist。S4–S5 的 launchd 接线要等迁移第 6–7 步完成(旧机器数据、plist)后在生产机做。

---

## 5. 关键实现约定(从 v1 保留并按 v2 修正)

### 5.1 包布局
```
hedge-fund/
  requirements-agent.txt             duckdb==1.5.5, yfinance==<钉死>
  hedge_fund/features/panel.py       PanelStore + PanelDataClient
  hedge_fund/features/rv.py          yang_zhang(), close_to_close(), har_forecast()
  hedge_fund/signals/xs.py           CrossSectionalModel(QuantModel)
  hedge_fund/signals/{reversal,momentum,vol_surface}.py
  hedge_fund/validation/{tearsheet,stats,placebo,walkforward,registry}.py
  agent/{universe,backfill,daily,ledger,brief}.py
  agent/sources/{sp500_membership,dolthub_iv,moomoo_chain,av_news}.py
  agent/tests/
optradar/(3 处小改)
  radar/ledger.py   SOURCES = ("budget","atm","agent")
  radar/report.py   标签表加 "agent"
  bin/run_and_notify.sh   agent 块在 run_daily 之后、masters 之前
```
- 两个 venv 都有 moomoo-api + PyYAML,agent 在 3.13 venv 里用 `sys.path` 直接 import optradar 的 `sources/moomoo_src.py`、`radar/horizon.py`、`radar/ideas.py`、`radar/ledger.py`。
- DuckDB:钉 1.5.5 与 optradar 同版本;agent 只在最后一步短暂开读写(≤ 2 秒),带重试;面板库是独立文件。

### 5.2 信号 API
- `raw(as_of, tickers)` → `compute(as_of)`:1/99% winsor → 稳健 z = (x − median)/(1.4826·MAD) → value = clip(z/3, −1, 1),缓存到 `signal_cache`。
- `compute()` 永远跑全 universe(当日成分),`predict()` 只查表。
- `components` 恒含 `raw, z, pct_rank, n_names`(全部数值);数据不足时 `abstained=True`。

### 5.3 S3 信号公式
| 信号 | raw | 符号 | 说明 |
|---|---|---|---|
| `resid_reversal_5` | −(过去 5 日收益对 SPY 和行业 ETF 回归后的残差累计) | + | 回归窗 60 日;Blitz et al. |
| `iv_spread` | 同一行权价 call IV − put IV,取 ATM 附近成交活跃档的 OI 加权均值 | + | Cremers–Weinbaum;call 相对贵 → 看涨 |
| `skew_25d` | (iv@δ≈−0.25 put − iv@δ≈+0.25 call)/iv_atm | − | Xing–Zhang–Zhao |
| `mom_12_1`(对照) | A[−21]/A[−252] − 1 | + | 只做对照和慢倾斜 |
| `optradar_rule`(基准) | `radar/ideas.py:44-51` 原样 | — | 预期失败,量化"方向已证伪" |

### 5.4 期权便宜门(门 ②)
- 预测持有期 RV:HAR(日、周、月 YZ RV)或 YZ20;对比所选合约 IV。
- 入场条件:`RV_forecast ≥ IV × k`(k 预注册,初值 0.9)且 `|预期波动| ≥ be7 + spread`(`radar/horizon.py` 已算 `flat7/be7`)。
- 不满足时:改用借方价差,或当天跳过这只票(记录原因)。

### 5.5 预注册阈值(v2,`thresholds_version: 2`)
1. h = 实际持有期(5 / 10 个交易日),标签从实际入场时点起算。
2. rank-IC ≥ 0.01,符号与预注册一致。
3. 不重叠周频 IC 的 ICIR ≥ 0.1。
4. NW t:已发表因子复现 ≥ 2,新假设 ≥ 3。
5. 平稳 bootstrap 95% CI 不含 0(v2.1:整面板置换不再作为门槛,见 §9)。
6. 2015 年后子样本符号一致。
7. 对 `mom_12_1` 的 partial IC ≥ 0.005。
8. **期权口径**:首尾 5 名原始收益 > S2 换算出的回本门槛(扣价差)。
9. `family_log` 每个 (信号, 变体, 期限) 计一次尝试;合成报告 deflated Sharpe。

### 5.6 账本
- `agent_runs`、`agent_signals`(含 `__composite__` 行)、`agent_returns`(1/5/10/20 日、从入场时点起算)、`agent_picks`(含"便宜门"是否通过和原因)写进 optradar.db,DDL 由 `agent/ledger.py` 自带(仿 `bin/congress.py`)。
- `open_agent_positions`:top-K = 5 按 composite,持仓黏性(前 15 内不换),过期权便宜门,市场 live 才开;`direction` 用 `'看涨'/'看跌'`(`dir_hit7` 比较的就是这两个字符串);`id = f"{snap_date}|{code}|agent"`;`mark()` 不分 source,自动结算;`scoreboard()` 改循环 `SOURCES`。
- **评估点预注册**:在 `agent/config.yaml` 写 `evaluate_at: {n_closed: 200, or_date: 2027-06-30}`;早报只显示滚动均值与 CI、距评估点还差多少,不做中途判决。

### 5.7 叙述者(后置)
只收数字 JSON;输出 `{headline, commentary, caveat}`;输出中每个数字必须出现在输入里,否则回落模板句;采样 1 次;缓存键用 `ClaudeCodeLLM.cache_key`。

---

## 6. 测试
- 合成面板:标准化、弃权、子集不变性、时点(改 as_of 之后的 bar 输出不变)。
- tearsheet:planted(IC 0.05)在 300 日×150 票上通过;纯噪声在 20 个种子里 ≥ 95% 失败;整面板置换在零假设下 p 近似均匀;平稳 bootstrap CI 覆盖真值;NW t 与手算 AR(1) 一致。
- 残差反转:植入行业因子后,原始反转与残差反转的 IC 差符合预期;`iv_spread`/`skew` 用 3 档期权链 fixture。
- 期权便宜门:给定 IV、RV、be7、spread 的边界用例。
- 账本:临时 DuckDB,DDL 幂等、`source='agent'` 插入、id 唯一、方向字符串、持仓黏性、`scoreboard` 出 agent 行。
- `agent/tests/selftest_agent.sh`:仿 `bin/selftest.sh`,`OPTRADAR_CONFIG` 隔离,真实 `optradar.db`/`out/` 不被触碰。

## 7. 风险
- 诚实结果可能是"没有东西越过期权回本点"。那也是有价值的结论:说明买方期权的方向策略在这个 universe 上不成立,应转向价差或卖方结构研究。
- DoltHub 数据质量和时效未验证;不可用时 S3 的期权信号要么付费、要么等 moomoo 归档攒数据。
- 时点成分只覆盖 S&P 500;退市股 yfinance 拿不到,计数记录。
- yfinance 限流;AV 配额与 `bin/congress.py` 共享;SEC 需要 `SEC_USER_AGENT`。
- 期权账本把方向、vol、theta 混在一起;股票级收益表作为辅助诊断。

## 8. 端到端验证
1. `~/.hedgefund-venv/bin/python -m pytest -q hedge_fund integrations agent`(上游自带的 5 个 Jev 显示名失败不算)。
2. tearsheet 自检:planted 通过、noise 失败。
3. S2 报告:历史 radar picks 的回本点 vs 实际波动分布,换算出的 IC 门槛。
4. S3 walk-forward 报告存 `site-data/validation/`,逐条核对 5.5 的阈值。
5. `selftest_agent.sh` 隔离全链路跑绿(需 OpenD 登录)。
6. 生产机接入后第一周:每天有 `out/agent/<date>.json`、`agent_signals` 行数 = 当日 universe、第 8 天起 agent 行有 `ret7_*`。

## 9. 执行记录

### S1(2026-09-19,分支 `agent-s1`)— 完成
报告:`site-data/validation/s1_2026-09-19.md`。

- **面板**:S&P 500 时点成分(1996→2026-08-18,2,720 次变更)+ yfinance 日线 2012-11→2026-09-18,621 只有数据,158 万行。
- **覆盖率**(当日成分中有数据的比例):2015 年 76% → 2020 年 90% → 2026 年 99%。缺的基本是后来被收购或退市的公司(K、IPG、HES、WBA、JNPR、HOLX 等),Yahoo 删除了它们的全部历史——这是剩余的幸存者偏差。改名用经核实的别名表处理(20 个);合并不做别名。可选补救:Tiingo 免费档(含退市股,需注册)或 Stooq。
- **冻结名单的偏差**:用 2026 年的成分回测,早年覆盖率只剩 65%,而且**所有票的平均收益被抬高**(5 日 +0.257% vs 当日成分 +0.220%;10 日 +0.550% vs +0.475%;首 5 名 10 日 +1.15% vs +0.93%)。偏差主要体现在收益水平上,IC 反而略低。对期权买方,收益水平恰恰决定能不能越过回本点,所以 S2 必须用当日成分。
- **信号**(当日成分,入场 = 次日开盘):
  - `mom_12_1`:h=5 IC +0.008,NW t 0.93,CI 含 0 → 不显著,符合预期(月频因子、周频期限)。
  - 原始 `reversal_5`:h=5 IC +0.011,NW t 2.00,CI [+0.0001, +0.022],92% 的年份为正;首 5 名 5 日原始收益 +0.41%,全体 +0.22%,即每 5 天约多 0.19%——低于 §1 #1 粗算的约 0.5% 期权回本门槛。S3 的残差反转要看能否明显更强。
- **工具自检**(真实面板):植入 IC 0.02 → 测得 +0.018,t 20.6;纯噪声 20 个种子误报率 0%。
- **对 v2 的修正(v2.1)**:第二轮对比建议的"整面板置换零分布"不适合作为均值 IC 的门槛。实测其零分布标准差 0.003,而动量 IC 均值的 NW 标准误差是 0.010,窄 3 倍——打乱标签的同时也去掉了因子收益随时间的波动,所以它检验的是"有没有任何关联",会系统性过度乐观。门槛只用 NW t + 平稳 bootstrap;置换留作可选诊断(`tearsheet(n_perm>0)`)。

### S2(2026-09-19)— 期权口径的门槛已量化
报告:`site-data/validation/s2_2026-09-19.md`;脚本 `agent/s2_option_hurdle.py`(复用 optradar `radar/horizon.bs`,口径与早报一致)。

- **免费历史期权价拿不到**:DoltHub 的 SQL API 已下线(连官方示例库都 404);AV `HISTORICAL_OPTIONS` 连 claude.ai 连接器也是付费端点。因此用模型度量:`IV = k × Yang-Zhang RV20`,37 DTE 平值,持有 7/14 自然日按 BS 以不变 IV 重定价,扣一次往返价差(2.0%,取自真实 picks 的中位数)。变化的部分(实际涨跌)是真实数据,2015→2026、每 5 个交易日一次、26 万次模拟。
- **真实锚点**(雷达 34 个 picks):IV/YZ-RV20 中位数 1.04,价差中位数 2.0%,7 天回本门槛 1.95%、1σ 6.53%。
- **门槛**(模型中位 IV 26.7%,7 天):回本需顺向 **0.8%–1.0%**,而全样本 7 天 |涨跌| 中位数只有 2.15%,随机买 call 只有 43% 能越过自己的门槛。
- **需要多少方向优势**(随买方付的波动率溢价而变):

| IV / 之后实际波动 | 回本门槛 | 随机买 call 的平均收益 | 需要额外的顺向漂移 |
|---|---|---|---|
| 1.00(公平定价) | 0.78% | +0.5% | −0.03% |
| 1.10 | 0.85% | −1.2% | **+0.08%** |
| 1.20 | 0.92% | −2.6% | **+0.19%** |
| 1.30 | 0.99% | −3.8% | +0.30% |

- **最重要的发现:门槛必须按每只票自己的 IV 算,不能用统一数字**。7 天口径下,随机票的平均门槛 0.91%,而 `reversal_5` 前 5 名是 **1.50%**——刚大跌的票波动率高、期权贵。反转组平均涨幅 +0.50%(优于随机 +0.25%),期权收益反而更差(−1.8% vs −0.2%);"涨幅 − 自身门槛"随机 −0.67%,反转 −1.00%,动量 −0.92%。**S1 里唯一勉强显著的信号,换成期权口径后反而更差。**
- **结论**:① 信号的评价指标改成 `涨幅 − 自身回本门槛`,而不是 IC 或原始涨幅;② 门槛 ② 的"期权便宜"不是可选项,是必需项——把高 IV 的票直接排除,或改用借方价差降低 vega/theta;③ 一个只有方向、不看波动率定价的信号,要在 VRP 1.1–1.2 下每 7 天多带来 0.1%–0.2% 的漂移,S1 的信号都达不到。
- **模型局限**(写进报告):IV 不随价格变动(真实世界跌时 IV 涨、买 call 会再吃一次 IV 下杀)、无微笑/偏度、价差固定 2%、不含财报。所以这是**乐观上界**,真实门槛只会更高。

### S3(2026-09-19)— 没有方向信号通过;唯一稳健的是"便宜门"
报告:`site-data/validation/s3_2026-09-19.md`;脚本 `agent/s3_signals.py`。评分口径按 S2 改成期权收益 + "涨幅 − 自身回本门槛",IV = 1.15 × YZ-RV20。测了 3 个方向信号 × 4 种便宜门 × 多空两侧 × 2 个持有期 = 48 个组合。

- **便宜门本身值约 3 个百分点**(7 天持有):随机选票不设门 −2.5%,加上"低 IV + 波动率回归"两道门变成 +0.4%;所有方向信号都同步抬升 2–4 个百分点。这是目前唯一稳定、可解释的效应,和 S2 的结论一致。
- **方向信号没有增量**:最好的组合 7 天 t 值只有 1.34,14 天最高 2.55;扣掉 48 次尝试的多重检验(Holm)后全部不显著。前后半段符号经常反转。
- **"平均涨幅 − 自身门槛"全为负**(−0.5% 到 −3%):即便平均收益为正的组合,也是靠少数大赢家的凸性,不是靠"通常能越过门槛"。胜率普遍只有 40%–44%,估计值噪声很大。
- **登记为候选假设(不启用)**:14 天持有、对 12-1 动量最弱的票买 put、限定低 IV 一半 → +7.7%,t 2.53,75% 的年份为正,前后半段都为正。方向上属于"动量在空头侧延续",有文献基础,但本次是在 48 个组合里挑出来的,必须用新数据独立验证,写入 `family_log`。
- **结论**:按预注册阈值,**没有信号可以晋升**。agent 以影子模式上线:每天照常计算并记录所有候选信号和"若启用会选哪几只",但不开纸面仓,直到某个信号在新数据上独立通过。这正是计划里预设的合法结果。

### S4–S6(2026-09-19)— 影子模式上线,时点数据开始积累
- **S4–S5 接线完成**(`agent/ledger.py`、`agent/daily.py`、`agent/brief.py`;optradar 分支 `agent-wiring` 已并入 main):
  - optradar.db 新增 `agent_runs / agent_signals / agent_picks / agent_returns`(自带 DDL,仿 `bin/congress.py`;按列名而非字典顺序插入;写锁只占数秒并带重试)。
  - `agent/daily.py`:增量更新面板 → 按当日成分股算全部候选信号 → 过便宜门 → VIX ≥ 30 熔断 → 写库 + `out/agent/<date>.json`。**影子模式:不开任何纸面仓**,`MODE` 改成 `live` 才会开。
  - 早报新增 ⑨ 节(纯数字,无 LLM),推送文案加一行;重复插入幂等。
  - 实测:503 只成分股,便宜门通过 236 只,30 个候选选择。
  - optradar 顺手修了限频静默跳过:拿不到到期日/期权链现在会记进 `rejected` 并写明原因(9/19 真实踩到过,重跑就恢复)。
- **S6 采集已上线**(两个新 launchd 任务,plist 存 `agent/launchd/`):
  - `com.louis.agent.chain` 工作日 12:45 PT(15:45 ET,收盘前,避免盘后陈旧报价)→ `agent/sources/moomoo_chain.py` 归档 ATM±8 档期权到 `panel.chain_daily`。这是 S2 缺的真实 IV 记录,攒够 12 个月才能验证真正的"期权便宜"信号。
  - `com.louis.agent.news` 工作日 13:15 PT → `agent/sources/av_news.py`。实测一次调用覆盖约 25 小时、1000 篇文章、1100 条 S&P 成分股记录(347 只票);配额账本与 `bin/congress.py` 共享,周六自动让出 22 次。
- **下一次决策点**:积累 3 个月实盘记录后,用 `agent_picks × agent_returns` 做一次实盘 IC 复核;期权链攒够 12 个月后重跑 S2/S3,用真实 IV 替换模型 IV。在此之前不加新信号(§9 的教训)。

### S7(2026-09-19)— 同样的信号做股票:没有期权门槛,也没有超额
起因:用户指出不只做期权,可以像 balder-ai.com 那样分长线/短线做股票。S2 的门槛是期权特有的,股票只需要扣交易成本,所以同样的信号值得单独测一遍。报告:`site-data/validation/s7_2026-09-19.md`,脚本 `agent/s7_stock_books.py`(当日成分股、次日开盘入场、每边 5bp 成本、前 20 名)。

| 书 | 信号 | 持有 | 多头年化 | 超额(减市场) | 多空 | NW t(超额/多空) |
|---|---|---|---|---|---|---|
| 短线 | reversal_5 | 5 日 | +16.0% | +3.7% | +5.1% | 0.62 / 0.50 |
| 短线 | reversal_5 | 21 日 | +14.0% | +1.7% | +1.5% | 0.67 / 0.39 |
| 长线 | mom_12_1 | 21 日 | +15.8% | +3.5% | +3.4% | 0.72 / 0.38 |
| 长线 | mom_12_1 | 63 日 | +16.9% | +4.8% | +6.3% | 1.16 / 0.88 |

- **多头腿看着很好(t 2.4–3.4),但那是市场 beta**:同期市场本身 63 日 +3.51%(约年化 14.7%)。扣掉市场之后,超额全部不显著,bootstrap 区间都含 0,多空腿同理。
- **和 KTD-Fin 对 LLM agent 的结论一致**:收益主要由被动市场暴露和风格暴露解释,选股 alpha 证据有限。
- **结论**:去掉期权门槛后信号并没有"活过来",只是从负变成约等于零。所以**不是期权口径的问题,是这几个信号本身没有边际**。股票书同样进影子模式,不单独上线。
- **真正的差异点在数据**(Balder 的做法值得抄的部分):13F 机构持仓、Form 4 内部人、13D 举牌、带日期的催化剂——全都在 EDGAR 免费、按申报日期天然时点正确,而我们目前一条都没接。下一批候选信号应该从这里来,而不是继续在价格序列上翻找。

### S8(2026-09-20)— Form 4 内部人买入:方向对,但仍未过阈值
第一个来自一手文件而非价格序列的信号。数据:SEC 季度 Form 345 批量数据集,2015q1→2026q2,**32.9 万条**交易、902 只票,其中公开市场买入 2.5 万条;按**申报日期**(不是交易日期)入场,次日开盘。脚本 `agent/sources/sec_form4.py` + `agent/s8_insider.py`,报告 `site-data/validation/s8_2026-09-20.md`。

| 变体 | 5 日超额 | t | 10 日超额 | t | 21 日超额 | t | 两半是否同号 |
|---|---|---|---|---|---|---|---|
| 任意买入(5424 次) | +0.02% | 0.27 | −0.08% | −0.64 | −0.20% | −0.90 | 否 |
| **金额 ≥ $250k(2449 次)** | **+0.17%** | **1.78** | **+0.28%** | **1.95** | +0.26% | 1.16 | **是** |
| 集中买入 ≥2 人(577 次) | +0.21% | 0.99 | +0.08% | 0.30 | +0.10% | 0.25 | 是 |

- **有效的维度是金额,不是人数**:文献常用的"集中买入"在我们这里样本太少(577 次);按金额筛选后单调为正、前后半段同号、83% 的年份为正(21 日)。
- **但仍不显著**:最好的 10 日 t = 1.95,bootstrap 区间含 0;按预注册规则(已发表因子复现需 t ≥ 2,且本次测了 12 个组合要计入多重检验)**不予晋升**,登记为候选。
- **63 日全部转负**,说明即便有效也是短窗口效应,不是长期持有的理由。
- **universe 是主要限制**:内部人效应的文献证据集中在小盘股,而 S&P 500 大盘股里高管公开市场买入本来就罕见(10 年只有 2449 次 ≥$250k)。要检验这个信号,需要把股票池扩到中小盘——但免费的时点成分数据只有 S&P 500,这是下一个要解决的数据问题。

### S9(2026-09-20)— 时点股票池建成,但免费价格数据撑不起中小盘
- **股票池已解决(免费)**:用 SEC Form 4 的 `SUBMISSION.tsv` 反推每个季度的在市公司(每份文件都带当时的代码和 CIK,**包含后来退市的**)。`panel.issuer_seen`:2015q1→2026q2,**14,331 只票、21.2 万条季度记录,每季度约 4,500 只在市**。这是免费、时点正确、无幸存者偏差的名单——`company_tickers.json` 只有当前公司,做不到这点。
- **价格数据是真正的墙**:从 2016q1 的非标普成分里随机抽 300 只,用 yfinance 查当年日线,**只有 44% 拿得到**(标普同期是 76%)。缺的全是后来被收购或退市的(UBSH、BONT、SFLY、NUAN、DWA…),缺失与结果强相关,用它做中小盘研究会系统性高估收益。
- **Stooq 已不可用**:现在返回机器人验证页,程序抓不到。
- **结论**:S8 的瓶颈无法用免费数据绕开。要检验内部人/13F/13D 这一类信号,必须有含退市股的价格源。三条路:
  1. **付费 EODHD All World ≈ $20/月**:明确包含退市股,覆盖 14k 票的完整历史,同时带基本面。一次性解锁整类信号的检验。
  2. **先试 Alpaca 免费档**(需注册):7 年以上历史,退市覆盖未知,值得先测一遍再决定花钱。
  3. **留在标普 500**:接受"内部人/机构类信号无法被公正检验"的现实,不再往这个方向投入。

## 参考
- 期权收益:Coval & Shumway (2001) https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00352 · Goyal & Saretto (2009) https://personal.utdallas.edu/~axs125732/CrossOptionsJFE.pdf · Cao & Han (2013) https://www-2.rotman.utoronto.ca/facbios/file/Han_JFE_published.pdf
- 期权隐含信号:Cremers & Weinbaum https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968237 · Xing, Zhang & Zhao https://www.ruf.rice.edu/~yxing/option-skew-FINAL.pdf
- 因子评测:Qlib 基准 https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md · Harvey, Liu & Zhu https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF · McLean & Pontiff https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623 · Bailey & López de Prado(PBO)https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253 ·(Deflated Sharpe)https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- 信号:Blitz et al.(残差反转)https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1911449 · Martineau(PEAD 消失)https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3111607 · Lopez-Lira & Tang https://arxiv.org/abs/2304.07619
- 数据:时点 S&P 500 成分 https://github.com/fja05680/sp500 · SEC insider 数据集 https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets · DoltHub options https://www.dolthub.com/repositories/post-no-preference/options · yfinance 限流 https://github.com/ranaroussi/yfinance/issues/2422
- Agent 框架与评测:TradingAgents https://github.com/TauricResearch/TradingAgents · FINSABER https://arxiv.org/abs/2505.07078 · RD-Agent(Q) https://arxiv.org/abs/2505.15155 · AlphaAgent https://arxiv.org/abs/2502.16789 · Alpha Arena https://nof1.ai/ · Agentic Trading 审计 https://arxiv.org/abs/2605.19337 · Memorization Problem https://arxiv.org/abs/2504.14765
