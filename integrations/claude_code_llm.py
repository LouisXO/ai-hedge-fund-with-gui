"""ClaudeCodeLLM — routes aihf's LLMClient protocol through the local Claude Code
CLI (subscription auth). Satisfies hedge_fund.llm.LLMClient by duck typing:
`model` attr + `complete(system, user) -> str`. No ANTHROPIC_API_KEY needed.

Sealed. A plain `claude -p --append-system-prompt` call is a full coding
agent: Claude Code's own ~60k-token system prompt, built-in tools, and every
claude.ai MCP connector on the account (Alpha Vantage live quotes included).
A persona "judging 2025 fundamentals" could fetch today's prices — look-ahead
that silently poisons backtests. So every call replaces the system prompt and
disables built-in tools, MCP servers, settings and session persistence, runs
from an empty directory, and is rejected if it took more than one turn.
Measured: 1 turn / ~0.5k context / ~6s versus 2+ turns / ~67k / ~12s.

Retries matter here. A backtest issues hundreds of sequential `claude -p`
calls; a bare non-zero exit rate of even a few percent silently turns into
abstained signals, and an abstained signal is indistinguishable from a
considered neutral view — it quietly corrupts the result. So transient
failures are retried with backoff, and a genuinely failed call raises
LLMCallError carrying a diagnostic record that LLMAgent persists in the cache.

Model aliases ("opus") drift when Claude Code repoints them, so cache keys use
the resolved model id, probed once per process per alias.

Usage with aihf:
    from hedge_fund.signals.buffett import BuffettAgent
    agent = BuffettAgent(llm=ClaudeCodeLLM(model="opus"))
For the master personas prefer integrations.masters_llm.MastersLLM.
"""
import hashlib
import json
import os
import random
import subprocess
import threading
import time

from hedge_fund.llm import LLMCallError
from hedge_fund.paths import CACHE_DIR

CLAUDE_BIN = os.path.expanduser("~/.claude/local/claude")

# Bump when the invocation changes in a way that can change answers.
TRANSPORT_VERSION = 2

SEALED_FLAGS = [
    "--tools", "",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--setting-sources", "",
    "--no-session-persistence",
    "--output-format", "json",
]
SEALED_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
SEALED_CWD = CACHE_DIR / "claude-sealed-cwd"

_resolved: dict[str, str] = {}
_resolve_lock = threading.Lock()


class ClaudeCodeLLM:
    def __init__(self, model: str = "opus", timeout: float = 90.0,
                 retries: int = 3, base_delay: float = 4.0):
        self.model = model
        self._timeout = timeout
        self._retries = retries
        self._base_delay = base_delay

    def complete(self, system: str, user: str) -> str:
        return self.call(system, user)["result"]

    def cache_key(self, agent: str, system: str, user: str) -> str:
        identity = {
            "provider": "ClaudeCode",
            "transport_version": TRANSPORT_VERSION,
            "model": self.resolved_model(),
            "agent": agent,
            "system": system,
            "user": user,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def resolved_model(self) -> str:
        """The concrete model id behind self.model (e.g. opus -> claude-opus-5)."""
        with _resolve_lock:
            if self.model not in _resolved:
                meta = self.call("Reply with the single word OK.", "OK")
                _resolved[self.model] = meta["resolved_model"]
            return _resolved[self.model]

    def call(self, system: str, user: str) -> dict:
        """One sealed call -> {result, resolved_model, usage, duration_ms, attempts}."""
        cmd = [CLAUDE_BIN, "-p", user, "--system-prompt", system,
               "--model", self.model, *SEALED_FLAGS]
        SEALED_CWD.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **SEALED_ENV}
        started = time.monotonic()
        attempts: list[dict] = []

        def fail(category: str, message: str):
            raise LLMCallError(message, {
                "provider": "ClaudeCode",
                "transport_version": TRANSPORT_VERSION,
                "model": self.model,
                "failure_category": category,
                "attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })

        for attempt in range(1, self._retries + 1):
            t0 = time.monotonic()
            rec = {"attempt": attempt}
            attempts.append(rec)
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout, cwd=SEALED_CWD, env=env)
            except subprocess.TimeoutExpired:
                rec["error"] = f"timeout after {self._timeout}s"
            else:
                rec["returncode"] = proc.returncode
                if proc.returncode != 0:
                    # the CLI often exits non-zero with an empty stderr when it
                    # is being rate-limited; treat every non-zero as transient
                    rec["error"] = "non-zero exit"
                    rec["stderr_tail"] = (proc.stderr or "")[-2000:]
                    rec["stdout_tail"] = (proc.stdout or "")[-2000:]
                else:
                    try:
                        envelope = json.loads(proc.stdout)
                    except ValueError:
                        rec["error"] = "unparseable envelope"
                        rec["stdout_tail"] = proc.stdout[-2000:]
                    else:
                        if envelope.get("is_error"):
                            rec["error"] = "error envelope"
                            rec["envelope"] = {k: envelope.get(k) for k in
                                               ("subtype", "api_error_status", "result", "stop_reason")}
                        elif envelope.get("num_turns", 1) != 1:
                            rec["num_turns"] = envelope.get("num_turns")
                            rec["permission_denials"] = envelope.get("permission_denials")
                            fail("unsealed", f"claude -p took {envelope.get('num_turns')} turns — "
                                             "a tool was reachable; refusing the answer")
                        else:
                            usage = envelope.get("modelUsage") or {}
                            resolved = max(usage, key=lambda m: usage[m].get("outputTokens", 0),
                                           default=self.model)
                            return {
                                "result": envelope["result"],
                                "resolved_model": resolved,
                                "usage": envelope.get("usage"),
                                "cost_usd_equiv": envelope.get("total_cost_usd"),
                                "duration_ms": envelope.get("duration_ms"),
                                "attempts": attempt,
                            }
            rec["elapsed_seconds"] = round(time.monotonic() - t0, 1)
            if attempt < self._retries:
                time.sleep(self._base_delay * (2 ** (attempt - 1)) + random.uniform(0, 2))

        fail("exhausted", f"claude -p failed after {self._retries} attempts: "
                          f"{attempts[-1].get('error')}")
