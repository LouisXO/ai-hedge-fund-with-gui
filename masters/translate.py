#!/usr/bin/env python
"""把大师理由翻成中文,写回 masters JSON 的 reasoning_cn 字段。

英文原文保留(reasoning 不动),页面可切换。一次批量翻译整天的理由:
比逐条调用快一个数量级,也让译文风格在同一上下文里保持一致。
Usage: python masters/translate.py [YYYY-MM-DD] [--force]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.claude_code_llm import ClaudeCodeLLM

MASTERS = os.path.join(ROOT, "site-data", "masters")

SYSTEM = """你是金融翻译。把投资大师的英文分析逐条翻成中文。

要求:
- 保留原有的语气和个人风格(Buffett 的家常比喻、Munger 的尖刻、Graham 的严谨、
  Lynch 的通俗、Druckenmiller 的果断),不要翻成中性报告腔。
- 金融术语用中文惯用译法:gross margin→毛利率,operating margin→营业利润率,
  ROE→净资产收益率,book value→账面价值,margin of safety→安全边际,
  free cash flow→自由现金流,multiple→倍数,conviction→信心。
- 数字、百分比、货币金额、股票代码原样保留。
- 不要增删内容,不要加解释。

输出格式(不要用 JSON —— 译文里的引号会破坏它):
每条译文前单独一行写 [编号],编号与输入一致,然后换行写译文。
译文本身可以包含任何标点、引号、换行。条目之间空一行。

示例:
[1]
第一条的译文……

[2]
第二条的译文……

不要输出任何其他内容(没有前言、没有总结)。"""


MARKER = re.compile(r"^\s*\[(\d+)\]\s*$", re.MULTILINE)


def parse_marked(text: str) -> dict[str, str]:
    """Split a [n]-delimited reply into {n: body}.

    Deliberately not JSON: a single unescaped quote in a translated sentence
    used to invalidate the whole batch of ten. With markers, a malformed entry
    costs only itself.
    """
    out, hits = {}, list(MARKER.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[m.end():end].strip()
        if body:
            out[m.group(1)] = body
    return out


def translate_batch(items: list[tuple[str, str]], timeout: int = 900) -> dict[str, str]:
    """items: [(key, english)] -> {key: chinese}"""
    numbered = "\n\n".join(f"[{k}]\n{txt}" for k, txt in items)
    result = ClaudeCodeLLM(model="opus", timeout=timeout, retries=2).complete(SYSTEM, numbered)
    out = parse_marked(result)
    if not out:
        raise RuntimeError(f"no [n] markers in output: {result[:200]}")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    date = args[0] if args else dt.date.today().isoformat()
    path = os.path.join(MASTERS, f"{date}.json")
    if not os.path.exists(path):
        cands = sorted(f for f in os.listdir(MASTERS) if f.endswith(".json"))
        if not cands:
            print("no masters data")
            return 1
        path = os.path.join(MASTERS, cands[-1])

    data = json.load(open(path))
    todo, index = [], {}
    for t in data["tickers"]:
        for s in t["signals"]:
            txt = s.get("reasoning")
            if not txt or s.get("abstained"):
                continue
            if s.get("reasoning_cn") and not force:
                continue
            key = str(len(todo) + 1)
            todo.append((key, txt))
            index[key] = s

    if not todo:
        print("nothing to translate (all cached)")
        return 0

    print(f"translating {len(todo)} reasonings...", flush=True)
    # chunk so a single prompt stays a sane size
    done = 0
    for i in range(0, len(todo), 10):
        chunk = todo[i:i + 10]
        try:
            out = translate_batch(chunk)
        except Exception as exc:
            print(f"  chunk {i//10 + 1} failed: {str(exc)[:120]}", flush=True)
            out = {}
        for k, cn in out.items():
            if k in index and isinstance(cn, str) and cn.strip():
                index[k]["reasoning_cn"] = cn.strip()
                done += 1
        # Anything the batch dropped gets one solo attempt — a batch failure
        # should cost at most a retry, never the whole chunk's translations.
        missing = [(k, t) for k, t in chunk if not index[k].get("reasoning_cn")]
        for k, txt in missing:
            try:
                solo = translate_batch([(k, txt)], timeout=300)
            except Exception:
                continue
            cn = solo.get(k) or next(iter(solo.values()), None)
            if cn and cn.strip():
                index[k]["reasoning_cn"] = cn.strip()
                done += 1
        print(f"  chunk {i//10 + 1}: {len(out)}批量 + {sum(1 for k, _ in missing if index[k].get('reasoning_cn'))} 单条补救",
              flush=True)

    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"✅ {done}/{len(todo)} 已翻译 → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
