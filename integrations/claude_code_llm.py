"""ClaudeCodeLLM — routes aihf's LLMClient protocol through the local Claude Code
CLI (subscription auth). Satisfies hedge_fund.llm.LLMClient by duck typing:
`model` attr + `complete(system, user) -> str`. No ANTHROPIC_API_KEY needed.

Retries matter here. A backtest issues hundreds of sequential `claude -p`
calls; a bare non-zero exit rate of even a few percent silently turns into
abstained signals, and an abstained signal is indistinguishable from a
considered neutral view — it quietly corrupts the result. So transient
failures are retried with backoff, and a genuinely failed call raises.

Usage with aihf:
    from hedge_fund.signals.buffett import BuffettAgent
    agent = BuffettAgent(llm=ClaudeCodeLLM(model="opus"))
"""
import json
import os
import random
import subprocess
import time

CLAUDE_BIN = os.path.expanduser("~/.claude/local/claude")


class ClaudeCodeLLM:
    def __init__(self, model: str = "opus", timeout: float = 300.0,
                 retries: int = 4, base_delay: float = 4.0):
        self.model = model
        self._timeout = timeout
        self._retries = retries
        self._base_delay = base_delay

    def complete(self, system: str, user: str) -> str:
        cmd = [
            CLAUDE_BIN, "-p", user,
            "--append-system-prompt", system,
            "--model", self.model,
            "--output-format", "json",
        ]
        last = ""
        for attempt in range(self._retries):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout)
            except subprocess.TimeoutExpired:
                last = f"timeout after {self._timeout}s"
            else:
                if proc.returncode == 0:
                    try:
                        envelope = json.loads(proc.stdout)
                    except ValueError:
                        last = f"unparseable output: {proc.stdout[:200]}"
                    else:
                        if envelope.get("is_error"):
                            last = f"error envelope: {str(envelope)[:200]}"
                        else:
                            return envelope["result"]
                else:
                    # the CLI often exits non-zero with an empty stderr when it
                    # is being rate-limited; treat every non-zero as transient
                    last = (f"exit {proc.returncode}: "
                            f"{(proc.stderr or proc.stdout or '').strip()[:200]}")

            if attempt < self._retries - 1:
                delay = self._base_delay * (2 ** attempt) + random.uniform(0, 2)
                time.sleep(delay)

        raise RuntimeError(f"claude -p failed after {self._retries} attempts: {last}")
