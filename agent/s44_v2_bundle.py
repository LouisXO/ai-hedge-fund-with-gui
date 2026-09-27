"""S44 — backtest of the long book's v2 rule bundle (definitions and adoption rule:
agent/books/long_v2_bundle.py, pre-registered 2026-09-26, commit 216ca92).

Same window and engine settings as S33 / S40: 2017-01-01 → 2026-08-31, half the quoted spread
per side, next-open fills, idle cash in SPY. Lines: base, floor2, jump5, cap20, clusters, bundle.
Also writes the base and bundle daily NAV (s44_nav.csv) — the drift monitor's reference
distribution.

Usage: python -m agent.s44_v2_bundle
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import pandas as pd

from agent.books import long_v2_bundle as v2
from agent.books.data import fundamentals, load_market
from agent.books.engine import Result, metrics, simulate
from agent.books.industry import industry_by_ticker
from agent.books.long_term import TOP_N
from agent.s33_negative_filters import veto_sizes
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2017-01-01", "2026-08-31"
CAPITAL = 100_000.0


def combine(a: Result, b: Result) -> Result:
    nav = a.nav + b.nav
    res = Result(nav, a.spy_nav + b.spy_nav, a.trades + b.trades, (a.exposure * a.nav + b.exposure * b.nav) / nav,
                 (a.turnover + b.turnover) / 2, size_factor=a.size_factor)
    res.metrics = metrics(res, max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)) if len(nav) >= 30 else {}
    return res


def entry_zone(lists: dict, n: int, base: dict | None = None) -> dict:
    """Engine sizes that make rank > n keep-only (size 0 = keep if held, never enter) — the live rule
    (agent.execute enters gate_passed names only). Without it the engine fills a free slot from anywhere
    in the top 2N, which is what every backtest before S44 did."""
    out = {d: dict(v) for d, v in (base or {}).items()}
    for d, names in lists.items():
        z = out.setdefault(d, {})
        for t in names[n:]:
            z[t] = 0.0
    return out


def run_lines(market, lists: dict, cl_lists: dict, groups: pd.Series, start: str, end: str, capital: float,
              exec_frac: float = 0.5, cash_in_spy: bool = True, fixed_cost_pct: float | None = None, strict: bool = True) -> dict[str, Result]:
    """lists[day] = v1 top 60; cl_lists[day] = {'M': [...], 'V': [...]}. Used by the backtest and by the shadow replay.
    strict=True: entries only from the top N (the live rule and the pre-registered definition)."""
    kw = dict(hold_days=None, exec_frac=exec_frac, cash_in_spy=cash_in_spy, fixed_cost_pct=fixed_cost_pct)
    zone = (lambda tg, n, sz=None: entry_zone(tg, n, sz)) if strict else (lambda tg, n, sz=None: sz)
    out = {"v1c": simulate(market, lists, start, end, TOP_N, capital=capital, sizes=zone(lists, TOP_N), **kw)}
    for name, (fl, jp) in {"floor2": (True, False), "jump5": (False, True)}.items():
        sz, _ = veto_sizes(lists, v2.veto_frame(market, fl, jp))
        out[name] = simulate(market, lists, start, end, TOP_N, capital=capital, sizes=zone(lists, TOP_N, sz), **kw)
    out["cap20"] = simulate(market, lists, start, end, TOP_N, capital=capital, sizes=zone(lists, TOP_N), entry_ok=v2.cap_rule(groups, TOP_N), **kw)
    both = v2.veto_frame(market, True, True)
    for name, vetoed in (("clusters", False), ("bundle", True)):
        halves = []
        for c in ("M", "V"):
            tg = {d: x[c] for d, x in cl_lists.items()}
            sz = veto_sizes(tg, both)[0] if vetoed else None
            halves.append(simulate(market, tg, start, end, v2.HALF_N, capital=capital / 2, sizes=zone(tg, v2.HALF_N, sz),
                                   entry_ok=v2.cap_rule(groups, v2.HALF_N) if vetoed else None, **kw))
        out[name] = combine(*halves)
    return out


def main() -> int:
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, START)
        fund = fundamentals(store)
        groups = industry_by_ticker(store)
    lists, cl_lists, share_m = {}, {}, []
    for d in market.adj.loc[START:END].index:
        fs = v2.scored(market, fund, d)
        lists[d] = fs.index[:v2.KEEP_MULT * TOP_N].tolist()
        cl_lists[d] = v2.cluster_lists(fs)
        share_m.append((v2.cluster_of(fs.head(TOP_N)) == "M").mean())
    print(f"scored {len(lists)} days; v1 top-30 is {pd.Series(share_m).mean():.0%} M-cluster on average [{time.time() - t0:.0f}s]", flush=True)
    stamp = dt.date.today().isoformat()
    path = os.path.join(OUT_DIR, f"s44_v2_bundle_{stamp}")
    report, L = {}, [f"# S44 — long book v2 rule bundle ({stamp})", "", f"{START} → {END}, half spread per side, next-open fills, idle cash in SPY. "
                     "Definitions pre-registered in agent/books/long_v2_bundle.py. Rules change entries only.", "",
                     "Two entry conventions. **strict** = a free slot is filled only from the top N of the list (the live rule in agent.execute and the "
                     "pre-registered definition) — the primary reading. **loose** = the engine fills a free slot from anywhere in the top 2N, which is "
                     "what S24–S40 ran; shown for comparability with those numbers.", ""]
    for mode, strict in (("strict", True), ("loose", False)):
        res = run_lines(market, lists, cl_lists, groups, START, END, CAPITAL, strict=strict)
        books = {("base" if k == "v1c" else k): r.metrics for k, r in res.items()}
        base = books["base"]
        verdict = {k: {"d_alpha2": m["alpha2_ann_pct"] - base["alpha2_ann_pct"],
                       "passes_a": bool(m["alpha2_ann_pct"] - base["alpha2_ann_pct"] >= 0.5 and m["alpha2_t_nw"] >= base["alpha2_t_nw"])}
                   for k, m in books.items() if k != "base"}
        report[mode] = {"books": books, "verdict": verdict}
        if strict:
            pd.DataFrame({k: r.nav for k, r in res.items()}).to_csv(os.path.join(OUT_DIR, "s44_nav.csv"))
        L += [f"## {mode}", "", "| line | CAGR | alpha2/yr | alpha2 t | Δ alpha2 | β mkt | β size | vol | MaxDD | trades | hit | turnover | exposure | criterion (a) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, m in books.items():
            v = verdict.get(k, {})
            L.append(f"| {k} | {m['cagr_pct']:+.1f}% | {m['alpha2_ann_pct']:+.1f}% | {m['alpha2_t_nw']:.2f} | {v.get('d_alpha2', 0):+.1f} | {m['beta_mkt']:.2f} | {m['beta_size']:.2f} | "
                     f"{m['vol_pct']:.0f}% | {m['max_drawdown_pct']:.0f}% | {m['n_trades']} | {m['trade_hit_rate']:.0%} | {m['turnover_ann']:.1f} | {m['avg_exposure']:.0%} | "
                     f"{'' if k == 'base' else ('pass' if v['passes_a'] else 'no')} |")
        L += ["", "By year (total return)", "", "| line | " + " | ".join(str(y) for y in base["by_year"]) + " |", "|---|" + "---|" * len(base["by_year"])]
        for k, m in books.items():
            L.append(f"| {k} | " + " | ".join(f"{m['by_year'].get(y, float('nan')) * 100:+.0f}%" for y in base["by_year"]) + " |")
        L.append("")
        print(f"{mode} done [{time.time() - t0:.0f}s]", flush=True)
    with open(path + ".json", "w") as f:
        s_, l_ = report["strict"]["books"], report["loose"]["books"]
        ledger = {"base": s_["base"], "long_floor2": s_["floor2"], "long_jump5_strict": s_["jump5"], "v2_cap20": s_["cap20"], "v2_cap20_loose": l_["cap20"],
                  "v2_clusters": s_["clusters"], "v2_bundle": s_["bundle"], "v2_bundle_loose": l_["bundle"]}      # top-level `books` = what family_log counts
        json.dump({"start": START, "end": END, "books": ledger, **report, "v1_top30_share_M": float(pd.Series(share_m).mean())}, f, indent=1, default=float)
    text = "\n".join(L) + "\n"
    with open(path + ".md", "w") as f:
        f.write(text)
    print(text)
    print(f"[{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
