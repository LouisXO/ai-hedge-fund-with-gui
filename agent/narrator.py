"""The narrator: two or three Chinese sentences for brief section ⑨, from numbers only.

The one place an LLM touches the daily output, and it cannot change a
number: it receives a JSON of figures already computed (regime, gate,
picks, paper books, live scoreboard), must answer with strict JSON
{headline, commentary, caveat}, and every digit-bearing token in its text
must appear in the input. Anything else falls back to a deterministic
template sentence and is logged. Sealed transport (integrations/
claude_code_llm.py: no tools, no MCP, single turn), one sample, cached by
(version, system prompt, input) so re-rendering the brief costs nothing.

Pre-registered contract v1 (docs/AGENT_PLAN.md §5.7).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from hedge_fund.paths import AGENT_DIR

NARRATOR_VERSION = "1"
CACHE_DIR = AGENT_DIR / "narrator_cache"
SYSTEM = (
    "你是一个只做转述的写作助手。你会收到一个 JSON,里面是一个量化交易系统当天已经算好的数字。"
    "用中文写 2–3 句话,只能复述输入里出现过的数字,不得计算、不得预测、不得引用任何外部知识或新闻,不得给建议。"
    "严格输出一个 JSON 对象,键为 headline(≤30 字)、commentary(2–3 句)、caveat(1 句,说明记录期短或未验证),不要输出其他内容。"
)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def cache_key(system: str, payload: str) -> str:
    return hashlib.sha256(f"narrator|{NARRATOR_VERSION}|{system}|{payload}".encode()).hexdigest()[:24]


def numbers_in(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM.findall(text)}


def validate(out: dict, payload: str) -> str | None:
    """None if acceptable, else the reason."""
    if not isinstance(out, dict) or not all(isinstance(out.get(k), str) and out.get(k) for k in ("headline", "commentary", "caveat")):
        return "shape"
    if len(out["headline"]) > 40:
        return "headline_too_long"
    allowed = numbers_in(payload)
    # tolerate numbers that appear as a rounded form of an input number (12.6 -> 12.6, 12.63 -> 12.6)
    rounded = {f"{float(x):.1f}" for x in allowed if x.replace(".", "", 1).isdigit()} | {f"{float(x):.0f}" for x in allowed if x.replace(".", "", 1).isdigit()}
    for k in ("headline", "commentary", "caveat"):
        for n in numbers_in(out[k]):
            if n not in allowed and n not in rounded:
                return f"fabricated_number:{n}"
    return None


def template(d: dict) -> dict:
    paper = d.get("paper") or {}
    pl = "、".join(f"{k} {v:+.1f}%" for k, v in paper.items()) if paper else "模拟盘尚无记录"
    return {"headline": f"{d.get('as_of', '')} 影子记录照常",
            "commentary": f"便宜门通过 {d.get('gate_passed', 0)}/{d.get('gate_of', 0)},期权候选 {d.get('n_option_picks', 0)} 个,"
                          f"内部人候选 {d.get('n_insider_picks', 0)} 个。模拟盘:{pl}。",
            "caveat": "以上为影子/模拟记录,没有信号通过多重检验校正,不构成任何建议。"}


def extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def narrate(d: dict, transport=None, use_cache: bool = True) -> dict:
    """d: numbers only (see brief.narrator_input). Returns {headline, commentary, caveat, source}."""
    payload = json.dumps(d, ensure_ascii=False, sort_keys=True, default=float)
    key = cache_key(SYSTEM, payload)
    path = Path(CACHE_DIR) / f"{key}.json"
    if use_cache and path.exists():
        return json.load(open(path))
    if transport is None:
        try:
            from integrations.claude_code_llm import ClaudeCodeLLM
            transport = ClaudeCodeLLM(model="sonnet", timeout=60.0)
        except Exception:
            return dict(template(d), source="template:no_transport")
    try:
        r = transport.call(SYSTEM, payload)
        text = r["result"] if isinstance(r, dict) else str(r)
        out = extract_json(text)
        reason = validate(out, payload)
        if reason:
            result = dict(template(d), source=f"template:{reason}")
        else:
            result = {k: out[k] for k in ("headline", "commentary", "caveat")}
            result["source"] = f"llm:{getattr(transport, 'resolved_model', lambda: 'unknown')()}"
    except Exception as exc:
        result = dict(template(d), source=f"template:error:{str(exc)[:60]}")
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(result, f, ensure_ascii=False)
    return result
