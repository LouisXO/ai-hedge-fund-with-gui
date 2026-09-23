"""Headline translation (English -> Chinese) for display only.

Display text, never an input to any signal. One batched call to the sealed Claude Code LLM
(integrations/claude_code_llm.py: no tools, single turn), cached per headline in
~/.hedge-fund/agent/translate_cache.json so a headline is translated once. Any failure returns
the English original, so a page never breaks on translation.
"""
from __future__ import annotations

import json
import os
import re

from hedge_fund.paths import AGENT_DIR

CACHE = AGENT_DIR / "translate_cache.json"
SYSTEM = ("把下面 JSON 数组里的英文财经新闻标题逐条翻译成简体中文。保留股票代码、公司英文名(可在后面括注中文)、数字和单位原样。"
          "不要添加原文没有的信息,不要评论。只输出一个 JSON 数组,长度与输入相同,顺序一致。")


def _load() -> dict:
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}


def translate(headlines: list[str], transport=None) -> dict[str, str]:
    """{english: chinese} for every headline; untranslatable ones map to themselves."""
    cache = _load()
    todo = [h for h in dict.fromkeys(headlines) if h and h not in cache]
    if todo:
        try:
            if transport is None:
                from integrations.claude_code_llm import ClaudeCodeLLM
                transport = ClaudeCodeLLM(model="sonnet", timeout=90.0)
            r = transport.call(SYSTEM, json.dumps(todo, ensure_ascii=False))
            text = r["result"] if isinstance(r, dict) else str(r)
            m = re.search(r"\[.*\]", text, re.S)
            out = json.loads(m.group(0)) if m else []
            if isinstance(out, list) and len(out) == len(todo):
                for en, zh in zip(todo, out):
                    if isinstance(zh, str) and zh.strip():
                        cache[en] = zh.strip()
                os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                with open(CACHE, "w") as f:
                    json.dump(cache, f, ensure_ascii=False)
        except Exception as exc:
            print(f"translate skipped: {str(exc)[:80]}")
    return {h: cache.get(h, h) for h in headlines}
