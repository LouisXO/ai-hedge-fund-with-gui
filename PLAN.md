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
- 部署:**Netlify 不动**(现有 push-to-deploy;实测 DNS 已在 Cloudflare 且流量已走 CF 代理)+ **Cloudflare Access** 直接挂自定义域(免费邮箱 OTP);Netlify Edge Function 校验 Cf-Access-Jwt-Assertion 头封死 *.netlify.app 绕行;tunnel 留给 P4 的按需后端。

## 2. 安全红线(先于一切上线动作)

1. **Cloudflare Access 必须先开**(免费,邮箱 OTP):新站含真实持仓,当前老站是公开的。Access 没配好之前,site-data 里不放持仓页。
2. 工具链全程只读 moomoo:**永不下单**,交易解锁只在 OpenD 手动。
3. 回测**不走订阅**:API+Haiku(几美元级)或仅 PEAD 量化 pod(零 LLM)。

## 3. 阶段拆解

| 阶段 | 交付 | 依赖 |
|---|---|---|
| **P1 MoomooDataClient** | aihf `DataClient` 协议 6 方法 → moomoo 实现 + 契约测试跑通;Buffett agent 用真实 RKLB 快照出信号 | 无 |
| **P2 大师信号管线** | masters/run_masters.py:5 agents × (持仓+Top5) → site-data/masters/<date>.json;接进 8:41 wrapper;iMessage 摘要加一行"大师分歧" | P1 |
| **P3 网站 v2** | 三页面静态站部署到现有 Netlify + **CF Access 门禁 + JWT 边缘校验** + 老站转 legacy 路径 | P2 |
| **P4 回测实验室** | FastAPI(tunnel 按需启)+ 页面触发回测(Haiku/PEAD)+ 结果归档进站 | P3 |
| **P5 期权回测(远期)** | OptRadar DuckDB 攒 3+ 个月期权链后,策略级期权回测(卖 strangle/价差胜率) | 数据积累 |

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
