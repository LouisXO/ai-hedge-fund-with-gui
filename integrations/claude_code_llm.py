"""ClaudeCodeLLM — routes aihf's LLMClient protocol through the local Claude Code
CLI (subscription auth). Satisfies hedge_fund.llm.LLMClient by duck typing:
`model` attr + `complete(system, user) -> str`. No ANTHROPIC_API_KEY needed.

Usage with aihf:
    from hedge_fund.signals.buffett import BuffettAgent
    agent = BuffettAgent(llm=ClaudeCodeLLM(model="opus"))
"""
import json
import os
import subprocess

CLAUDE_BIN = os.path.expanduser("~/.claude/local/claude")


class ClaudeCodeLLM:
    def __init__(self, model: str = "opus", timeout: float = 300.0):
        self.model = model
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        cmd = [
            CLAUDE_BIN, "-p", user,
            "--append-system-prompt", system,
            "--model", self.model,
            "--output-format", "json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self._timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr[:400]}")
        envelope = json.loads(proc.stdout)
        if envelope.get("is_error"):
            raise RuntimeError(f"claude -p error: {str(envelope)[:400]}")
        return envelope["result"]
