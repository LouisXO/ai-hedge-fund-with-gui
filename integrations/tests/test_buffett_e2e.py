"""P1 final acceptance: real aihf BuffettAgent + MoomooDataClient + ClaudeCodeLLM."""
import sys, time
sys.path.insert(0, "/Users/louis/hedge-fund")
from integrations.moomoo_client import MoomooDataClient
from integrations.claude_code_llm import ClaudeCodeLLM

from hedge_fund.signals.buffett import BuffettAgent

data = MoomooDataClient()
agent = BuffettAgent(llm=ClaudeCodeLLM(model="opus"))
t0 = time.time()
sig = agent.predict("RKLB", "2026-09-11", data)
dt = time.time() - t0
data.close()
print(f"\n=== BuffettAgent(RKLB) — {dt:.0f}s, 全链路 moomoo→ClaudeCode, $0 API ===")
print("signal    :", sig.signal if hasattr(sig,'signal') else sig)
print("conviction:", getattr(sig, 'conviction', None))
print("thesis    :", (getattr(sig, 'thesis', '') or getattr(sig, 'reasoning', ''))[:400])
