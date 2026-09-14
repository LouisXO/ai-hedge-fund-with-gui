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


def score_one(ticker, args, client, llm) -> list:
    """Replay one ticker: for each past print, build the schema as it looked
    the day before, predict, then compare with what actually happened."""
    events = client.earnings_events(ticker, limit=args.limit + args.n)
    if len(events) < args.n + 3:
        print(f"  {ticker}: 历史事件不足({len(events)}),跳过")
        return []

    rows = []
    for ev in events[:args.n]:
        as_of = (dt.date.fromisoformat(ev["filed"]) - dt.timedelta(days=1)).isoformat()
        snap = build_earnings_snapshot(ticker, as_of, client, limit=args.limit)
        if len(snap.events) < 3:
            continue
        try:
            p = ask(llm, snap.render())
        except Exception as exc:
            print(f"  {ticker} {ev['period']}: 预测失败 {str(exc)[:80]}", flush=True)
            continue
        actual_d0, actual_d5 = ev.get("move_d0"), ev.get("move_d5")
        pred_dir = p["direction"]
        hit_d0 = (actual_d0 is not None and
                  ((pred_dir == "bullish" and actual_d0 > 0) or
                   (pred_dir == "bearish" and actual_d0 < 0)))
        hit_d5 = (actual_d5 is not None and
                  ((pred_dir == "bullish" and actual_d5 > 0) or
                   (pred_dir == "bearish" and actual_d5 < 0)))
        rows.append(dict(ticker=ticker, period=ev["period"], filed=ev["filed"], pred=pred_dir,
                         conf=p["confidence"],
                         pred_d0=p["expected_move_d0_pct"], actual_d0=actual_d0,
                         pred_d5=p["expected_move_d5_pct"], actual_d5=actual_d5,
                         hit_d0=hit_d0, hit_d5=hit_d5,
                         pattern=p.get("key_pattern")))
        print(f"  {ticker:5} {ev['period']:9} 预测 {pred_dir:8} 当日 {p['expected_move_d0_pct']:+5.1f}% "
              f"实际 {actual_d0:+5.1f}%  {'✅' if hit_d0 else '❌'}   "
              f"+5日 预测 {p['expected_move_d5_pct']:+6.1f}% 实际 {actual_d5:+6.1f}%  "
              f"{'✅' if hit_d5 else '❌'}", flush=True)

    return rows


def cmd_score(args, client, llm) -> int:
    tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
    all_rows, t0 = [], time.time()
    for t in tickers:
        all_rows.extend(score_one(t, args, client, llm))

    if not all_rows:
        print("无有效回放结果")
        return 1

    def summarize(rows):
        directional = [r for r in rows if r["pred"] != "neutral"]
        n = len(directional)
        if not n:
            return None
        return dict(n_total=len(rows), n_dir=n,
                    acc_d0=round(100.0 * sum(1 for r in directional if r["hit_d0"]) / n, 1),
                    acc_d5=round(100.0 * sum(1 for r in directional if r["hit_d5"]) / n, 1))

    print(f"\n=== 大样本回放评分 ({len(all_rows)} 次, {time.time()-t0:.0f}s) ===")
    print(f"{'标的':<7}{'样本':>5}{'方向判断':>9}{'当日命中':>9}{'+5日命中':>10}")
    for t in tickers:
        sub = [r for r in all_rows if r["ticker"] == t]
        st = summarize(sub)
        if st:
            print(f"{t:<7}{st['n_total']:>5}{st['n_dir']:>9}{st['acc_d0']:>8.0f}%{st['acc_d5']:>9.0f}%")
        elif sub:
            print(f"{t:<7}{len(sub):>5}{0:>9}{'-':>9}{'-':>10}  (全部中性)")

    tot = summarize(all_rows)
    print("-" * 42)
    print(f"{'合计':<7}{tot['n_total']:>5}{tot['n_dir']:>9}{tot['acc_d0']:>8.0f}%{tot['acc_d5']:>9.0f}%")
    print(f"\n随机基准 50%。给出方向判断 {tot['n_dir']}/{tot['n_total']} 次,其余中性。")
    if tot["n_dir"] < 20:
        print("⚠️  方向样本 <20,命中率仍属噪声,不足以判定有效性。")

    out_dir = os.path.join(ROOT, "site-data", "earnings")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "score_batch.json")
    with open(path, "w") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "tickers": tickers, "summary": tot, "rows": all_rows},
                  f, ensure_ascii=False, indent=1)
    print(f"\n→ {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["predict", "score"])
    ap.add_argument("--ticker", default="RKLB", help="单票或逗号分隔多票")
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
