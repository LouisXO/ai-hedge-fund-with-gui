"""Typed contract for the master personas — pure prompt building and mapping.

Borrowed from upstream's Jev contract (hedge_fund/llm/contract.py): the persona
prompt stays verbatim as reference material, and the model answers typed
questions instead of choosing its own 0-100 "confidence".

  - direction: bullish | bearish | neutral, under the persona's signal rules
  - bullish_strength / bearish_strength: 0-4 on a fixed evidence rubric, each
    case scored independently of the direction answer

The compatibility payload keeps LLMAgent's {signal, confidence, reasoning}
shape. `confidence` is investment conviction (strength x 25), not the model's
certainty in its answer. Neutral carries zero conviction.

Several samples are aggregated: majority direction (no majority -> neutral),
median strength of the chosen case. Free-form confidence clustered in 55-91
and flipped direction on unchanged weekend data, so one sample is too noisy.

Bump CONTRACT_VERSION whenever the questions, rubric or mapping change — it
is part of the prompt-cache identity.
"""

from __future__ import annotations

import statistics
from collections import Counter

from hedge_fund.llm import extract_json

CONTRACT_VERSION = 1

DIRECTIONS = ("bullish", "bearish", "neutral")

STRENGTH_LEVELS = (
    "Evidence does not support the case or directly contradicts it.",
    "Limited support; substantial unsupported assumptions are required.",
    "Meaningful support with material conflicting evidence or unresolved gaps.",
    "Strong support across relevant criteria with limited material weaknesses.",
    "Compelling support across relevant criteria with no material contradiction "
    "apparent in the supplied evidence.",
)

EVIDENCE_RULES = """Evidence rules (these override anything in the persona brief):
- Judge ONLY from the financial snapshot in the user message. It is the whole
  of what this investor knows.
- Treat the snapshot's latest period as the present. Ignore anything you know
  about this company, its stock price, news or events after that data —
  including the current date of this session. Using outside knowledge makes
  the assessment worthless for backtesting.
- Ignore the persona brief's instructions about response format and its
  confidence scale; the rubric below replaces them. Keep its criteria,
  signal definitions and voice."""


class ContractError(ValueError):
    """A sample cannot be interpreted as a typed persona assessment."""


def build_system(persona_prompt: str) -> str:
    rubric = "\n".join(f"  {i} = {text}" for i, text in enumerate(STRENGTH_LEVELS))
    return f"""<persona_brief>
{persona_prompt.strip()}
</persona_brief>

You are the investor described in the persona brief.

{EVIDENCE_RULES}

Answer three questions:
1. direction — which assessment fits the evidence under this investor's
   signal rules: bullish, bearish or neutral.
2. bullish_strength — how strongly does the evidence support this investor's
   bullish case? Score it on its own, whatever you answered for direction.
3. bearish_strength — the same for the bearish case, scored on its own.

Strength rubric (integer 0-4). Score the strength of the investment case, not
your certainty in the answer or the probability of profit:
{rubric}

Reply with ONLY this JSON object, no prose around it:
{{"direction": "bullish|bearish|neutral", "bullish_strength": 0-4,
  "bearish_strength": 0-4, "reasoning": "3-6 sentences in the investor's voice,
  citing figures from the snapshot"}}"""


def parse_sample(text: str) -> dict:
    """One model reply -> validated {direction, bullish_strength, bearish_strength, reasoning}."""
    try:
        data = extract_json(text)
    except ValueError as exc:
        raise ContractError(f"no JSON object: {exc}") from None
    direction = str(data.get("direction", "")).strip().lower()
    if direction not in DIRECTIONS:
        raise ContractError(f"direction must be one of {DIRECTIONS}, got {data.get('direction')!r}")
    out = {"direction": direction}
    for case in ("bullish_strength", "bearish_strength"):
        value = data.get(case)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) \
                or not 0 <= value <= 4:
            raise ContractError(f"{case} must be an integer 0-4, got {value!r}")
        out[case] = int(value)
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ContractError("reasoning must be a non-empty string")
    out["reasoning"] = reasoning.strip()
    return out


def aggregate(samples: list[dict], requested: int) -> dict:
    """Valid samples -> LLMAgent compatibility payload with provider metadata.

    `requested` is how many samples were asked for; a majority means more than
    half of those, so failed samples count against agreement.
    """
    if not samples:
        raise ContractError("no valid samples to aggregate")
    votes = Counter(s["direction"] for s in samples)
    top, count = votes.most_common(1)[0]
    direction = top if count * 2 > requested else "neutral"

    if direction == "neutral":
        strength = 0.0
    else:
        strength = float(statistics.median(s[f"{direction}_strength"] for s in samples))
    confidence = strength * 25

    matching = [s for s in samples if s["direction"] == direction]
    if matching and direction != "neutral":
        pick = min(matching, key=lambda s: abs(s[f"{direction}_strength"] - strength))
        reasoning = pick["reasoning"]
    elif matching:
        reasoning = matching[0]["reasoning"]
    else:
        split = ", ".join(f"{d} {n}" for d, n in votes.most_common())
        reasoning = f"Samples split ({split}); no majority, so no directional view."

    return {
        "signal": direction,
        "confidence": confidence,
        "reasoning": reasoning,
        "provider_metadata": {
            "masters_contract": {
                "contract_version": CONTRACT_VERSION,
                "requested": requested,
                "valid": len(samples),
                "agreement": round(count / requested, 3),
                "votes": dict(votes),
                "strength": strength,
                "samples": [{k: s[k] for k in ("direction", "bullish_strength", "bearish_strength")}
                            for s in samples],
            }
        },
    }
