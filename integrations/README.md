# integrations/ — moomoo × aihf 接线层 (P1 完成)

- `moomoo_client.py` — MoomooDataClient,满足 aihf `DataClient` 协议(6+方法),
  基于快接口(history kline / snapshot / financials income stmt / news)。
- `claude_code_llm.py` — ClaudeCodeLLM,把 aihf 的 LLMClient 走本机 Claude Code 订阅($0 API)。
  **密封调用**:替换系统提示词,关掉内置工具、MCP(含 claude.ai 连接器)、设置文件和会话持久化,
  超过 1 轮直接拒收——否则大师能调实时行情,回测被未来数据污染。缓存键用解析后的真实模型 ID。
- `masters_contract.py` — 大师的类型化合约(借鉴上游 Jev):方向 + 0–4 强度评分标准,
  多次采样取多数方向、中位强度;`confidence` = 强度 × 25,中性为 0。改题目/标准/映射就升 `CONTRACT_VERSION`。
- `masters_llm.py` — MastersLLM,run_masters / backtest 用的 LLMClient(合约 + 密封传输 + 采样)。
- `tests/test_masters_llm.py` — 上面三个的单元测试(无网络):`pytest integrations/tests/test_masters_llm.py`
- `tests/test_contract.py` — 协议契约 + 真实数据验收。
- `tests/test_buffett_e2e.py` — BuffettAgent 端到端(moomoo 数据 + Claude Code → 信号)。

运行环境:`~/.hedgefund-venv`(Python 3.13,装了 aihf 可编辑 + moomoo-api)。
moomoo-api 不在 poetry 依赖里,新机器 / `poetry install --sync` 之后要补:
`~/.hedgefund-venv/bin/python -m pip install moomoo-api==10.10.7008`
`~/.hedgefund-venv/bin/python integrations/tests/test_buffett_e2e.py`

P1 已知边界:财务只取利润表(营收/毛利/营业/净利/增长)+ 快照估值;
ROE/负债/BVPS/现金流需资产负债表+现金流量表(P2)。仅适合实盘信号,非回测(快照估值是当下值,非 point-in-time)。
