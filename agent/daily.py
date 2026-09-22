"""The agent's daily run — shadow mode.

Order (after optradar's run_daily has finished and released its lock):
 1. incremental yfinance update of the panel (last 10 days) + SPY/^VIX;
 2. compute every registered signal on the last completed bar, over that
    day's point-in-time S&P 500 members;
 3. apply the option-cheapness gate (S2/S3: the gate is the only effect
    that survived, and each name's breakeven is what a pick must clear);
 4. record signals and would-be picks in optradar.db, fill realized
    returns for older dates, and write out/agent/<date>.json.

No paper positions are opened: nothing has passed validation, so the
agent earns a stock-level scoreboard first (docs/AGENT_PLAN.md §9, S3).
Flip MODE to "live" only after a signal passes on fresh data.

Usage: python -m agent.daily [--date YYYY-MM-DD] [--dry-run] [--no-update]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import uuid

import numpy as np
import pandas as pd

from agent import ledger, signals_insider
from agent.books import live as long_live
from agent.books.data import load_market
from agent.backfill import backfill_bars, backfill_index, session_closed
from agent.s2_option_hurdle import breakeven_move
from agent.s3_signals import DTE, K_IV, SPREAD_PCT, resid_reversal_5
from hedge_fund.features.factors import mom_12_1, reversal_5
from hedge_fund.features.panel import PanelStore
from hedge_fund.features.rv import yang_zhang

MODE = "shadow"          # "shadow" = record only; "live" = also open paper positions
TOP_K = 5
VIX_KILL = 30.0
OUT_DIR = "/Users/louis/optradar/out/agent"
VALIDATED: set[str] = set()   # nothing promoted yet (S3)


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout.strip()
    except Exception:
        return ""


def compute(store: PanelStore, as_of: str | None) -> dict:
    close, adj = store.bars_wide("close"), store.bars_wide("adj_close")
    opn, high, low = store.bars_wide("open"), store.bars_wide("high"), store.bars_wide("low")
    spy = store.index_series("SPY", "adj_close").reindex(adj.index).ffill()
    vix = store.index_series("^VIX", "close").reindex(adj.index).ffill()
    if as_of is None:
        # last COMPLETED bar: during the session anything dated today is partial
        cutoff = pd.Timestamp(dt.date.today()) if session_closed() else pd.Timestamp(dt.date.today()) - pd.Timedelta(days=1)
        day = adj.index[adj.index <= cutoff][-1]
    else:
        day = adj.index[adj.index <= pd.Timestamp(as_of)][-1]
    mask = store.membership_mask(pd.DatetimeIndex([day]), sorted(adj.columns)).iloc[0]

    rv20 = yang_zhang(opn, high, low, close, 20).loc[day]
    rv60 = yang_zhang(opn, high, low, close, 60).loc[day]
    iv = rv20 * K_IV
    members = mask[mask].index
    sig = {"resid_reversal_5": resid_reversal_5(adj, spy).loc[day],
           "reversal_5": reversal_5(adj).loc[day],
           "mom_12_1": mom_12_1(adj).loc[day]}
    frames = {}
    for name, s in sig.items():
        s = s.reindex(members)
        z = (s - s.median()) / (1.4826 * (s - s.median()).abs().median() or np.nan)
        frames[name] = pd.DataFrame({"value": z.clip(-3, 3) / 3, "raw": s, "rv20": rv20.reindex(members),
                                     "rv60": rv60.reindex(members), "iv": iv.reindex(members)}).dropna(subset=["raw"])
    return {"day": day, "frames": frames, "iv": iv.reindex(members), "rv20": rv20.reindex(members),
            "rv60": rv60.reindex(members), "vix": float(vix.loc[day]) if day in vix.index else float("nan"),
            "members": list(members), "panel": (adj, opn * (adj / close), spy)}


def gate(iv: pd.Series, rv20: pd.Series, rv60: pd.Series) -> pd.DataFrame:
    """Option-cheapness gate: cheap half of the cross-section AND vol not elevated."""
    low_iv = iv <= iv.median()
    reverting = rv20 < rv60
    reason = pd.Series("", index=iv.index)
    reason[~low_iv] = "iv_above_median"
    reason[low_iv & ~reverting] = "rv20_above_rv60"
    return pd.DataFrame({"passed": low_iv & reverting, "reason": reason})


def pick(frames: dict, g: pd.DataFrame, iv: pd.Series, hold: int = 14) -> list[dict]:
    """Top-K per signal and side among names that clear the gate."""
    out = []
    pool = g.index[g["passed"]]
    for name, df in frames.items():
        s = df["value"].reindex(pool).dropna()
        for side, asc in (("C", False), ("P", True)):
            top = s.sort_values(ascending=asc).head(TOP_K)
            for rank, (ticker, val) in enumerate(top.items(), 1):
                iv_i = float(iv.get(ticker, np.nan))
                be = breakeven_move(iv_i, DTE, hold, SPREAD_PCT, side) if np.isfinite(iv_i) else None
                out.append({"ticker": ticker, "signal_name": name, "side": side, "rank": rank,
                            "value": float(val), "iv": iv_i,
                            "rv20": float(df["rv20"].get(ticker, np.nan)),
                            "rv60": float(df["rv60"].get(ticker, np.nan)),
                            "breakeven_pct": be, "gate_passed": True,
                            "gate_reason": "", "status": MODE})
    return out


def insider_picks(store: PanelStore, day: pd.Timestamp, window: int = 3) -> list[dict]:
    """Stock-side candidates (S11-S13): cluster or >=$250k buys, small/mid only, limit orders."""
    df = signals_insider.candidates(store, day, window)
    if df.empty:
        return []
    out = []
    for rank, r in enumerate(df[df["eligible"]].itertuples(), 1):
        edge = signals_insider.expected_edge(store, str(r.bucket), day.year)
        out.append({"ticker": r.ticker, "signal_name": signals_insider.SIGNAL, "side": "L", "rank": rank,
                    "value": float(min(r.buy_usd / signals_insider.BIG_USD, 5.0)),
                    "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None,
                    "gate_passed": True, "gate_reason": r.kind, "status": MODE,
                    "limit_ref": float(r.last_close), "spread_pct": edge["quoted_spread_pct"],
                    "expected_net_pct": edge["net_at_half_spread_pct"], "instrument": "stock"})
    return out


def long_book_picks(day: pd.Timestamp, optradar_db: str) -> list[dict]:
    """Composite long book, scored every completed bar (signal-driven cadence)."""
    con = ledger.connect(optradar_db, read_only=True)
    try:
        row = con.execute("SELECT max(as_of) FROM agent_picks WHERE signal_name = ?", [long_live.SIGNAL]).fetchone()
    except Exception:
        row = (None,)
    finally:
        con.close()
    last_recorded = pd.Timestamp(row[0]) if row and row[0] is not None else None
    if not long_live.should_score(day, last_recorded):
        return []
    with PanelStore(read_only=True) as store:
        market = load_market(store, (day - pd.Timedelta(days=420)).date().isoformat())
        rows = long_live.targets(store, market, day)
        try:                                                   # v2 shadow line (S24): same list unless SPY is in a crash regime
            rows += long_live.targets(store, market, day, v2=True)
        except Exception as exc:
            print(f"agent: v2 shadow skipped: {exc}")
        return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true", help="compute and write JSON, touch no database")
    ap.add_argument("--no-update", action="store_true", help="skip the yfinance incremental update")
    ap.add_argument("--optradar-db", default=ledger.OPTRADAR_DB)
    args = ap.parse_args()

    run_id = f"{dt.date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    status, error = "ok", None
    if not args.no_update:
        # yfinance is flaky under launchd (sqlite cache "unable to open database file", 2026-09-22);
        # the after-close Alpaca update already holds the last completed bar, so a failed refresh
        # must not stop the run — score on what the panel has.
        try:
            with PanelStore() as store:
                backfill_bars(store, incremental=True, pause=1.0)
                backfill_index(store)
        except Exception as exc:
            print(f"agent: panel refresh failed, scoring on the existing panel: {exc}")
    try:
        with PanelStore(read_only=True) as store:
            state = compute(store, args.date)
    except Exception as exc:                       # a bad panel must not take the brief down
        print(f"agent: compute failed: {exc}")
        return 1

    day = state["day"]
    g = gate(state["iv"], state["rv20"], state["rv60"])
    regime = "stress" if state["vix"] >= VIX_KILL else "normal"
    picks = [] if regime == "stress" else pick(state["frames"], g, state["iv"])
    for p in picks:
        p.setdefault("instrument", "option")
    with PanelStore(read_only=True) as store:                      # stock side, independent of the gate
        picks += insider_picks(store, day)
    try:
        picks += long_book_picks(day, args.optradar_db)              # daily ranked list, once per bar
    except Exception as exc:
        print(f"agent: long book skipped: {exc}")

    payload = {"as_of": str(day.date()), "run_id": run_id, "mode": MODE, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
               "universe_n": len(state["members"]), "vix": state["vix"], "regime": regime,
               "validated_signals": sorted(VALIDATED), "gate": {"passed": int(g["passed"].sum()),
                                                                "of": int(len(g))},
               "picks": picks, "note": "shadow mode: no paper positions; see docs/AGENT_PLAN.md §9 (S3)"}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{day.date()}.json"), "w") as f:
        json.dump(payload, f, indent=1, default=float)

    if not args.dry_run:
        con = ledger.connect(args.optradar_db)
        try:
            ledger.ensure_schema(con)
            ledger.write_signals(con, day.date(), run_id, state["frames"], VALIDATED)
            ledger.write_picks(con, [{**p, "as_of": day.date(), "ledger_id": None, "run_id": run_id} for p in picks])
            adj, adj_open, spy = state["panel"]
            filled = ledger.fill_returns(con, adj, adj_open, spy)
            ledger.write_run(con, {"run_id": run_id, "as_of": day.date(), "run_ts": pd.Timestamp.now(),
                                   "git_sha": _git_sha(), "mode": MODE, "universe_n": len(state["members"]),
                                   "n_signals": len(state["frames"]), "n_validated": len(VALIDATED),
                                   "vix": state["vix"], "regime": regime, "n_picks": len(picks),
                                   "n_opened": 0, "status": status, "error": error})
        finally:
            con.close()
        print(f"agent {day.date()}: {len(state['members'])} members, gate {int(g['passed'].sum())}/{len(g)}, "
              f"{len(picks)} shadow picks, {filled} return rows filled")
    else:
        print(f"agent {day.date()} (dry-run): gate {int(g['passed'].sum())}/{len(g)}, {len(picks)} picks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
