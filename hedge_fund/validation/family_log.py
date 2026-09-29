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
Holm is a report, not an adoption gate (§5.5, thresholds v3).

S48 (2026-09-29, audit research #1 and #5):
  - one book, one row: rows with the same n_trades and a t within 1e-3 are the same backtest
    re-run under another name (base / long_base / long_half_spread ...); the first is kept and
    the others are listed in `same_as`;
  - S30's momentum books (report key `full`, not `books`) are counted;
  - S24's six variants that were not re-run after the data audit (S28/S29) are flagged
    `pre_audit` ("修正前数据"): their numbers are on the old data;
  - the base book's PSR(0) and deflated Sharpe (Bailey & López de Prado 2014) for 5 / 13 /
    all distinct trials, and the expected maximum t of N independent null trials. The trials
    are not independent (five of the thirteen are one composite book), so only a range is
    honest, and each DSR assumes sd(t) = 1 across trials.

Signal-level tests before the books existed (S3's three price signals,
S7's stock versions, S8's insider event study) were separate families and
all failed their own gates; they are listed in §9 and not repeated here.

Usage: python -m hedge_fund.validation.family_log [--write] [--reports DIR] [--nav CSV] [--panel DB]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from statistics import NormalDist

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(ROOT, "site-data", "validation")
BASE_NAV = "s47_base_nav.csv"                           # the long book v1 restated on the corrected data (S47)
CONTROLS = {"insider_5d", "short_insider_5d"}          # the same v1 insider book re-run as a control in every report
ALIASES = {"long_composite_daily_n30": "base",         # S19's daily composite is S24's base
           "insider_buy_h5": "insider_v1_5d"}           # the v1 line under the event-line interface (S23 control)
SECTIONS = {"s30_momentum_book_": "full"}              # reports whose books sit under another key than `books`
# Keyed by the exact report file: a later S24 re-run on the corrected data (s24_long_v2_<newer date>.json)
# replaces these rows through the "later report wins" rule and must not inherit the flag.
PRE_AUDIT = {"s24_long_v2_2026-09-22.json": {"n50", "invvol", "stop20", "issuance", "secneutral", "combo"}}
SAME_T_TOL = 1e-3
EULER_GAMMA = 0.5772156649015329
N = NormalDist()


def _p_two_sided(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def _books(fname: str, d: dict) -> dict | None:
    for prefix, key in SECTIONS.items():
        if fname.startswith(prefix) and isinstance(d.get(key), dict):
            return d[key]
    books = d.get("books")
    return books if isinstance(books, dict) else None


def dedup_same_book(rows: list[dict]) -> list[dict]:
    """Merge rows that are the same backtest under another name (same n_trades, |dt| < 1e-3)."""
    kept: list[dict] = []
    for r in rows:
        twin = next((k for k in kept if k["n_trades"] == r["n_trades"] and abs(k["t"] - r["t"]) < SAME_T_TOL), None)
        if twin is None:
            kept.append(r)
        else:
            twin.setdefault("same_as", []).append(f"{r['variant']} ({r['report']})")
    return kept


def collect(reports: str = REPORTS) -> list[dict]:
    rows, seen = [], set()
    for f in sorted(glob.glob(os.path.join(reports, "*.json"))):
        if "_fund_v1" in f:                                   # archived pre-audit numbers (S28), kept for the record only
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        fname = os.path.basename(f)
        books = _books(fname, d) if isinstance(d, dict) else None
        if not books:
            continue
        pre_audit = PRE_AUDIT.get(fname, set())
        for name, m in books.items():
            t = m.get("alpha2_t_nw") if isinstance(m, dict) else None
            if t is None or (isinstance(t, float) and math.isnan(t)) or not m.get("n_trades"):
                continue                                   # factor-only cells never traded: not a tested variant
            key = ALIASES.get(name, name)
            if key in CONTROLS:
                key = "insider_v1_5d"
            row = {"variant": key, "report": fname, "t": float(t),
                   "alpha2_ann_pct": m.get("alpha2_ann_pct"), "cagr_pct": m.get("cagr_pct"), "n_trades": m.get("n_trades")}
            if name in pre_audit:
                row["pre_audit"] = True
            if key in seen:                                   # same variant re-run on newer data: the later report wins
                rows[[r["variant"] for r in rows].index(key)] = row
                continue
            seen.add(key)
            rows.append(row)
    return dedup_same_book(rows)


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


# -- deflated Sharpe ---------------------------------------------------------------------------

def expected_max_t(n_trials: int) -> float:
    """E[max] of n independent N(0, 1) draws (the false-strategy theorem's approximation)."""
    if n_trials <= 1:
        return 0.0
    return (1 - EULER_GAMMA) * N.inv_cdf(1 - 1 / n_trials) + EULER_GAMMA * N.inv_cdf(1 - 1 / (n_trials * math.e))


def deflated_sharpe(x: np.ndarray, n_trials: int, nw_ratio: float = 1.0, sd_t: float = 1.0) -> dict:
    """PSR against SR0 = the Sharpe the best of n_trials null strategies would show.

    x: daily series (here alpha + residual of the two-factor regression). The cross-trial sd of
    the daily Sharpe is sd_t / sqrt(T) (t = SR x sqrt(T)); sd_t = 1 is the null value. nw_ratio
    (t_NW / t_iid of x) scales the z-score so serial correlation is not ignored. n_trials = 1
    gives PSR(0).
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    T = len(x)
    sr = x.mean() / x.std(ddof=1)
    d = x - x.mean()
    skew = float((d ** 3).mean() / d.std() ** 3)
    kurt = float((d ** 4).mean() / d.std() ** 4)                      # not excess
    sr0 = sd_t / math.sqrt(T) * expected_max_t(n_trials)
    den = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(T - 1) / den * nw_ratio
    return {"n_trials": n_trials, "expected_max_t": expected_max_t(n_trials), "sr_ann": sr * math.sqrt(252),
            "sr0_ann": sr0 * math.sqrt(252), "skew": skew, "kurtosis": kurt, "dsr": N.cdf(z)}


def alpha2_series(r: np.ndarray, spy: np.ndarray, size: np.ndarray) -> tuple[np.ndarray, float]:
    """alpha + residual of r on [1, SPY, IWM - SPY] (the engine's alpha2), and the daily alpha."""
    X = np.column_stack([np.ones(len(r)), spy, size])
    b = np.linalg.lstsq(X, r, rcond=None)[0]
    return r - X @ b + b[0], float(b[0])


def base_book_dsr(nav_csv: str, panel_path: str, trials: list[int], nav_col: str = "v1c") -> dict:
    """PSR(0) and DSR of the base book's alpha2 series. Needs SPY and IWM from index_daily."""
    import pandas as pd

    from hedge_fund.features.panel import PanelStore
    from hedge_fund.validation.stats import newey_west_t

    nav = pd.read_csv(nav_csv, parse_dates=["trade_date"]).set_index("trade_date")[nav_col]
    with PanelStore(panel_path, read_only=True) as store:
        spy = store.index_series("SPY").reindex(nav.index).ffill()
        iwm = store.index_series("IWM").reindex(nav.index).ffill()
    r = nav.pct_change().dropna()
    m = spy.pct_change().reindex(r.index).fillna(0)
    f = (iwm.pct_change() - spy.pct_change()).reindex(r.index).fillna(0)
    e, a = alpha2_series(r.to_numpy(), m.to_numpy(), f.to_numpy())
    t_nw = newey_west_t(e, lag=5)
    t_iid = e.mean() / (e.std(ddof=1) / math.sqrt(len(e)))
    ratio = t_nw / t_iid if t_iid else 1.0
    psr = deflated_sharpe(e, 1, ratio)["dsr"]
    rows = [deflated_sharpe(e, n, ratio) for n in trials]
    return {"nav_file": os.path.basename(nav_csv), "column": nav_col, "start": str(nav.index[0].date()),
            "end": str(nav.index[-1].date()), "T": int(len(e)), "alpha2_ann_pct": a * 252 * 100, "t_nw": t_nw,
            "t_iid": t_iid, "psr0": psr, "trials": rows,
            "dsr_range": [min(x["dsr"] for x in rows), max(x["dsr"] for x in rows)]}


# -- output ------------------------------------------------------------------------------------

def render(rows: list[dict], dsr: dict | None = None, dsr_note: str | None = None) -> str:
    n_same = sum(len(r.get("same_as", [])) for r in rows)
    L = [f"# Family-wise ledger — {len(rows)} book-level variants", "",
         "Two-sided p from the Newey-West t; Holm step-down over the whole family. "
         "Variants with a negative t are the rejected event lines (the sign is informative, the test is symmetric). "
         "Holm is reported, not used as an adoption gate (AGENT_PLAN §5.5, thresholds v3).", "",
         f"One backtest, one row: {n_same} re-runs of the same book under another name (same trades, same t) are merged "
         "into the first row (`same_as` in family_log.jsonl). "
         "修正前数据 = S24 variant not re-run after the S28/S29 data audit (old data).", "",
         "| variant | report | t | alpha2/yr | p raw | p Holm | ≤0.05 | ≤0.10 | note |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        a = f"{r['alpha2_ann_pct']:+.1f}%" if r.get("alpha2_ann_pct") is not None else "—"
        note = "修正前数据" if r.get("pre_audit") else ""
        if r.get("same_as"):
            note = (note + "; " if note else "") + f"= {len(r['same_as'])} re-run(s)"
        L.append(f"| {r['variant']} | {r['report']} | {r['t']:.2f} | {a} | {r['p_raw']:.3f} | {r['p_holm']:.3f} | "
                 f"{'✓' if r['p_holm'] <= 0.05 else ''} | {'✓' if r['p_holm'] <= 0.10 else ''} | {note} |")
    L += ["", "## Selection: expected best t of N null trials", "",
          "| N trials | E[max t] |", "|---|---|"]
    for n in sorted({5, 13, len(rows)}):
        L.append(f"| {n} | {expected_max_t(n):.2f} |")
    L += ["", "## Base book: PSR(0) and deflated Sharpe", ""]
    if dsr is None:
        L.append(f"Not computed: {dsr_note or 'no NAV / panel given'}.")
    else:
        source = (" This NAV is the latest restatement on the corrected data (agent/s47_restate.py: S47, then S47b); the base row in the ledger above still comes "
                  "from its own report, so its t and alpha2 can differ from these."
                  if dsr["nav_file"] == BASE_NAV else "")
        L += [f"alpha2 series (alpha + residual on SPY and IWM − SPY) of `{dsr['nav_file']}` [{dsr['column']}], "
              f"{dsr['start']} → {dsr['end']}, T = {dsr['T']}: alpha2 {dsr['alpha2_ann_pct']:+.2f}%/yr, "
              f"NW t {dsr['t_nw']:.2f} (iid t {dsr['t_iid']:.2f}).{source}", "",
              f"**PSR(0) = {dsr['psr0']:.3f}** (no selection at all; the usual bar is 0.95).", "",
              "| trials N | E[max t] | SR0 (ann.) | SR (ann.) | DSR |", "|---|---|---|---|---|"]
        for x in dsr["trials"]:
            L.append(f"| {x['n_trials']} | {x['expected_max_t']:.2f} | {x['sr0_ann']:.3f} | {x['sr_ann']:.3f} | {x['dsr']:.3f} |")
        lo, hi = dsr["dsr_range"]
        L += ["", f"**DSR range {lo:.2f}–{hi:.2f}.** The trials are not independent (5 of the 13 books before v1 were "
              "variants of one composite book; the ledger rows share data and rules), so the effective N is unknown and only "
              "the range is meaningful. Each DSR assumes sd(t) = 1 across trials; skew and kurtosis are the series' own."]
    return "\n".join(L) + "\n"


def main() -> int:
    from hedge_fund.paths import PANEL_DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write family_log.jsonl / .md / _dsr.json next to the reports")
    ap.add_argument("--reports", default=REPORTS)
    ap.add_argument("--nav", default=None, help=f"base NAV csv (default <reports>/{BASE_NAV})")
    ap.add_argument("--nav-col", default="v1c")
    ap.add_argument("--panel", default=str(PANEL_DB))
    args = ap.parse_args()
    rows = holm(collect(args.reports))
    nav = args.nav or os.path.join(args.reports, BASE_NAV)
    dsr, note = None, None
    try:
        dsr = base_book_dsr(nav, args.panel, sorted({5, 13, len(rows)}), args.nav_col)
    except Exception as exc:                            # a locked or missing panel must not block the ledger
        note = f"{type(exc).__name__}: {exc}"
    text = render(rows, dsr, note)
    print(text)
    if args.write:
        with open(os.path.join(args.reports, "family_log.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(args.reports, "family_log.md"), "w") as f:
            f.write(text)
        if dsr is not None:
            with open(os.path.join(args.reports, "family_log_dsr.json"), "w") as f:
                json.dump(dsr, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
