# Hedge Fund 驾驶舱 — 总体规划 (2026-09-13)

目标:把 hedge-fund.louisleng.com 从「v1 演示站」升级为**私人投资驾驶舱**,
把手头所有资源焊成一条链:moomoo 实时数据 → OptRadar 雷达 → aihf v2 大师引擎 → 网站展示。
网站同时解决悬置需求:早报的 routine 级富展示层(手机浏览器直接看,历史可回翻)。

## 0. 资源盘点(全部已验证可用)

| 资源 | 状态 | 在链条中的角色 |
|---|---|---|
| moomoo OpenD (本机:11111) | ✅ 登录态,LV1 期权全字段实测 | 唯一行情/基本面/持仓数据源,零 API 费 |
| OptRadar (~/optradar) | ✅ 每日 8:41 launchd 已运行 | 期权雷达 + 持仓哨兵 + DuckDB 期权链历史积累 |
| ClaudeCodeLLM (~/optradar/masters/) | ✅ 实测 8s/次 | aihf 的 LLM 层走 Claude Code 订阅,$0 边际成本 |
| aihf v2 引擎 (本仓库 v2-rebuild 分支) | ✅ = upstream 2.2.0 | 大师 agents / PEAD / 回测 / FundSpec |
| hedge-fund.louisleng.com | ✅ 在线(v1 + Cloudflare tunnel) | 升级目标;老站保持在线直到新站就绪 |
| Claude Code 订阅 / gh CLI | ✅ | 每晚大师信号计费来源 / 部署通道 |

## 1. 架构:static-first,每日生成、推送即部署

```
8:41 launchd(现有 optradar 管线,追加两步)
  ├─ ① 雷达+哨兵 → out/<date>.json          (已有)
  ├─ ② 大师信号:5 personas × (持仓+Top5)     (新,ClaudeCodeLLM,~10min)
  ├─ ③ site-data/ 汇总 JSON → 静态站 build    (新)
  └─ ④ git push → Cloudflare Pages 自动部署   (新;Mac 无需对外开服务)
```

- 前端:轻量静态站(Astro 或 Next.js static export),读 JSON 渲染。不复用 v1 React 组件(v1 绑死旧后端,重写三个页面更快)。
- 页面:**今日雷达**(哨兵/异动/机会+AI点评) · **大师信号**(5 persona 每票的 conviction+论点,历史时间线) · **我的组合**(持仓+盈亏+警报) · **回测实验室**(P4)
- 部署:**两层架构**。
  - 公开层(Netlify 不动):大师信号、市场异动、回测 showcase——观点与市场事实,无仓位无金额,挂免责声明。
  - 私有层(Tailscale,**现网已存在**):tailnet 已运行,Windows 常开机 `lx-pc`(100.90.46.88)在线。Mac 8:41 生成 site-data 后经 tailnet 推给 lx-pc,由 lx-pc 24/7 serve(`tailscale serve` 或小静态服务);手机(iphone-14-pro-max,已在 tailnet)随时可看,Mac 睡觉无影响。敏感数据不出自家设备。
  - CF Access 方案作废(Tailscale 对私有数据是降维打击:网络不可达 > 门禁拦截)。

## 2. 安全红线(先于一切上线动作)

1. **敏感分层铁律**:含仓位/金额/具体下单结构的内容只进私有层(Tailscale,不出本机);公开层(Netlify)只放观点与市场事实。
2. 工具链全程只读 moomoo:**永不下单**,交易解锁只在 OpenD 手动。
3. 回测**不走订阅**:API+Haiku(几美元级)或仅 PEAD 量化 pod(零 LLM)。

## 3. 阶段拆解

| 阶段 | 交付 | 依赖 |
|---|---|---|
| **P1 MoomooDataClient** | aihf `DataClient` 协议 6 方法 → moomoo 实现 + 契约测试跑通;Buffett agent 用真实 RKLB 快照出信号 | 无 |
| **P2 大师信号管线** | masters/run_masters.py:5 agents × (持仓+Top5) → site-data/masters/<date>.json;接进 8:41 wrapper;iMessage 摘要加一行"大师分歧" | P1 |
| **P3 网站 v2** | 三页面静态站部署到现有 Netlify + **CF Access 门禁 + JWT 边缘校验** + 老站转 legacy 路径 | P2 |
| **P4 回测实验室** | FastAPI(tunnel 按需启)+ 页面触发回测(Haiku/PEAD)+ 结果归档进站 | P3 |
| **P5 财报预测 schema(新,优先级提到 P4 之后)** | 建 `EarningsSnapshot`:把已有但未使用的期权维度喂进 LLM —— 财报日历的 `iv_rank`/`iv_percentile`/`option_volume`、财报历史的 `option_iv_crush`、`earnings_price_move` 的财报前后股价序列、OptRadar 累积的期权链快照。再用**历史财报回放**给 schema 打分(过去 N 次财报的方向准确率),形成校准闭环 | P4(回测框架已可复用) |
| **P6 期权回测(远期)** | OptRadar DuckDB 攒 3+ 个月期权链后,策略级期权回测(卖 strangle/价差胜率) | 数据积累 |

## 4. Fork 治理

- `main`:冻结 = 线上老站,不动。
- `v2-rebuild`:开发主线(= upstream 2.2.0 起步),我们的代码放 `integrations/`(MoomooDataClient、ClaudeCodeLLM)+ `site/`(前端),便于持续 rebase 上游。
- 你的 18 个历史提交永久保存在 main;新站上线后 main 归档为 `legacy-v1` 分支,v2-rebuild 转正。

## 5. 開支模型

| 项 | 通道 | 成本 |
|---|---|---|
| 每晚大师信号 (~40 调用) | Claude Code 订阅 | $0(耗订阅额度,约 10 分钟) |
| 回测 | API Haiku / PEAD-only | ~$2-5/次 / $0 |
| 行情+基本面 | moomoo OpenD | $0 |
| 托管 | Cloudflare Pages + Access | $0 |

## 6. 方法论参考(2026-09-13)

来源:@Balder13946731 关于 LLM 财报预测的公开说明。核心论点:
**「没有结构性的输入就不会有结构性的输出」** —— 直接问 LLM「这个财报怎么看」无效,
需要 schema 作为种子(含期权、订单流、历史),用同一 schema 重建历史输入,
再以历史校准出预测规律,并监控提示词是否「强行记住不合理的记忆」以约束过拟合。

**对本项目的诊断**:2026-09-13 的回测显示 BuffettAgent 对 RKLB 连续 67 周给出同一看空
判断、跑输基准 59.75 个百分点。根因不是模型能力,而是 **schema 与标的错配** ——
喂的是通用价值投资 schema(ROE/负债/安全边际),而 RKLB 是期权驱动的高成长标的。
P5 即针对此:补期权维度 + 建历史评分闭环。缺口清单见上表(元学习与过拟合监控尚未实现)。

## 7. 实证结论(2026-09-13,负面结果如实记录)

三个实验,同一个主题:**LLM 在这些任务上没有提供 alpha,有价值的是结构化的数据分析。**

### 7.1 P5 财报预测 schema — 无预测力

31 次历史回放(8 票 × 4 期),20 次给出方向判断:

| | 模型命中 | 正确基准 | 差距 |
|---|---:|---:|---|
| 当日方向 | 45% | 50%(实际涨跌各 10) | −5pp |
| +5日方向 | 55% | **70%**(实际 14 跌/6 涨) | **−15pp** |

幅度同样失败:+5日预测均值 3.2% vs 实际 11.1%,相关系数 **−0.14**,68% 的样本低估幅度。
**注意基准的选取**:用 50% 当随机基准会高估表现 —— 模型偏空(13 bearish / 7 bullish)
而样本期本就偏跌,正确的对照是「永远猜多数方向」。

结论:此路当前不通,不应继续加功能。

### 7.2 委员会 vs 单飞 — 完全无差别

RKLB / 67 周 / 同窗口:5 persona 委员会与 Buffett 单飞**净值逐点完全相同**
(−30.39%,回撤 42.15%,夏普 −1.10),耗时 71 分钟 vs 17 分钟。

原因:5 位意见高度一致(−0.7 ~ −0.9),净头寸每期都打满 −1.0,风控一律 clamp 到 −0.25。
**更深的一点:persona 之间的错误是高度相关的** —— 都是价值派框架,都嫌 RKLB 不盈利。
分散只能消除独立误差,消不掉共同的框架偏见。增加 persona = 成倍成本买同一个结果。

### 7.3 真正有 edge 的地方 — 隐含 vs 实际波动(纯统计,零 LLM)

财报前 IV 推出的 5 日隐含波动 vs 实际 |+5日| 波动,80 个样本:

| 标的 | 隐含 | 实际 | 实际>隐含 | 含义 |
|---|---:|---:|---:|---|
| RGTI | 21.3% | 11.9% | 1/10 | 期权极度高估波动,卖方优势最大 |
| TSM | 6.9% | 4.1% | 2/10 | 卖方有利 |
| NVDA | 8.0% | 7.2% | 2/10 | 卖方有利 |
| MSFT | 4.7% | 8.0% | 6/10 | 隐含低估,买方有利 |
| PLTR | 10.5% | 19.1% | 5/10 | 买方有利 |
| **合计** | 10.0% | 10.6% | **36%** | 比值 1.07 但仅 36% 超出 = 高胜率负偏态 |

这是卖波动率的典型画像:多数时候赚小钱,少数几次输大钱。分票差异远大于整体均值,
**按标的区分买/卖方立场比任何 LLM 方向预测都可靠**。后续应沿此方向,而非 7.1。
