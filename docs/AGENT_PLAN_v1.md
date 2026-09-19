# 多信号验证型交易 Agent — 详细计划

日期:2026-09-18 · 仓库:`~/hedge-fund`(v2-rebuild)+ `~/optradar`(main)· 实施后先复制一份到 `hedge-fund/docs/AGENT_PLAN.md` 入库。

## 0. Context:为什么做这件事

- 现状:optradar 每天 08:41 出买方期权雷达(方向靠 SMA20 + 5 日动量门,结构靠 Black-Scholes 持有期定价),5 位 LLM 大师周一/周四打分,纸面账本记 budget/atm 两种合约的 7/14 天结算。
- 已被证伪(hedge-fund/PLAN.md §7–§9,2026-09-13/15):LLM 方向预测(+5 日命中 55% vs "永远猜多数方向"基准 70%)、5 人委员会 = 单人(误差完全相关)、财报卖方"边际"是 30 日 IV 代理口径的产物。§9 结论:**"没有记分牌,加任何东西之前先闭环"**。
- 今天(9/18)已完成:LLM 调用密封(无工具/MCP、单轮)、类型化合约 + 3 次采样、缓存键带真实模型 ID。这堵住了"大师能偷看实时行情"的污染,但没有改变"大师没有预测力"。
- 目标:把动量、期权/波动率、新闻情绪、内部人、国会、PEAD、价值/质量整合成**一个 agent**,每个信号先过统一检验才能影响决策,每天的建议进纸面账本闭环;对照开源 agent 框架逐条避坑。

### 已拍板的决定
| 项 | 决定 |
|---|---|
| LLM 角色 | 不出方向信号。只做:① 文本→结构化字段提取(可选、后置),② 早报叙述,③(可选)研究假设提出 |
| 数据源 | 主用 moomoo;验证/回填用 yfinance 免费日线(标明来源);Alpha Vantage 留免费档(25 次/天);内部人改用 SEC EDGAR 免费数据;有必要再付费 |
| 大师 | 退役,不进决策路径;周一/四继续生成只供公开站 |
| 纸面账本 | 新 `source='agent'` 期权仓(沿用 horizon.pick 选合约、7/14 天结算)+ 股票级 `agent_signals` / `agent_returns` 表 |
| 铁律 | 只读、永不下单;仓位不上公开站;所有 LLM 调用走密封 `ClaudeCodeLLM` |

### 我替用户定的默认值(可改)
- 实施顺序按**数据就绪**排:P-1 基础 → P0 动量 → 合成+账本+早报(先跑通闭环,影子模式)→ P4 PEAD(数据现成)→ P3 内部人 → P5 价值/质量 → P1 新闻 / P2 期权(数据要攒 6–12 个月,代码先写、验证后置)→ 叙述者。
- `top_k = 5`,agent 仓沿用 `buyer.budget_usd = 550` 和 ③ 的流动性门。
- 验证 universe:市值 ≥ $5B + 期权成交量 ≥ 2000 + ADV20 ≥ $50M,约 250 只(输出是期权,流动性优先)。
- VIX 用 yfinance `^VIX`;公开站只展示信号注册表状态 + IC,不展示 picks;提取器(extractor)延后到 AV 原生情绪分有结论之后。

## 1. 开源框架对比(决定我们怎么做、不怎么做)

调研:TradingAgents(107k★)、Trading-R1、FinMem、FinAgent、FinCon、FinRobot、FinGPT、virattt/ai-hedge-fund(63k★,我们的上游)、Qlib + RD-Agent(Q)、FinRL(-DeepSeek)、nof1 Alpha Arena 真钱赛、StockBench / LiveTradeBench / FINSABER(KDD 2026)/ KTD-Fin / CLQT 等 2025–26 评测,以及 Lean / freqtrade / Jesse / vectorbt 等确定性框架。

### 1.1 各家怎么做决策
| 框架 | LLM 的位置 | 信号→仓位 | 风控 | 验证 | 独立评测结论 |
|---|---|---|---|---|---|
| TradingAgents | 全部:提取+辩论+决定 | 文字辩论→买/卖/持,无仓位 | 口头"风险辩论" | 3 个月、3 只票、无成本、在训练窗内 | 77 篇审计最低可复现级;look-ahead 修复到 2026 才做 |
| FinMem / FinAgent | 决策者,带分层记忆 | 单股买/卖/持 | 无 | 6 个月、5 只票 | FINSABER 20 年×100+ 票重跑:Sharpe −0.23 / 0.24 vs 买入持有 0.70 |
| Trading-R1 | 4B 微调决策者 | 5 档标签(波动调整收益分位) | 无 | 2024 年内测试 | 作者自认多头偏置;无代码 |
| FinRL-DeepSeek | 新闻打分×RL 动作 | RL 策略 | CVaR、turbulence 熔断 | NDX 2019–23 | 信息比率≈0;LLM 注入越多越差 |
| ai-hedge-fund v2(上游) | persona → [−1,1] | 加权均值→去均值→毛敞口归一→硬 clamp | 每票/总敞口上限,clamp 留现金,审计事件 | 单一代码路径,PIT 快照合约 | 无公开业绩;我们 §7 证伪了 persona |
| Qlib + RD-Agent(Q) | **研究员**:提假设、写因子代码 | Qlib ML + TopK | Qlib 回测约束 | CSI300 2017–20 | 唯一有正面机制证据的 LLM 用法;LLM 在实盘环路之外 |
| FinRobot | 叙述者 | 不交易 | — | — | "数字代码算,文字 LLM 写,每个输出有来源" |
| nof1 Alpha Arena(真钱) | 唯一交易员 | 直接下单 | 赛区杠杆上限 | 2 周真钱 | 中位模型亏损;手续费吃掉收益;可迁移技巧:写计划(目标/止损/失效)每次喂回 |
| Lean / freqtrade / Jesse | 无(Jesse 有 MCP 让 LLM 驱动研究) | 显式规则/模型 | 显式 | walk-forward、look-ahead 检测器、蒙特卡洛 | 行业骨架 |

新闻情绪→次日收益(Lopez-Lira & Tang,JFE 2026):效应真实但集中在小盘股、负面新闻、隔夜;Sharpe 从 2021Q4 的 6.5 衰减到 2024 的 1.2;20bp 往返成本下归零;部分样本内效应被证明是模型记忆(Gao et al. 2025)。

### 1.2 反复出现的失败模式(逐条防)
1. 参数化 look-ahead / 记忆:模型知识窗内回测全部污染,遮蔽 ticker/日期无效;样本内收益夸大 ~7×。
2. 玩具窗口和 universe:3–12 个月、3–6 只大盘股、牛市。
3. 无成本、无退市、无执行语义。
4. 牛市胆小、熊市鲁莽。
5. 推理模型 ≠ 交易能力;LMArena 排名与 P&L 相关 0.05。
6. 不确定性:相同输入不同交易;复杂输出解析失败。
7. 口头风控不是风控。
8. 星数与证据无关。

### 1.3 好框架里反复出现的设计 → 我们的对应
| 模式 | 出处 | 我们的实现 |
|---|---|---|
| LLM 形成观点,代码形成仓位 | ai-hedge-fund、FinRobot、Lean | 更进一步:观点也由代码信号产生;LLM 只做文本字段和叙述 |
| 硬性确定性风控在构建之后,clamp 留现金并记审计事件 | ai-hedge-fund `apply_limits`、FinRL turbulence | 复用 `apply_limits` + 新增 VIX/已实现波动率 regime 门 |
| 时点正确在数据层强制,快照哈希 + 缓存 | ai-hedge-fund、CLQT TimeGate | 面板表带 `as_of`/`source`;信号只读 ≤ as_of;LLM 字段带发布时间戳 |
| 弃权 ≠ 中性 | ai-hedge-fund | 沿用 `metadata["abstained"]`;数据不足即弃权 |
| 计划持久化对抗不确定性 | nof1 S1.5 | 每条纸面仓写入 7 日 breakeven(已有 `flat7/be7`),叙述者只能引用不能改 |
| 确定性工具当上下文,LLM 当读者 | FinAgent、FinRobot | 叙述者只收数字 + 验证状态,永远不收原始新闻 |
| 对已实现结果的闭环反思 | TradingAgents 决策日志、FinCon | 纸面账本 7/14 天结算 + 股票级 1/5/20 日收益 + 周六审计 |
| LLM 当研究员,回测器当裁判 | RD-Agent(Q)、Jesse MCP | 可选 P6:夜间密封 LLM 对候选信号提假设,由检验层打分;永不进实盘环路 |
| 验证协议 | FINSABER、KTD-Fin、2512.12924 | 截面 rank-IC + 置换 + FWER + 簇 bootstrap(复用 `masters/event_stats.py`)、安慰剂、基准对照、成本、预注册阈值 |

**我们与所有 LLM-agent 框架的根本区别**:决策路径上没有 LLM。这是 §7 实证 + FINSABER / KTD-Fin / FinRL-DeepSeek 三组独立证据共同指向的结论。LLM 唯一有证据的位置是"文本→数字"和"研究员"。

## 2. 目标架构

```
数据层   moomoo(实盘行情/期权链/持仓) · yfinance(验证日线,标明来源) · AV 免费档(新闻情绪一次调用覆盖 universe;国会) · SEC EDGAR Form 4(内部人,免费) · optradar chain_raw/chain_runs(每日 ATM±8 档归档)
   │
面板层   ~/.hedge-fund/agent/panel.db(DuckDB):bars(provenance)、index_daily(SPY/^VIX)、universe、news_sentiment、insider_tx、chain_daily、fundamentals_pit、earnings_events、signal_cache
   │
信号库   CrossSectionalModel(QuantModel)子类,按日截面批量计算 → Signal(value ∈ [−1,1], components)
   │      P0 动量/趋势/反转 · P1 新闻 · P2 期权/波动率 · P3 内部人+国会 · P4 PEAD · P5 价值/质量
   │
检验层   hedge_fund/validation/:rank-IC、置换 p + Westfall-Young FWER、簇/块 bootstrap、安慰剂、基准、衰减、换手、成本
   │      信号状态 candidate → validated → retired(registry.yaml);只有 validated 进合成
   │
合成层   blend_signals(IC 加权收缩)+ min_conviction 门 + regime 门(VIX/RV)+ apply_limits(硬 clamp,审计)
   │
输出层   agent_signals → optradar horizon.pick 选合约 → paper_ledger source='agent' → 7/14 天结算 → scoreboard;股票级 agent_returns → live IC
   │
叙述层   密封 LLM 读"数字 + 验证状态 + 记分" → 早报 ⑨ 节;不改任何数字
```

## 3. 环境事实(影响实现)
- 两个 venv 都有 moomoo-api + PyYAML,所以 optradar 的 `sources/moomoo_src.py`、`radar/horizon.py`、`radar/ideas.py`、`radar/ledger.py` 能从 3.13 venv 用 `sys.path` 直接 import(interop 机制,不走 subprocess)。
- `~/.hedgefund-venv` 缺 duckdb / yfinance;`masters/event_stats.py`、`build_event_db.py` 已经 import duckdb(现在本机就跑不了)。P-1 装 `duckdb==1.5.5`(与 `~/.moomoo/venv` 同版本,避免存储格式升级互锁)+ `yfinance`,记在 `hedge-fund/requirements-agent.txt`,不动上游 pyproject。
- DuckDB 跨进程:读写连接独占文件锁。agent 只在最后一步短暂开读写(≤2 s),不跨 moomoo/LLM 调用持锁;`_connect_with_retry(tries=6, wait=10s)`。面板库是独立文件,不与 optradar 争锁。
- moomoo 历史 K 线配额按账户资产定,不是固定 100;P-1 先调 `get_history_kl_quota` 看实际值。实盘 watchlist 走 moomoo,大 universe 历史走 yfinance。
- 本机 optradar 是干净 clone(无 db/out/.env/plist),launchd 接线要等迁移第 6–7 步完成后在生产机做。
- `hedge-fund/.gitignore` 忽略 `*.txt`,清单文件用 YAML/JSON。
- `site-data/events.db` 已有:universe 669 名(含市值、期权量,301 名满足 >$5B 且期权量 >5000)、earnings_events 6,354 行 / 2015-11→2026-09(eps_actual/est/surprise、filing_window、move_d0..d5)——P4 数据现成。
- AV NEWS_SENTIMENT 本账号历史约从 2026-03 起(2025 年窗口返回 0 条),字段:`time_published`、`ticker_sentiment[{ticker, relevance_score, ticker_sentiment_score}]`;`limit` 最大 1000;一次可传多个 ticker。
- AV INSIDER_TRANSACTIONS 按票调用,`share_price=0` 是授予/行权,必须过滤;因此内部人主源改 SEC EDGAR(季度批量数据集 + 每日 form index),需 `.env` 里 `SEC_USER_AGENT`。

## 4. 包布局

```
hedge-fund/
  requirements-agent.txt            duckdb==1.5.5, yfinance
  hedge_fund/
    paths.py                        + AGENT_DIR, PANEL_DB
    features/panel.py               PanelStore(DDL/读写) + PanelDataClient(DataClient over panel.db,给 backtest_fund/run_cycle 离线用)
    features/rv.py                  yang_zhang(), close_to_close()
    signals/xs.py                   CrossSectionalModel(QuantModel) 基类 + PanelSource 协议
    signals/momentum.py             mom_12_1, trend_20_60, reversal_5, optradar_rule(基准)
    signals/news_sentiment.py       news_1d, news_5d, news_5d_surprise
    signals/vol_surface.py          iv_rv, iv_rank, pcr_vol, pcr_oi, oi_chg_5, skew_25d
    signals/insider.py              insider_cluster, congress_net
    signals/pead.py                 + pead_sue, ear(现有 PEADModel 不动)
    signals/value_quality.py        value_quality
    signals/__init__.py             全部注册进 ALPHA_MODEL_REGISTRY
    portfolio/construction.py       + min_conviction
    portfolio/regime.py             regime_scale() + RegimeGate
    fund/spec.py                    BlendPolicy.min_conviction, FundSpec.regime
    pipeline/run_cycle.py           净额后、apply_limits 前乘 regime scale;CycleRecord.regime_state/scale
    validation/{models,ic,stats,placebo,baselines,registry,report,__main__}.py + registry.yaml + family_log.jsonl
  agent/
    config.yaml, paths.py, universe.py, backfill.py
    sources/{av_news,sec_form4,moomoo_chain,moomoo_fundamentals,events_import}.py
    optradar_bridge.py              sys.path 接 optradar;读 config(尊重 OPTRADAR_CONFIG)和 out/<date>.json
    daily.py                        `python -m agent.daily --date D [--dry-run] [--no-llm]`
    ledger.py                       agent_* DDL、open_agent_positions()、fill_returns()、live_ic()
    brief.py                        ⑨ HTML 块 + 一行摘要(仿 bin/masters_line.py)
    narrator.py, extractor.py       密封 LLM 合约(版本化)
    backtest.py, mandate.yaml       mandate 由 registry 生成(权重 = f(IC)),入库
    tests/                          单测 + selftest_agent.sh
  site-data/validation/<run_id>.json  验证报告(入库,无仓位)
optradar/(3 处小改)
  radar/ledger.py                   SOURCES = ("budget","atm","agent");scoreboard 循环 SOURCES
  radar/report.py                   source 标签表加 "agent"
  bin/run_and_notify.sh             agent 块在 run_daily 之后、masters 之前
```

## 5. 数据面板(`hedge_fund/features/panel.py`,`~/.hedge-fund/agent/panel.db`)

表:`bars(ticker, trade_date, open, high, low, close, adj_close, volume, source, adj_basis, fetched_at; PK ticker,trade_date,source)`、`index_daily(SPY,^VIX)`、`universe(ticker, as_of, market_cap, adv20_usd, option_volume, n_bars, in_universe, reason)`、`reconcile_log`、`news_sentiment(article_id, time_published, ticker, relevance, sentiment, overall_sentiment, source_domain, fetched_at; PK article_id,ticker)`、`news_fetch_log`、`insider_tx(accession, ticker, cik, filing_date, trans_date, owner, is_officer, is_director, is_tenpct, title, trans_code, acq_disp, shares, price, value_usd, source)`、`chain_daily`(= optradar chain_raw 列 + spot, dte, snap_ts, source)、`fundamentals_pit(ticker, report_period, period_end, filing_date, revenue, gross_profit, operating_income, net_income, equity, total_liabilities, fcf, roe, roa, current_ratio, shares_now, source)`、`earnings_events`(从 events.db 导入)、`signal_cache(signal_name, signal_version, as_of, ticker, value, components JSON)`。

- 回填 `python -m agent.backfill bars --start 2014-01-01`:yfinance 每批 50 只,`auto_adjust=False`,退避重试;`--incremental` 每个交易日早晨重拉最近 10 天;`--full` 每周日全量(yfinance 拆股/分红会改写历史)。SPY、^VIX 进 `index_daily`。
- `reconcile --date D`:watchlist 上 yfinance close(拆股调整)vs optradar `underlying_daily.close`(moomoo QFQ),|diff| < 0.5% 除除息日附近;写 `reconcile_log`,早报显示 >1% 的条数。
- 口径:收益和价格类信号用 `adj_close`(全收益);已实现波动率用拆股调整 OHLC;实盘 spot 只来自 moomoo 快照,永不进信号计算。信号 as-of = 上一完整交易日,次日 08:45 执行 → 验证用 1 日实施滞后。
- universe(`agent/universe.py`):种子 = events.db.universe ∪ optradar watchlist;过滤市值 ≥ $5B、期权量 ≥ 2000、价格 ≥ $5、ADV20 ≥ $50M、≥ 500 根 bar、剔除 ETF;冻结到 `agent/universe_2026-09.yaml`(含构建日期和标准),验证运行记 `universe_hash`。幸存者偏差不可避免,每份报告注明;动量基准同 universe 所以对比公平。

## 6. 信号 API 与公式(`hedge_fund/signals/xs.py`)

```python
class CrossSectionalModel(QuantModel):
    name: str; version: str = "1"; lookback_days: int; min_names: int = 30; sign: int = +1
    def raw(self, as_of, tickers) -> pd.DataFrame   # index=ticker, 列 'raw'(NaN=弃权)+ 组件列
    def compute(self, as_of) -> pd.DataFrame        # raw → 1/99% winsor → 稳健 z=(x−median)/(1.4826·MAD) → value=clip(z/3,−1,1);缓存到 signal_cache
    def predict(self, ticker, date, data_client) -> Signal   # 查 compute(last_bar_on_or_before(date));data_client 不用
```
- `compute()` 永远跑全 universe,`predict()` 只是查表,z 分数与 run_cycle 传入的子集无关。`Signal.components` 恒含 `raw, z, pct_rank, n_names`(全部数值,bool 转 0/1);`metadata` 含 `signal_version, as_of_bar, panel_source`,raw 为 NaN / 不在 universe / `n_names < min_names` 时 `abstained=True`。
- 记号:`A[t]` 调整收盘,t=0 为 as-of bar;`σ_n` = n 根日对数收益样本标准差。

| 阶段 | 信号 | raw 公式 | 预注册符号 | 备注 |
|---|---|---|---|---|
| P0 | `mom_12_1` | A[−21]/A[−252] − 1 | + | 需 ≥230 bar;组件 ret_1m, sigma_252 |
| P0 | `trend_20_60` | ln(SMA20/SMA60) | + | 组件 dist20, dist60, slope20 |
| P0 | `reversal_5` | −(A[0]/A[−5] − 1) | + | 上周输家跑赢 |
| P0 | `optradar_rule`(基准,不合成) | close>SMA20 且 5 日>1% → +1;反之 −1;否则弃权 | — | `radar/ideas.py:44-51` 原样,量化"方向已证伪" |
| P1 | `news_1d/5d` | Σ r_j s_j / Σ r_j,窗口 (16:00 ET 第 −W 日, 16:00 ET 第 0 日],r_j ≥ 0.1 | + | 1d 需 ≥2 篇,5d ≥3 篇 |
| P1 | `news_5d_surprise`(主) | news_5d − 该票前 60 日 news_1d 均值 | + | 去掉"永远正面报道"的水平 |
| P2 | `iv_rv` | ln(iv_atm / RV_YZ20) | − | iv_atm = ATM 看涨/看跌 IV 均值(bid>0) |
| P2 | `iv_rank` | (iv_atm − min₂₅₂)/(max₂₅₂ − min₂₅₂) | − | 需 ≥120 个日观测 |
| P2 | `pcr_vol` / `pcr_oi` | ln((Σput+1)/(Σcall+1)) | − | 只有总量,预期弱 |
| P2 | `oi_chg_5` | ln(ΣOI[0]/ΣOI[−5]),同一到期才算 | (双侧) | 跨 roll 为 NaN |
| P2 | `skew_25d` | (iv@δ≈−0.25 − iv@δ≈+0.25)/iv_atm | − | |
| P3 | `insider_cluster` | 1[n_buyers≥2]·ln(1+buy_value/ADV20) − 0.25·ln(1+sell_value/ADV20),30 天窗,按 `filing_date ≤ D` | + | 只算公开市场买入 trans_code='P', price>0;稀疏轨道 |
| P3 | `congress_net` | ln(1+Σbuy amount_min) − ln(1+Σsell amount_min),60 天,`filed_date ≤ D` | + | 覆盖只有 watchlist,先 background |
| P4 | `pead_sue` | SUE=(eps_actual−eps_est)/A[E−1],最近事件 1≤D−E≤60 交易日 | + | E 按 filing_window 做会话对齐(PRE→当日,否则次日);变体 `pead_sue_decay` 单独注册 |
| P4 | `ear` | 对齐后的公告日收益,同 60 日窗 | + | |
| P5 | `value_quality` | 0.5·z(z(EY)+z(B/P)) + 0.5·z(z(ROE)+z(GM)−z(D/E)),用 `filing_date ≤ D` 的最新一行 | + | 股数用当前值(moomoo 无 PIT 股数),报告注明 |

Yang–Zhang(`features/rv.py`,n=20,拆股调整 OHLC):o=ln(O/C₋₁), c=ln(C/O), u=ln(H/O), d=ln(L/O);σ²_YZ = var(o) + k·var(c) + (1−k)·mean(u(u−c)+d(d−c)),k = 0.34/(1.34+(n+1)/(n−1));RV% = 100·√(252σ²)。`rv_cc20` 与 optradar `_hv20` 一致。

数据源说明:
- P1:AV 一次调用 `tickers=` 逗号列表 + `time_from/to` + `limit=1000`。回填按 10 组×~25 票×周,2026-03→今 ≈ 280 次 ≈ 13 个工作日(每天 22 次);`~/.hedge-fund/agent/av_quota.json` 记每日用量,周六额度留给 `bin/congress.py`。实盘每天 3 次(3 组 ~85 票)。回填分数不是 PIT(AV 今天算的),真正 PIT 记录从实盘爬取起,报告注明。
- P2:optradar `chain_raw` 只有 watchlist 4 天历史;新增 `agent/sources/moomoo_chain.py` 夜间为 universe 归档 ATM±8(单独 launchd,收盘后,限速调优)。`iv_rv/pcr_*` 要到 ~2027-09 才够 12 个月,`iv_rank` 再晚一年。代码和测试现在写,注册表 `data_ready_after` 标注。P2 启动时探一次 AV `HISTORICAL_PUT_CALL_RATIO` / `HISTORICAL_VOLUME_OPEN_INTEREST_RATIO` 是否免费——是的话 pcr 不用等。
- P3:SEC EDGAR 季度批量 insider 数据集(SUBMISSION / NONDERIV_TRANS / REPORTINGOWNER,2006→)+ 每日 `form.YYYYMMDD.idx` 过滤 universe 的 CIK 解析 Form 4 XML;AV INSIDER_TRANSACTIONS 只做 ≤3 票/天抽查。
- P5:每周日 moomoo 财务报表爬取(`MoomooDataClient._statement/_filing_dates`),5 端点×250 名 ≈ 25 分钟。

## 7. 检验层(`hedge_fund/validation/`)

函数:`forward_returns(bars, horizons=(1,5,20), lag=1)`;`ic_series(signal_panel, fwd, horizon, method="spearman", min_names=30)`;`ic_summary(ics, horizon, n_boot=4000, seed=7)` → mean, std, ICIR, Newey-West t(lag=horizon), 块 bootstrap CI95(块=horizon+1), hit_rate, n_dates, 按年、前后半段;`decay_curve`、`turnover`、`quantile_spread(q=5, cost_bps=10)`、`partial_ic(signal, control=mom_12_1)`;`stats.py`:`perm_gate_tests`/`cluster_boot` 从 `masters/event_stats.py` 原样迁入(masters 改 import),新增 `perm_ic_test(panels, fwd, horizons, n_perm=2000)`(日内打乱 ticker,family = 本次运行所有信号×期限,Westfall-Young max-|T| + Holm)、`newey_west_t`、`block_bootstrap`;`placebo.py`:`shifted(k=60)`、`date_shuffled`、`lookahead`(用未来 bar 算,必须明显膨胀——检验工具本身的功效)、`planted(ic_target=0.05)`(必须 PASS);`baselines.py`:`momentum_baseline`、`optradar_rule_baseline`、`always_majority`。

CLI:`python -m hedge_fund.validation run --signal X --start --end [--horizons 1,5,20] [--universe agent/universe_2026-09.yaml] [--perms 2000] [--family a,b,c]` → `site-data/validation/<run_id>.json` + markdown,每个 (signal, horizon) 追加 `family_log.jsonl`;`report`、`promote`(FAIL 拒绝)、`retire --reason`、`live-ic --since`、`family --all`。

**预注册通过规则**(`registry.yaml` `thresholds_version: 1`,改动必须升版本并注明日期;主期限 h=5,lag=1):
1. ≥250 个交易日、跨 ≥12 个月、每日名称中位数 ≥100。
2. 平均 rank-IC ≥ 0.02 且符号与预注册一致。
3. ICIR ≥ 0.30(日频),NW t ≥ 2.5,块 bootstrap 95% CI 不含 0。
4. Westfall-Young p_fwer ≤ 0.05(family = 本次运行所有信号×期限)。
5. 安慰剂:`shifted`、`date_shuffled` 的 |mean IC| ≤ 0.01 且不显著;`lookahead` IC ≥ 真实 IC 的 2 倍。
6. 超过动量:IC − IC_mom ≥ 0.005(同日期),或去掉 `mom_12_1` 后 `partial_ic` ≥ 0.015。
7. 符号一致:前后半段均 >0,且 ≥60% 的年份 >0。
8. 成本:日 top 五分位换手 ≤ 0.35,或 h=20 五分位价差扣 10bp/边后 >0。
9. 跨时间多重检验:`family_log.jsonl` 近 24 个月全部条目 Holm 校正 p ≤ 0.10;每季度最多新注册 6 个假设。

稀疏轨道(insider/congress/低覆盖 PEAD):同规则但改为 ≥200 个事件、只在 ≥20 个非弃权名称的日期算 IC,另加 CAR[+1,+20] 事件研究(复用 `event_study/stats.py` + `cluster_boot` 按日期)。

状态 `candidate → validated → retired`,validated 带 `live_confirmed`。退役规则(预注册):实盘滚动 3 个月 rank-IC < 0 且 bootstrap p < 0.10,或 6 个月 IC < 0.5 × 验证 IC。经 LLM 提取的信号行带 `resolved_model`,注册表记 `llm_cutoff`,`run` 只用 `time_published > llm_cutoff` 且跨度 <12 个月拒绝 promote。

`registry.yaml` 条目示例:
```yaml
signals:
  mom_12_1: {module: "hedge_fund.signals.momentum:MomentumModel", version: "1", sign: +1, status: candidate,
             registered: 2026-09-18, hypothesis: "12-1 winners continue", runs: [], validated_on: null,
             ic_validated: null, icir_validated: null, weight: null, live_confirmed: false, llm_cutoff: null}
```

## 8. 合成 + regime 门

- 权重(`registry.weights_from_registry()`,只用 validated):w_s = 0.5·ICIR_s/ΣICIR + 0.5/N,归一,动量家族(`mom_12_1`+`trend_20_60`)合计 ≤ 50%;`write_strategy_yaml("agent/mandate.yaml")` 写成 `ModelSpec.weight`,mandate 是数据,`backtest_fund` 能原样重放。
- `BlendPolicy.min_conviction`(默认 0,agent 预注册 0.2 ≈ |z| ≥ 0.6):去均值后、毛敞口归一前把 |scaled| < 阈值 置 0。
- `portfolio/regime.py`:输入 ^VIX 收盘 + SPY YZ20 RV(`index_daily`);`calm`(VIX<20 且 RV<20)→1.0;`elevated`(任一 ∈[20,30) 或 VIX 5 日涨 >20%)→0.5;`stress`(任一 ≥30)→0.0。数字预注册不拟合。`FundSpec.regime: RegimeGate | None`;`run_cycle` 在净额后、`apply_limits` 前乘 scale,`CycleRecord` 记 `regime_state/regime_scale`;scale=0 当天不开新仓,账本继续 mark。
- `agent/mandate.yaml`(生成):`strategies: [{name: validated-xs, models: [...], blend: {conviction_weighted, gross_target 1.0, market_neutral true, min_conviction 0.2}}]`,`risk: {max_position_pct 0.10, max_gross_exposure 1.0}`,`regime: {...}`,`rebalance: daily`,`benchmark: SPY`。`agent/backtest.py` 用 `PanelDataClient` 调 `backtest_fund`(`hedge_fund/run.py` 写死 FDClient,不用它)。

## 9. 账本接线(`agent/ledger.py`,DDL 写进 optradar.db,仿 `bin/congress.py`)

```sql
agent_runs(run_id PK, as_of, run_ts, git_sha, registry_version, thresholds_version, universe_n, n_validated, regime_state, regime_scale, vix, rv_spy, top_k, n_picks, n_opened, status, error)
agent_signals(as_of, ticker, signal_name, signal_version, value, components JSON, abstained, validated, weight, composite, rank, run_id; PK as_of,ticker,signal_name)   -- 每票另加一行 signal_name='__composite__'
agent_returns(as_of, ticker, close0, ret1, ret5, ret20, ret1_lag1, ret5_lag1, ret20_lag1, spy_ret5, spy_ret20, source, filled_at; PK as_of,ticker)
agent_picks(as_of, ticker, rank, composite, direction, ledger_id, contract, status, reason; PK as_of,ticker)
```
- `open_agent_positions(con, snap_date, picks, src, cfg)`:top-K(K=5)按 |composite|,过 floor,`regime_scale > 0` 且市场 live(同 `run_daily.py:155` 判断);`direction = '看涨'/'看跌'`(`dir_hit7` 比较的就是这两个字符串),`right='C'/'P'`;`src.snapshot` 取 spot → `src.pick_expiry(t,25,50)` → `src.atm_quotes(width=8)` → `radar.ideas._liq` 流动性门 → `radar.horizon.pick(strip, right, spot, dte, cfg["buyer"])`;插入行形状同 `ledger._row_from_pick`,`id=f"{snap_date}|{code}|agent"`,`source='agent'`,已存在跳过;strip 顺手归档到 `chain_daily`。`mark()` 不分 source,次日自动结算;`scoreboard()` 改循环 `SOURCES`,`report.py` 标签加 `agent`,`weekly_pack.py` 自动带上。
- `fill_returns(con, panel)` 每日补 1/5/20 日 `adj_close` 收益;`live_ic(con)` = 按日 Spearman(composite, ret5_lag1) → 实盘记分 + 退役检查。
- `bin/run_and_notify.sh` 在 STATUS/SUMMARY 之后、masters 之前插入:
```zsh
AGENT_LINE=""
if [ "$STATUS" = "OK" ] && [ -z "$SKIP_AI" ]; then
  echo "--- agent start $(date) ---" >> "$LOG"
  PYTHONPATH=/Users/louis/hedge-fund "$HOME/.hedgefund-venv/bin/python" -m agent.daily --date "$TODAY" >> "$LOG" 2>&1 || echo "agent failed (non-fatal)" >> "$LOG"
  AGENT_LINE=$(PYTHONPATH=/Users/louis/hedge-fund "$HOME/.hedgefund-venv/bin/python" -m agent.brief "$TODAY" 2>/dev/null)
  PYTHONPATH=/Users/louis/hedge-fund "$HOME/.hedgefund-venv/bin/python" -m agent.brief "$TODAY" --append-html "out/$TODAY.html" >> "$LOG" 2>&1
  [ -n "$AGENT_LINE" ] && SUMMARY="$SUMMARY | $AGENT_LINE"
fi
```
- `agent.daily` 步骤(自带 8 分钟 SIGALRM,输出 `out/agent/<date>.json` + `status.txt`):① 增量 yfinance + SPY/VIX;② 读 `out/<date>.json` 和只读 `chain_raw`/`congress_trades`;③ 计算所有注册信号(候选也算,记 `validated=false`——影子记录,供日后实盘验证);④ 合成 validated + floor + regime;⑤ moomoo 取 top-K 合约;⑥ 叙述者(缓存);⑦ 一次短读写事务写 `agent_runs/agent_signals/agent_returns/agent_picks/paper_ledger`。预计 2–3 分钟。
- 早报 ⑨(`agent/brief.py --append-html`,同 masters 的 `<footer>` 拼接):regime 状态/scale + VIX/RV;validated 信号表(名称、验证 IC/ICIR、实盘 60 日 IC、n);picks 表(票、composite、rank、方向、贡献最大的信号、合约、成本、7 日门槛/1σ、是否开仓+原因);`source='agent'` 记分行 + 股票级 live rank-IC;叙述文字;固定脚注"候选信号 N 个未验证,不参与打分"。不含真实持仓,不上公开站(公开站以后最多展示注册表状态 + IC)。

## 10. 叙述者 / 提取器合约

- 叙述者(`agent/narrator.py`,`NARRATOR_VERSION=1`):输入只有数字 JSON(regime、validated 表、picks、scoreboard、live_ic);系统提示:只能复述输入里的数字、不预测、不引用新闻和外部知识、中文 2–3 句;输出严格 `{"headline":"≤60字","commentary":"2-3句","caveat":"1句"}`;校验:`extract_json` + 输出里每个数字 token 必须出现在输入中,否则回落到确定性模板句并记日志;采样 1 次;缓存键 `ClaudeCodeLLM.cache_key("agent_narrator", SYSTEM_v1, json)`。
- 提取器(`agent/extractor.py`,`EXTRACTOR_VERSION=1`,**延后**):输入 `{ticker, title, summary}`(不给 URL/日期/AV 分数);输出 `{event_type ∈ [guidance_raise, guidance_cut, mna_target, mna_acquirer, buyback, equity_offering, exec_change, legal_regulatory, product, macro, none], polarity −1|0|1, company_specific, confidence 0-3}`;存 `panel.news_events` 带 `resolved_model`;聚合成 `news_events_5d`;只在 `llm_cutoff` 之后的日期验证。

## 11. 测试
- `signals/test_xs.py`:合成面板 `make_panel(120 票, 600 日)`;标准化、弃权、子集不变性(5 票 predict = 全量 compute 的对应行)、时点(改 as_of 之后的 bar 输出逐字节不变)、缓存往返。
- 每个信号一个测试文件:动量植入漂移→排序正确、`optradar_rule` 复现 `ideas.py`;YZ 在常方差 GBM 上误差 <10%;期权链 3 档 fixture 的 pcr/skew/ATM、`oi_chg_5` 跨 roll 为 NaN;内部人 2 个买家→cluster、价格 0/代码 A 排除、按 filing_date 而非 trans_date;PEAD PRE/AFTER 对齐、60 日窗;价值/质量 `filing_date > D` 排除。
- `validation/test_ic.py`:`planted(0.05)` 300 日×150 票必须全部 PASS;纯噪声 20 个种子里 ≥95% FAIL;`lookahead` 被标出;NW t 与手算 AR(1) 一致;bootstrap CI 覆盖真值;置换 p 在零假设下近似均匀;p_fwer ≥ p_raw 恒成立。`test_registry.py`:promote 拒绝 FAIL、权重和为 1、家族上限生效、YAML 过 `load_spec`。
- `portfolio/test_construction.py` 加 `min_conviction`;`test_regime.py` 三态 + 尖峰规则;`pipeline/test_run_cycle.py` 加 regime 周期。
- `agent/tests/test_ledger.py`(临时 DuckDB):DDL 幂等、`open_agent_positions` 插 `source='agent'`、id 唯一、方向字符串、`fill_returns`、`scoreboard` 出 agent 行(optradar 路径不存在则 skip)。
- `agent/tests/test_narrator.py`:用 `integrations/tests/test_masters_llm.py` 的 `FakeTransport`;合法 JSON 通过、捏造数字触发模板回落、第二次调用命中缓存。
- `agent/tests/selftest_agent.sh`:仿 `bin/selftest.sh`,临时目录 + sed 过的 config,`OPTRADAR_CONFIG=$T/config.yaml run_daily.py --force` 后跑 `agent.daily --optradar-config $T/config.yaml --panel-db $T/panel.db --dry-run --no-llm`;断言 `out/agent/<date>.json` 键、`$T/test.db` 里有 `agent_signals`、市场 live 时有 `source='agent'` 的 paper_ledger 行、HTML 含 "⑨"、真实 `optradar.db`/`out/` 未动。

## 12. 分阶段与停/走标准(每个 session ≈ 2–3 小时)

| 阶段 | 交付 | 停/走 | 工作量 |
|---|---|---|---|
| P-1 基础 | 装依赖;`paths`/`PanelStore`/`PanelDataClient`/DDL;universe 构建 + 冻结 YAML;yfinance 回填 2014→ + SPY/VIX + reconcile;`CrossSectionalModel`;检验层核心(ic/stats 迁移/placebo/baselines/registry/CLI);events.db 导入;查 moomoo kline 配额 | ~250 名回填缺失 <2%;planted PASS / noise FAIL 测试绿;`masters/event_stats.py` 迁移后仍能跑 | 3 |
| P0 动量 | 3 信号 + `optradar_rule`;2016→2026-08 一个 family 跑验证 | ≥1 个 PASS 则进合成;全 FAIL 也是有效结果,agent 以影子模式上线(只记录不开仓);`optradar_rule` 预期 FAIL,数字写进早报 | 1 |
| 合成 + 账本 + 早报 | `min_conviction`、`RegimeGate`、`run_cycle` 改动、`weights_from_registry` → `mandate.yaml`、`agent/backtest.py`;`agent/daily.py`、`agent/ledger.py`、optradar 3 处小改、`agent_returns` 填充、live IC、⑨ HTML、selftest | mandate 回放在验证窗内复现 composite 的 IC;selftest 隔离跑绿;实盘第一周每天有 `agent_signals` 且 agent 行被 mark | 3 |
| P4 PEAD | `pead_sue`、`ear`;2015→ 验证 | 数据现成,纯看通过规则 | 1 |
| P3 内部人 + 国会 | EDGAR 批量加载 + 每日索引更新;`insider_cluster` 稀疏轨道立即验证(2006→);国会覆盖扩展计划 | cluster 过事件轨道则进;congress 覆盖到 universe 前留 background | 2 |
| P5 价值/质量 | 周日财务爬取;信号;验证受 moomoo 报表深度限制 | PASS 则进;股数近似已注明 | 2 |
| P1 新闻 | 爬虫 + 配额账本;回填 2026-03→(~13 个工作日额度);3 信号;测试 | 数据门:≥12 个月 → ~2027-03 前无结论;只发标注的 6 个月预览 | 2 |
| P2 期权/波动率 | 夜间 `moomoo_chain` 归档 launchd;`rv.py`;6 信号;测试;探 AV pcr 端点是否免费 | 数据门 ≈ 2027-09;归档 2 周后缺日率 <5% 才继续 | 2 |
| 叙述者(+提取器) | 合约、校验、缓存、测试 | 20 次缓存运行叙述者从不输出输入之外的数字 | 1(+1) |

合计 ≈ 15–17 个 session。**上线时机**:合成+账本+早报阶段完成即可在生产机接入 launchd(影子/实盘由 validated 信号数量决定)——但要等迁移第 6–7 步(旧机器数据、plist)完成。

## 13. 风险与已知限制
- 诚实结果可能是"没有 validated 信号":大盘 universe、1 日滞后、严格 FWER 可能让合成器为空。影子模式是一等公民,早报如实写。
- universe 幸存者偏差:免费数据无法修,报告注明。
- yfinance 非官方、会限流、拆股改写历史:分批 + 退避 + 周日全量 + moomoo 对账;moomoo kline 留作 watchlist 应急回退。
- AV 配额与 `bin/congress.py` 共享 25/天:配额账本按文件记,congress.py 可选读它。
- AV 新闻回填非 PIT;SEC 季度数据集有滞后,近 3 个月必须走每日索引;需要 `SEC_USER_AGENT`。
- moomoo 250 名夜间期权链归档限速(约 15–25 分钟),OpenD 盘后要开着;盘后 bid/ask 陈旧,IV/OI 可用。
- 期权纸面账本把方向和 vol/theta 混在一起;股票级 `agent_returns` 是更干净的记分,两者都展示。
- DuckDB 锁冲突(重试)和 venv 版本漂移(钉 1.5.5)。
- regime 阈值预注册不拟合,某些年份会显得不对。
- `Signal.components` 是 `dict[str,float]`,组件必须全数值。

## 14. 验证方法(端到端)
1. 单测:`~/.hedgefund-venv/bin/python -m pytest -q hedge_fund integrations agent`(现有 415 个 + 新增;上游自带的 5 个 Jev 显示名失败不算)。
2. 检验层自检:`python -m hedge_fund.validation run --signal __planted__`(PASS)与 `--signal __noise__`(FAIL)。
3. P0 验证运行产出 `site-data/validation/<run_id>.json`,人工核对每条通过规则的数字。
4. `agent/tests/selftest_agent.sh` 隔离全链路跑绿(需 OpenD 登录)。
5. `agent/backtest.py` 重放 mandate,composite IC 与验证报告一致。
6. 生产机接入后:第一周每天 `out/agent/<date>.json` 生成、`launchctl list` 无重复触发、`agent_signals` 行数 = universe 大小、第 8 天起 `paper_ledger` agent 行有 `ret7_*`。
