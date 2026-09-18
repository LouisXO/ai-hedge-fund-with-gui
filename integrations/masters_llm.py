"""MastersLLM — the master personas' LLMClient: typed contract over sealed Claude Code.

Same shape as upstream's JevLLM: LLMAgent hands over the persona prompt and
the snapshot unchanged; this adapter asks the typed questions from
integrations.masters_contract, samples several times, and returns the
{signal, confidence, reasoning, provider_metadata} JSON LLMAgent already parses.

cache_key() puts the resolved model, transport and contract versions and the
sample count into the prompt-cache identity, so changing any of them re-asks
instead of replaying answers produced under different rules.

Usage:
    llm = MastersLLM(ClaudeCodeLLM(model="opus"), samples=3)
    agent = BuffettAgent(llm=llm)
"""
from __future__ import annotations

import hashlib
import json

from hedge_fund.llm import LLMCallError

from integrations import masters_contract as contract
from integrations.claude_code_llm import TRANSPORT_VERSION, ClaudeCodeLLM


class MastersLLM:
    def __init__(self, transport: ClaudeCodeLLM | None = None, samples: int = 3) -> None:
        if samples < 1:
            raise ValueError("samples must be >= 1")
        self._transport = transport if transport is not None else ClaudeCodeLLM()
        self._samples = samples

    @property
    def model(self) -> str:
        return self._transport.model

    def cache_key(self, agent: str, system: str, user: str) -> str:
        identity = {
            "provider": "ClaudeCode",
            "adapter": "MastersLLM",
            "transport_version": TRANSPORT_VERSION,
            "contract_version": contract.CONTRACT_VERSION,
            "samples": self._samples,
            "model": self._transport.resolved_model(),
            "agent": agent,
            "system": system,
            "user": user,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def complete(self, system: str, user: str) -> str:
        sealed_system = contract.build_system(system)
        valid, failures, calls = [], [], []
        for i in range(self._samples):
            try:
                meta = self._transport.call(sealed_system, user)
            except LLMCallError as exc:
                failures.append({"sample": i, "error": str(exc),
                                 "diagnostics": exc.diagnostic_record})
                continue
            calls.append({k: meta.get(k) for k in
                          ("resolved_model", "duration_ms", "attempts", "cost_usd_equiv")})
            try:
                valid.append(contract.parse_sample(meta["result"]))
            except contract.ContractError as exc:
                failures.append({"sample": i, "error": f"contract: {exc}",
                                 "raw": meta["result"][-4000:]})

        # A majority of the requested samples must be usable, otherwise the
        # "majority direction" would be decided by whatever survived.
        if len(valid) * 2 <= self._samples:
            raise LLMCallError(
                f"only {len(valid)}/{self._samples} usable samples",
                {"provider": "ClaudeCode", "adapter": "MastersLLM",
                 "contract_version": contract.CONTRACT_VERSION,
                 "failure_category": "samples", "failures": failures, "calls": calls})

        payload = contract.aggregate(valid, self._samples)
        meta = payload["provider_metadata"]["masters_contract"]
        meta["calls"] = calls
        if failures:
            meta["failures"] = failures
        return json.dumps(payload)
