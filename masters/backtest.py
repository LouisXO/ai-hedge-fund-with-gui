#!/usr/bin/env python
"""P4 回测实验室 — 用 moomoo 的 point-in-time 数据回放一个基金。

同一条 run_cycle 在历史上循环(和实盘同一代码路径),对标基准出净值曲线。
默认单票、单 persona、周频:先验证管线,再扩规模。

Usage:
  python masters/backtest.py --tickers RKLB --personas buffett \
      --start 2025-06-01 --end 2026-09-13 [--cadence weekly] [--model opus]
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
from integrations.moomoo_client import MoomooDataClient

from hedge_fund.backtesting.fund import backtest_fund
from hedge_fund.fund.spec import Fund, FundSpec, ModelSpec, RiskLimits, StrategySpec
from masters.run_masters import PERSONAS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="RKLB")
    ap.add_argument("--personas", default="buffett")
    ap.add_argument("--start", default="2025-06-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--cadence", default="weekly",
                    choices=["daily", "weekly", "monthly"])
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--model", default="opus")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    names = [p.strip() for p in args.personas.split(",") if p.strip() in PERSONAS]
    if not names:
        print("no valid personas")
        return 1

    spec = FundSpec(
        name="moomoo-backtest",
        strategies=[StrategySpec(
            name="desk", weight=1.0,
            models=[ModelSpec(name=n) for n in names])],
        risk=RiskLimits(max_position_pct=0.25, max_gross_exposure=1.0),
        capital=args.capital,
        rebalance=args.cadence,
        benchmark=args.benchmark,
    )
    llm = ClaudeCodeLLM(model=args.model)
    def build(n):
        cls_ = PERSONAS[n]
        return cls_() if n == "pead" else cls_(llm=llm)
    fund = Fund(spec, models={"desk": [build(n) for n in names]})

    data = MoomooDataClient()
    t0 = time.time()
    print(f"backtest {tickers} | {names} | {args.start} → {args.end} "
          f"| {args.cadence} | benchmark {args.benchmark}", flush=True)

    def progress(i, n, record):
        held = len(getattr(record, "target_weights", {}) or {})
        print(f"  [{i}/{n}] {record.as_of}  持仓 {held}  ({time.time()-t0:.0f}s)",
              flush=True)

    try:
        result = backtest_fund(fund, args.start, args.end, data,
                               universe=tickers, on_cycle=progress)
    finally:
        data.close()

    m = result.metrics
    print(f"\n=== 回测结果 ({time.time()-t0:.0f}s) ===")
    print(f"  区间        {result.start} → {result.end}  ({m.n_cycles} 个调仓周期, "
          f"{m.n_orders} 笔委托)")
    print(f"  基金收益    {m.total_return_pct:+.2%}   (年化 {m.annualized_return_pct:+.2%})")
    print(f"  基准 {args.benchmark:<6} {m.benchmark_return_pct:+.2%}")
    print(f"  超额        {m.excess_return_pct:+.2%}")
    print(f"  最大回撤    {m.max_drawdown_pct:.2%}")
    print(f"  夏普        {m.sharpe_ratio:.2f}")
    if m.n_cycles < 12:
        print("  ⚠️  周期数过少,以上指标(尤其年化/夏普)不具统计意义")

    out_dir = os.path.join(ROOT, "site-data", "backtests")
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{'-'.join(tickers)}_{'-'.join(names)}_{args.start}_{args.end}"
    path = os.path.join(out_dir, f"{tag}.json")
    with open(path, "w") as f:
        json.dump(json.loads(result.model_dump_json()), f, ensure_ascii=False, indent=1)
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
