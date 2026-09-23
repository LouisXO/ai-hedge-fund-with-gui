"""The multiple-testing ledger: every book-level variant ever backtested, with Holm.

Until now the "Holm budget" lived in docs/AGENT_PLAN.md §9 as a hand count.
This collects every variant from the report JSONs in site-data/validation
(anything with a `books` dict and an alpha2 Newey-West t), de-duplicates
the repeated controls, and reports the family-wise picture:

  p_raw   two-sided from the NW t (normal approximation)
  p_holm  Holm step-down over the whole family
  pass    p_holm <= 0.05 / 0.10

Two-sided is deliberately conservative: several variants were
pre-registered with a sign, but the family also contains post-hoc reads,
and one rule for all is easier to defend than a per-variant argument.

Signal-level tests before the books existed (S3's three price signals,
S7's stock versions, S8's insider event study) were separate families and
all failed their own gates; they are listed in §9 and not repeated here.

Usage: python -m hedge_fund.validation.family_log [--write]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

REPORTS = "/Users/louis/hedge-fund/site-data/validation"
CONTROLS = {"insider_5d", "short_insider_5d"}          # the same v1 insider book re-run as a control in every report
ALIASES = {"long_composite_daily_n30": "base",         # S19's daily composite is S24's base
           "insider_buy_h5": "insider_v1_5d"}           # the v1 line under the event-line interface (S23 control)


def _p_two_sided(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def collect() -> list[dict]:
    rows, seen = [], set()
    for f in sorted(glob.glob(os.path.join(REPORTS, "*.json"))):
        if "_fund_v1" in f:                                   # archived pre-audit numbers (S28), kept for the record only
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        books = d.get("books")
        if not isinstance(books, dict):
            continue
        for name, m in books.items():
            t = m.get("alpha2_t_nw") if isinstance(m, dict) else None
            if t is None or (isinstance(t, float) and math.isnan(t)) or not m.get("n_trades"):
                continue                                   # factor-only cells never traded: not a tested variant
            key = ALIASES.get(name, name)
            if key in CONTROLS:
                key = "insider_v1_5d"
            row = {"variant": key, "report": os.path.basename(f), "t": float(t),
                   "alpha2_ann_pct": m.get("alpha2_ann_pct"), "cagr_pct": m.get("cagr_pct"), "n_trades": m.get("n_trades")}
            if key in seen:                                   # same variant re-run on newer data: the later report wins
                rows[[r["variant"] for r in rows].index(key)] = row
                continue
            seen.add(key)
            rows.append(row)
    return rows


def holm(rows: list[dict]) -> list[dict]:
    n = len(rows)
    order = sorted(rows, key=lambda r: _p_two_sided(r["t"]))
    running = 0.0
    for i, r in enumerate(order):
        p = _p_two_sided(r["t"])
        adj = min(1.0, (n - i) * p)
        running = max(running, adj)
        r["p_raw"], r["p_holm"] = p, running
    return sorted(rows, key=lambda r: -abs(r["t"]))


def render(rows: list[dict]) -> str:
    L = [f"# Family-wise ledger — {len(rows)} book-level variants", "",
         "Two-sided p from the Newey-West t; Holm step-down over the whole family. "
         "Variants with a negative t are the rejected event lines (the sign is informative, the test is symmetric).", "",
         "| variant | report | t | alpha2/yr | p raw | p Holm | ≤0.05 | ≤0.10 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        a = f"{r['alpha2_ann_pct']:+.1f}%" if r.get("alpha2_ann_pct") is not None else "—"
        L.append(f"| {r['variant']} | {r['report']} | {r['t']:.2f} | {a} | {r['p_raw']:.3f} | {r['p_holm']:.3f} | "
                 f"{'✓' if r['p_holm'] <= 0.05 else ''} | {'✓' if r['p_holm'] <= 0.10 else ''} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write family_log.jsonl and family_log.md next to the reports")
    args = ap.parse_args()
    rows = holm(collect())
    text = render(rows)
    print(text)
    if args.write:
        with open(os.path.join(REPORTS, "family_log.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(REPORTS, "family_log.md"), "w") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
