#!/usr/bin/env python
"""P5 财报预测 + 历史回放评分。

predict:  用 EarningsSnapshot 让 LLM 预测下一次财报的方向/幅度/期权结构。
score:    对过去 N 次财报做回放 —— 每次只用该次之前的事件构建 schema,
          预测完再与实际结果比对,得出方向命中率。这就是「用历史校准」。

Usage:
  python masters/earnings_predict.py predict --ticker RKLB
  python masters/earnings_predict.py score   --ticker RKLB --n 6
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.claude_code_llm import ClaudeCodeLLM
from integrations.earnings_schema import build_earnings_snapshot
from integrations.moomoo_client import MoomooDataClient

SYSTEM = """你是财报事件交易分析师。给你一份某标的的财报事件档案(含历史财报前 IV、
EPS 实际/预期、财报后 0/1/3/5 日股价反应、IV crush),请预测下一次财报的反应。

要求:
- 只依据档案中的数据推理,不要引入档案外的记忆或新闻。
- 重点看该标的自身的历史模式:EPS 意外与股价反应的相关性有多强?
  财报后是延续还是反转?IV crush 幅度是否稳定?
- 明确区分「当日反应」和「+5日漂移」——它们经常方向相反。

严格只输出 JSON:
{"direction": "bullish|bearish|neutral",
 "expected_move_d0_pct": <float,当日预期涨跌百分数>,
 "expected_move_d5_pct": <float>,
 "confidence": <0-100>,
 "key_pattern": "<你从历史中identified的最重要的一条模式,一句话>",
 "option_note": "<对期权买卖方的一句话提示,考虑 IV crush>"}"""


def ask(llm, snapshot_text: str) -> dict:
    out = llm.complete(SYSTEM, snapshot_text)
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"no JSON: {out[:200]}")
    return json.loads(out[i:j + 1])


def cmd_predict(args, client, llm) -> int:
    as_of = args.as_of or dt.date.today().isoformat()
    snap = build_earnings_snapshot(args.ticker, as_of, client, limit=args.limit)
    if not snap.events:
        print(f"{args.ticker}: 无财报事件数据")
        return 1
    print(snap.render())
    print("\n--- 模型预测 ---")
    p = ask(llm, snap.render())
    print(f"  方向        {p['direction']}  (信心 {p['confidence']})")
    print(f"  当日预期    {p['expected_move_d0_pct']:+.1f}%   +5日 {p['expected_move_d5_pct']:+.1f}%")
    print(f"  关键模式    {p['key_pattern']}")
    print(f"  期权提示    {p['option_note']}")
    return 0


def cmd_score(args, client, llm) -> int:
    """Replay: for each past print, build the schema as it looked the day
    before, predict, then compare with what actually happened."""
    events = client.earnings_events(args.ticker, limit=args.limit + args.n)
    if len(events) < args.n + 3:
        print(f"{args.ticker}: 历史事件不足({len(events)}),无法回放 {args.n} 次")
        return 1

    rows, t0 = [], time.time()
    for ev in events[:args.n]:
        as_of = (dt.date.fromisoformat(ev["filed"]) - dt.timedelta(days=1)).isoformat()
        snap = build_earnings_snapshot(args.ticker, as_of, client, limit=args.limit)
        if len(snap.events) < 3:
            continue
        try:
            p = ask(llm, snap.render())
        except Exception as exc:
            print(f"  {ev['period']}: 预测失败 {str(exc)[:80]}")
            continue
        actual_d0, actual_d5 = ev.get("move_d0"), ev.get("move_d5")
        pred_dir = p["direction"]
        hit_d0 = (actual_d0 is not None and
                  ((pred_dir == "bullish" and actual_d0 > 0) or
                   (pred_dir == "bearish" and actual_d0 < 0)))
        hit_d5 = (actual_d5 is not None and
                  ((pred_dir == "bullish" and actual_d5 > 0) or
                   (pred_dir == "bearish" and actual_d5 < 0)))
        rows.append(dict(period=ev["period"], filed=ev["filed"], pred=pred_dir,
                         conf=p["confidence"],
                         pred_d0=p["expected_move_d0_pct"], actual_d0=actual_d0,
                         pred_d5=p["expected_move_d5_pct"], actual_d5=actual_d5,
                         hit_d0=hit_d0, hit_d5=hit_d5,
                         pattern=p.get("key_pattern")))
        print(f"  {ev['period']:9} 预测 {pred_dir:8} 当日 {p['expected_move_d0_pct']:+5.1f}% "
              f"实际 {actual_d0:+5.1f}%  {'✅' if hit_d0 else '❌'}   "
              f"+5日 预测 {p['expected_move_d5_pct']:+6.1f}% 实际 {actual_d5:+6.1f}%  "
              f"{'✅' if hit_d5 else '❌'}", flush=True)

    if not rows:
        print("无有效回放结果")
        return 1
    directional = [r for r in rows if r["pred"] != "neutral"]
    n_d = len(directional) or 1
    acc0 = 100.0 * sum(1 for r in directional if r["hit_d0"]) / n_d
    acc5 = 100.0 * sum(1 for r in directional if r["hit_d5"]) / n_d
    print(f"\n=== 回放评分 ({len(rows)} 次, {time.time()-t0:.0f}s) ===")
    print(f"  给出方向判断 {len(directional)}/{len(rows)} 次(其余中性)")
    print(f"  当日方向命中率  {acc0:.0f}%   (随机基准 50%)")
    print(f"  +5日方向命中率  {acc5:.0f}%")
    print("  ⚠️  样本极小,命中率仅供参考,不足以证明有效性" if len(directional) < 10 else "")

    out_dir = os.path.join(ROOT, "site-data", "earnings")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"score_{args.ticker}.json")
    with open(path, "w") as f:
        json.dump({"ticker": args.ticker, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "n": len(rows), "directional": len(directional),
                   "acc_d0_pct": round(acc0, 1), "acc_d5_pct": round(acc5, 1),
                   "rows": rows}, f, ensure_ascii=False, indent=1)
    print(f"\n→ {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["predict", "score"])
    ap.add_argument("--ticker", default="RKLB")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--limit", type=int, default=10, help="schema 里保留的历史事件数")
    ap.add_argument("--n", type=int, default=6, help="回放次数")
    ap.add_argument("--model", default="opus")
    args = ap.parse_args()

    client = MoomooDataClient()
    llm = ClaudeCodeLLM(model=args.model)
    try:
        return cmd_predict(args, client, llm) if args.cmd == "predict" \
            else cmd_score(args, client, llm)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
