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
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTERS = os.path.join(ROOT, "site-data", "masters")
CLAUDE = os.path.expanduser("~/.claude/local/claude")

SYSTEM = """你是金融翻译。把投资大师的英文分析逐条翻成中文。

要求:
- 保留原有的语气和个人风格(Buffett 的家常比喻、Munger 的尖刻、Graham 的严谨、
  Lynch 的通俗、Druckenmiller 的果断),不要翻成中性报告腔。
- 金融术语用中文惯用译法:gross margin→毛利率,operating margin→营业利润率,
  ROE→净资产收益率,book value→账面价值,margin of safety→安全边际,
  free cash flow→自由现金流,multiple→倍数,conviction→信心。
- 数字、百分比、货币金额、股票代码原样保留。
- 不要增删内容,不要加解释。

严格只输出 JSON:{"1": "译文", "2": "译文", ...},键对应输入编号。"""


def translate_batch(items: list[tuple[str, str]], timeout: int = 900) -> dict[str, str]:
    """items: [(key, english)] -> {key: chinese}"""
    numbered = "\n\n".join(f"[{k}]\n{txt}" for k, txt in items)
    proc = subprocess.run(
        [CLAUDE, "-p", numbered, "--append-system-prompt", SYSTEM,
         "--model", "opus", "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr[:300]}")
    result = json.loads(proc.stdout)["result"]
    start, end = result.find("{"), result.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"no JSON in translation output: {result[:200]}")
    return json.loads(result[start:end + 1])


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
            continue
        for k, cn in out.items():
            if k in index and isinstance(cn, str) and cn.strip():
                index[k]["reasoning_cn"] = cn.strip()
                done += 1
        print(f"  chunk {i//10 + 1}: {len(out)} translated", flush=True)

    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"✅ {done}/{len(todo)} 已翻译 → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
