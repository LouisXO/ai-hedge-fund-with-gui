# integrations/ — moomoo × aihf 接线层 (P1 完成)

- `moomoo_client.py` — MoomooDataClient,满足 aihf `DataClient` 协议(6+方法),
  基于快接口(history kline / snapshot / financials income stmt / news)。
- `claude_code_llm.py` — ClaudeCodeLLM,把 aihf 的 LLMClient 走本机 Claude Code 订阅($0 API)。
- `tests/test_contract.py` — 协议契约 + 真实数据验收。
- `tests/test_buffett_e2e.py` — BuffettAgent 端到端(moomoo 数据 + Claude Code → 信号)。

运行环境:`~/.hedgefund-venv`(Python 3.13,装了 aihf 可编辑 + moomoo-api)。
`~/.hedgefund-venv/bin/python integrations/tests/test_buffett_e2e.py`

P1 已知边界:财务只取利润表(营收/毛利/营业/净利/增长)+ 快照估值;
ROE/负债/BVPS/现金流需资产负债表+现金流量表(P2)。仅适合实盘信号,非回测(快照估值是当下值,非 point-in-time)。
