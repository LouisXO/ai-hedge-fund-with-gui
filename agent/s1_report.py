"""S1 report: panel coverage, look-ahead universe bias, and tool self-checks.

Answers three questions before any new signal is built (docs/AGENT_PLAN.md
v2, step S1):
1. How much of the point-in-time S&P 500 does the free panel actually cover?
2. How much does selecting on today's members inflate a backtest? Measured
   as momentum IC on members-at-t versus the frozen 2026 member list.
3. Do the evaluation tools find a small planted edge and reject noise on
   the real return panel (not just synthetic data)?

Usage: python -m agent.s1_report [--start 2015-01-01] [--boots 2000]
Writes site-data/validation/s1_<date>.json and .md (no positions, public-safe).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from hedge_fund.features.factors import mom_12_1, reversal_5
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t
from hedge_fund.validation.tearsheet import (forward_returns, noise_signal, planted_signal, rank_ic, summarize_ic,
                                             tearsheet)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "site-data", "validation")


def load(store: PanelStore, start: str):
    lookback_start = (pd.Timestamp(start) - pd.Timedelta(days=420)).date().isoformat()
    close = store.bars_wide("close", start=lookback_start)
    adj = store.bars_wide("adj_close", start=lookback_start)
    opn = store.bars_wide("open", start=lookback_start)
    adj_open = opn * (adj / close)
    members = sorted({t for _, m in store.membership_changes() for t in m})
    cols = sorted(set(members) | set(adj.columns))
    adj, adj_open = adj.reindex(columns=cols), adj_open.reindex(columns=cols)
    mask_pit = store.membership_mask(adj.index, cols)
    last_members = store.membership_changes()[-1][1]
    mask_frozen = pd.DataFrame(np.repeat(np.array([[c in last_members for c in cols]]), len(adj.index), axis=0),
                               index=adj.index, columns=cols)
    return adj, adj_open, mask_pit, mask_frozen


def coverage(adj: pd.DataFrame, mask: pd.DataFrame, start: str) -> dict:
    have = adj.notna()
    m = mask.loc[start:]
    cov = (m & have.loc[start:]).sum(axis=1) / m.sum(axis=1)
    return {int(y): round(float(v), 3) for y, v in cov.groupby(cov.index.year).mean().items()}


def _brief(ts: dict) -> dict:
    """Headline numbers per horizon, for the markdown table."""
    out = {}
    for h, r in ts["horizons"].items():
        ic, tl = r["ic"], r["tails"]
        out[h] = {"ic": ic["mean"], "t_nw": ic["t_nw"], "ci": ic["boot_ci95"], "icir_w": ic["icir_nonoverlap"],
                  "yrs_pos": ic["share_years_positive"], "top5": tl["top_mean"],
                  "bot5": tl["bottom_mean"], "all": tl["all_mean"], "q_spread": r["quantiles"]["spread_raw"]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--boots", type=int, default=2000)
    args = ap.parse_args()

    with PanelStore(read_only=True) as store:
        adj, adj_open, mask_pit, mask_frozen = load(store, args.start)
    end = adj.index[-11]  # leave room for the 10-day label
    sl = slice(args.start, end)
    horizons = (1, 5, 10)
    report = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": args.start,
              "end": str(end.date()), "entry": "open of t+1", "horizons": list(horizons),
              "source": "yfinance daily bars; S&P 500 point-in-time membership (fja05680/sp500)"}

    report["coverage_pit_by_year"] = coverage(adj, mask_pit, args.start)
    report["coverage_frozen_by_year"] = coverage(adj, mask_frozen, args.start)

    mom = mom_12_1(adj)
    rev = reversal_5(adj)
    kw = dict(horizons=horizons, n_boot=args.boots)
    print("mom_12_1 pit ...", flush=True)
    report["mom_12_1_pit"] = tearsheet(mom.loc[sl], adj, adj_open, mask_pit, **kw)
    print("mom_12_1 frozen ...", flush=True)
    report["mom_12_1_frozen"] = tearsheet(mom.loc[sl], adj, adj_open, mask_frozen, **kw)
    print("reversal_5 pit ...", flush=True)
    report["reversal_5_pit"] = tearsheet(rev.loc[sl], adj, adj_open, mask_pit, **kw)

    # tool self-check on the real panel: a planted IC of ~0.02 must be found, noise must not
    fwd5 = forward_returns(adj, adj_open, 5).where(mask_pit).loc[sl]
    planted = summarize_ic(rank_ic(planted_signal(fwd5, 0.02, seed=5), fwd5), 5, n_boot=args.boots)
    noise_t = [newey_west_t(rank_ic(noise_signal(fwd5, seed=100 + i), fwd5).to_numpy(), 5) for i in range(20)]
    checks = {"planted_0.02": {"ic": planted["mean"], "t_nw": planted["t_nw"], "ci": planted["boot_ci95"]},
              "noise_20_seeds": {"t_nw": [round(t, 2) for t in noise_t],
                                 "false_positive_rate": float(np.mean(np.abs(noise_t) > 1.96))}}
    report["self_check"] = checks

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    jpath = os.path.join(OUT_DIR, f"s1_{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(report, f, indent=1, default=float)

    lines = [f"# S1 report — {stamp}", "",
             f"Window {args.start} → {report['end']}, entry at next open, horizons {horizons} trading days.", "",
             "## Coverage (share of index members with bars)", "",
             "| year | members-at-t | frozen 2026 list |", "|---|---|---|"]
    for y in report["coverage_pit_by_year"]:
        lines.append(f"| {y} | {report['coverage_pit_by_year'][y]:.1%} | {report['coverage_frozen_by_year'][y]:.1%} |")
    lines += ["", "## Signals", "",
              "| signal / universe | h | mean IC | NW t | boot 95% CI | weekly ICIR | yrs>0 | top-5 raw | bottom-5 raw | all | Q5−Q1 raw |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in ("mom_12_1_pit", "mom_12_1_frozen", "reversal_5_pit"):
        for h, b in _brief(report[key]).items():
            lines.append(f"| {key} | {h} | {b['ic']:+.4f} | {b['t_nw']:+.2f} | [{b['ci'][0]:+.4f}, {b['ci'][1]:+.4f}] | "
                         f"{b['icir_w']:+.3f} | {b['yrs_pos']:.0%} | {b['top5']:+.3%} | "
                         f"{b['bot5']:+.3%} | {b['all']:+.3%} | {b['q_spread']:+.3%} |")
    pl, nz = checks["planted_0.02"], checks["noise_20_seeds"]
    lines += ["", "## Tool self-check on the real panel (h=5)", "",
              f"- planted IC 0.02: found IC {pl['ic']:+.4f}, NW t {pl['t_nw']:+.2f}, "
              f"CI [{pl['ci'][0]:+.4f}, {pl['ci'][1]:+.4f}]",
              f"- pure noise, 20 seeds: |NW t| > 1.96 in {nz['false_positive_rate']:.0%} (expected ~5%)"]
    with open(jpath.replace(".json", ".md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n→ {jpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
