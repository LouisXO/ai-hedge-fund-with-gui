#!/usr/bin/env python
"""P2 大师信号管线 — 5 位 persona 对 (持仓 + 当日机会) 各出一个 Signal。

数据: moomoo OpenD (MoomooDataClient)。LLM: 本机 Claude Code 订阅 ($0 API)。
只读:不下单、不改持仓。

Usage:
  python masters/run_masters.py [--date YYYY-MM-DD] [--tickers AAPL,MSFT]
                               [--workers 4] [--personas buffett,munger]
Output: site-data/masters/<date>.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.claude_code_llm import ClaudeCodeLLM
from integrations.moomoo_client import MoomooDataClient

from hedge_fund.signals.buffett import BuffettAgent
from hedge_fund.signals.druckenmiller import DruckenmillerAgent
from hedge_fund.signals.graham import GrahamAgent
from hedge_fund.signals.lynch import LynchAgent
from hedge_fund.signals.munger import MungerAgent

PERSONAS = {
    "buffett": BuffettAgent,
    "munger": MungerAgent,
    "graham": GrahamAgent,
    "lynch": LynchAgent,
    "druckenmiller": DruckenmillerAgent,
}

OPTRADAR_OUT = "/Users/louis/optradar/out"


def tickers_from_optradar(date: str) -> tuple[list[str], dict[str, str]]:
    """(tickers, role map) from the day's radar: positions + idea shortlist."""
    path = os.path.join(OPTRADAR_OUT, f"{date}.json")
    if not os.path.exists(path):  # fall back to the most recent report
        cands = sorted(f for f in os.listdir(OPTRADAR_OUT) if f.endswith(".json"))
        if not cands:
            return [], {}
        path = os.path.join(OPTRADAR_OUT, cands[-1])
    d = json.load(open(path))
    roles: dict[str, str] = {}
    for s in d.get("pos_stocks", []):
        roles[_bare(s["code"])] = "position"
    for o in d.get("pos_options", []):
        roles.setdefault(_bare(o["underlying"]), "position")
    for i in d.get("ideas", []):
        roles.setdefault(_bare(i["ticker"]), "idea")
    return list(roles), roles


def _bare(code: str) -> str:
    return code.split(".")[-1].upper()


def run_one(persona: str, ticker: str, date: str, model: str) -> dict:
    """One (persona, ticker) prediction. Own client per thread — moomoo ctx is not
    thread-safe, and each Claude Code call is its own process anyway."""
    t0 = time.time()
    data = MoomooDataClient()
    try:
        agent = PERSONAS[persona](llm=ClaudeCodeLLM(model=model))
        sig = agent.predict(ticker, date, data)
        meta = sig.metadata or {}
        return {
            "persona": persona, "ticker": ticker,
            "value": float(sig.value),
            "stance": meta.get("signal") or _stance(sig.value),
            "confidence": meta.get("confidence"),
            "abstained": bool(meta.get("abstained")),
            "reasoning": sig.reasoning,
            "cached": bool(meta.get("cached")),
            "secs": round(time.time() - t0, 1),
        }
    except Exception as exc:  # one failure must not sink the batch
        return {"persona": persona, "ticker": ticker, "value": 0.0,
                "stance": "error", "abstained": True, "error": str(exc)[:200],
                "reasoning": None, "secs": round(time.time() - t0, 1)}
    finally:
        data.close()


def _stance(v: float) -> str:
    return "bullish" if v > 0.15 else "bearish" if v < -0.15 else "neutral"


def summarize(rows: list[dict], roles: dict[str, str]) -> list[dict]:
    """Per-ticker consensus + disagreement (the headline the brief needs)."""
    out = []
    for t in sorted({r["ticker"] for r in rows}):
        votes = [r for r in rows if r["ticker"] == t and not r["abstained"]]
        vals = [r["value"] for r in votes]
        bulls = sum(1 for r in votes if r["stance"] == "bullish")
        bears = sum(1 for r in votes if r["stance"] == "bearish")
        out.append({
            "ticker": t,
            "role": roles.get(t, "idea"),
            "consensus": round(statistics.mean(vals), 3) if vals else None,
            # spread of conviction = how much the desk disagrees
            "disagreement": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
            "bulls": bulls, "bears": bears,
            "neutrals": len(votes) - bulls - bears,
            "voted": len(votes),
            "abstained": sum(1 for r in rows if r["ticker"] == t and r["abstained"]),
            "signals": sorted(
                [r for r in rows if r["ticker"] == t],
                key=lambda r: -abs(r["value"]),
            ),
        })
    # most contested first — that's what's worth reading
    out.sort(key=lambda x: -(x["disagreement"] or 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--tickers", default=None, help="override, comma separated")
    ap.add_argument("--personas", default=",".join(PERSONAS))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default="opus")
    args = ap.parse_args()

    if args.tickers:
        tickers = [_bare(t) for t in args.tickers.split(",") if t.strip()]
        roles = {t: "manual" for t in tickers}
    else:
        tickers, roles = tickers_from_optradar(args.date)
    personas = [p.strip() for p in args.personas.split(",") if p.strip() in PERSONAS]
    if not tickers:
        print("no tickers — is optradar output present?")
        return 1

    jobs = [(p, t) for t in tickers for p in personas]
    print(f"{len(personas)} personas x {len(tickers)} tickers = {len(jobs)} calls "
          f"(workers={args.workers}, model={args.model})", flush=True)

    rows, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, p, t, args.date, args.model): (p, t) for p, t in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            rows.append(r)
            flag = "×" if r["abstained"] else r["stance"][:4]
            print(f"  [{i}/{len(jobs)}] {r['persona']:<14} {r['ticker']:<5} "
                  f"{flag:<8} {r['value']:+.2f} ({r['secs']}s)", flush=True)

    summary = summarize(rows, roles)
    payload = {
        "date": args.date,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "personas": personas,
        "elapsed_secs": round(time.time() - t0, 1),
        "tickers": summary,
    }
    os.makedirs(os.path.join(ROOT, "site-data/masters"), exist_ok=True)
    path = os.path.join(ROOT, "site-data/masters", f"{args.date}.json")
    with open(path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"\n=== 共识与分歧 ({time.time()-t0:.0f}s) ===")
    for s in summary:
        print(f"{s['ticker']:<5} [{s['role']:<8}] consensus {str(s['consensus']):>6} "
              f"| 分歧 {s['disagreement']:.2f} | {s['bulls']}多/{s['bears']}空/{s['neutrals']}中")
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
