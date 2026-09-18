"""Unit tests for the sealed transport, the masters contract and MastersLLM. No network."""
import json
import subprocess

import pytest

from hedge_fund.llm import LLMCallError
from hedge_fund.llm.cache import PromptCache
from hedge_fund.signals.buffett import BuffettAgent
from integrations import claude_code_llm as ccl
from integrations import masters_contract as contract
from integrations.masters_llm import MastersLLM


def sample(direction, bull, bear, reasoning="Because the numbers say so."):
    return json.dumps({"direction": direction, "bullish_strength": bull,
                       "bearish_strength": bear, "reasoning": reasoning})


class FakeTransport:
    model = "opus"

    def __init__(self, replies):
        self._replies = list(replies)
        self.systems = []

    def resolved_model(self):
        return "claude-opus-5"

    def call(self, system, user):
        self.systems.append(system)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"result": reply, "resolved_model": "claude-opus-5", "duration_ms": 1, "attempts": 1}


# ---------------------------------------------------------------- contract

def test_system_prompt_keeps_persona_and_adds_rules():
    system = contract.build_system("You are Warren Buffett.")
    assert "You are Warren Buffett." in system
    assert "Judge ONLY from the financial snapshot" in system
    assert "4 = Compelling support" in system


@pytest.mark.parametrize("bad", [
    '{"direction": "up", "bullish_strength": 1, "bearish_strength": 1, "reasoning": "x"}',
    '{"direction": "bullish", "bullish_strength": 5, "bearish_strength": 1, "reasoning": "x"}',
    '{"direction": "bullish", "bullish_strength": 2.5, "bearish_strength": 1, "reasoning": "x"}',
    '{"direction": "bullish", "bullish_strength": true, "bearish_strength": 1, "reasoning": "x"}',
    '{"direction": "bullish", "bullish_strength": 2, "bearish_strength": 1, "reasoning": ""}',
    "no json here",
])
def test_parse_sample_rejects_malformed(bad):
    with pytest.raises(contract.ContractError):
        contract.parse_sample(bad)


def test_parse_sample_accepts_fenced_json():
    got = contract.parse_sample("```json\n" + sample("Bearish", 1, 3.0) + "\n```")
    assert got["direction"] == "bearish" and got["bearish_strength"] == 3


def test_majority_direction_and_median_strength():
    samples = [contract.parse_sample(sample(*s)) for s in
               [("bullish", 3, 1), ("bullish", 4, 0), ("neutral", 2, 2)]]
    out = contract.aggregate(samples, requested=3)
    assert out["signal"] == "bullish"
    assert out["confidence"] == 75.0          # median bullish strength 3 x 25
    meta = out["provider_metadata"]["masters_contract"]
    assert meta["agreement"] == pytest.approx(0.667, abs=1e-3)
    assert meta["votes"] == {"bullish": 2, "neutral": 1}


def test_no_majority_is_neutral_with_zero_conviction():
    samples = [contract.parse_sample(sample(*s)) for s in
               [("bullish", 3, 1), ("bearish", 1, 3), ("neutral", 2, 2)]]
    out = contract.aggregate(samples, requested=3)
    assert out["signal"] == "neutral" and out["confidence"] == 0.0


def test_failed_samples_count_against_majority():
    # 1 bullish of 3 requested (2 failed) is not a majority
    samples = [contract.parse_sample(sample("bullish", 4, 0))]
    assert contract.aggregate(samples, requested=3)["signal"] == "neutral"


def test_directional_zero_strength_keeps_direction():
    samples = [contract.parse_sample(sample("bearish", 0, 0))] * 3
    out = contract.aggregate(samples, requested=3)
    assert out["signal"] == "bearish" and out["confidence"] == 0.0


# ---------------------------------------------------------------- MastersLLM

def test_masters_llm_payload_parses_through_llm_agent(tmp_path):
    fake = FakeTransport([sample("bearish", 1, 3), sample("bearish", 0, 4), sample("neutral", 1, 2)])
    llm = MastersLLM(fake, samples=3)
    response = llm.complete("persona", "snapshot")
    parsed = BuffettAgent(llm=llm, cache=PromptCache(tmp_path))._parse(response)
    assert parsed["signal"] == "bearish" and parsed["confidence"] == 75.0  # median bearish 3
    assert parsed["provider_metadata"]["masters_contract"]["valid"] == 3
    assert all("persona" in s and "Evidence rules" in s for s in fake.systems)


def test_masters_llm_tolerates_one_bad_sample():
    fake = FakeTransport([sample("bullish", 3, 1), "garbage", sample("bullish", 2, 1)])
    out = json.loads(MastersLLM(fake, samples=3).complete("persona", "snapshot"))
    assert out["signal"] == "bullish"
    assert out["provider_metadata"]["masters_contract"]["failures"][0]["error"].startswith("contract")


def test_masters_llm_raises_with_diagnostics_without_majority():
    fake = FakeTransport([LLMCallError("boom", {"x": 1}), "garbage", sample("bullish", 3, 1)])
    with pytest.raises(LLMCallError) as exc:
        MastersLLM(fake, samples=3).complete("persona", "snapshot")
    diag = exc.value.diagnostic_record
    assert diag["failure_category"] == "samples" and len(diag["failures"]) == 2


def test_cache_key_changes_with_contract_samples_and_model(monkeypatch):
    fake = FakeTransport([])
    k3 = MastersLLM(fake, samples=3).cache_key("buffett", "s", "u")
    assert k3 == MastersLLM(fake, samples=3).cache_key("buffett", "s", "u")
    assert k3 != MastersLLM(fake, samples=1).cache_key("buffett", "s", "u")
    monkeypatch.setattr(contract, "CONTRACT_VERSION", 99)
    assert k3 != MastersLLM(fake, samples=3).cache_key("buffett", "s", "u")
    monkeypatch.setattr(contract, "CONTRACT_VERSION", 1)
    fake.resolved_model = lambda: "claude-opus-6"
    assert k3 != MastersLLM(fake, samples=3).cache_key("buffett", "s", "u")


# ---------------------------------------------------------------- sealed transport

def envelope(**kw):
    base = {"is_error": False, "num_turns": 1, "result": "hello",
            "modelUsage": {"claude-opus-5": {"outputTokens": 10}}}
    return json.dumps({**base, **kw})


def test_call_is_sealed(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, envelope(), "")
    monkeypatch.setattr(ccl.subprocess, "run", fake_run)
    out = ccl.ClaudeCodeLLM().call("SYS", "USER")
    cmd = seen["cmd"]
    assert out["result"] == "hello" and out["resolved_model"] == "claude-opus-5"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert "--append-system-prompt" not in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert seen["kw"]["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert seen["kw"]["cwd"] == ccl.SEALED_CWD


def test_multi_turn_answer_is_refused(monkeypatch):
    monkeypatch.setattr(ccl.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 0, envelope(num_turns=3), ""))
    with pytest.raises(LLMCallError) as exc:
        ccl.ClaudeCodeLLM().call("SYS", "USER")
    assert exc.value.diagnostic_record["failure_category"] == "unsealed"


def test_retries_then_raises_with_attempt_log(monkeypatch):
    monkeypatch.setattr(ccl.time, "sleep", lambda s: None)
    monkeypatch.setattr(ccl.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 1, "", "rate limited"))
    with pytest.raises(LLMCallError) as exc:
        ccl.ClaudeCodeLLM(retries=3).call("SYS", "USER")
    diag = exc.value.diagnostic_record
    assert diag["failure_category"] == "exhausted" and len(diag["attempts"]) == 3
    assert diag["attempts"][0]["stderr_tail"] == "rate limited"


def test_llm_agent_persists_diagnostics_on_failure(tmp_path, monkeypatch):
    fake = FakeTransport([LLMCallError("x", {"a": 1})] * 3)
    agent = BuffettAgent(llm=MastersLLM(fake, samples=3), cache=PromptCache(tmp_path))
    monkeypatch.setattr(agent, "build_snapshot", lambda *a: type(
        "S", (), {"render": lambda self: "snap", "content_hash": "h"})())
    sig = agent.predict("RKLB", "2026-09-17", data_client=None)
    assert sig.metadata["abstained"] is True
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["diagnostics"]["failure_category"] == "samples"
