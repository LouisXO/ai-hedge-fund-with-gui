"""The insider-buy candidate list, in the form the daily run and the ledger need.

Rules come straight from what S11-S13 measured, not from taste:
- open-market purchases only, dated by FILING date (S11);
- keep a name if ONE filing day in the trailing window is either a cluster
  (>= 2 distinct insiders that day) or large (>= $250k that day), the two
  cuts that showed an edge (S11). The test is the backtest's own
  (agent/events/insider.py over agent/books/data.insider_flows, single
  trades above $50M ignored), so the live list cannot drift from it;
- the window is counted in TRADING days, so Monday's run still sees
  Friday's filings (S47, 2026-09-28: until then the window was 2 calendar
  days merged into one test — 8% of the entries were names the backtest
  never had, and a Friday filing loaded on Monday morning was never traded);
- ADV floor and ceiling: skip micro caps, where the 1.41% spread eats the
  0.60% gross edge, and skip large caps, where the edge is 0.07% (S13);
- entry is a LIMIT order, never a market order: at a full spread the edge
  is zero, at half it is +0.28% (t 2.57), passive +0.39% (t 3.60). So the
  candidate carries a limit reference price and the live quoted spread,
  and the ledger records what the fill would have cost.

Usage: python -m agent.signals_insider [--date YYYY-MM-DD] [--window 3]   (window in trading days)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import numpy as np
import pandas as pd

from agent.books.data import insider_flows
from agent.books.short_term import ADV_CEILING, ADV_FLOOR, BIG_USD, HOLD_DAYS   # the backtest's numbers, not copies
from agent.events.insider import InsiderBuys
from agent.s13_real_spreads import BUCKETS, LABELS
from hedge_fund.features.panel import PanelStore

MIN_ADV = ADV_FLOOR        # above micro: its spread is wider than the edge
MAX_ADV = ADV_CEILING      # below large: the edge there is 0.07%, i.e. nothing
SIGNAL = "insider_buy"
VERSION = "1"
ENTRY_KINDS = ("on_time", "late", "retry")


def window_sessions(as_of: dt.date, window: int, sessions: list[dt.date]) -> list[dt.date]:
    """The last `window` trading days up to and including as_of (on a Monday: Friday and Monday)."""
    return sorted(d for d in set(sessions) if d <= as_of)[-window:]


def triggers(events: pd.DataFrame, days: list[dt.date]) -> pd.DataFrame:
    """Per ticker, the most recent filing day inside `days` that qualifies on its own.

    events: the backtest's event list (InsiderBuys.events), one row per qualifying (filing day,
    ticker). Nothing is added up across days: one small buyer on each of two days is not a
    cluster here because it is not one in the backtest. lag_sessions = sessions from the trigger
    day to the last of `days`; 0 is the backtest's entry (filed on D, bought at the open of D+1).
    """
    lag = {pd.Timestamp(d): len(days) - 1 - i for i, d in enumerate(days)}
    ev = events[events["date"].isin(list(lag))].sort_values(["date", "strength"]).drop_duplicates("ticker", keep="last")
    return pd.DataFrame({"ticker": ev["ticker"], "trigger_filing_day": ev["date"].dt.date,
                         "lag_sessions": ev["date"].map(lag).astype(int), "buy_usd": ev["strength"].astype(float),
                         "kind": ev["detail"]}).reset_index(drop=True)


def entry_kind(lag_sessions: int, retry: bool) -> str:
    """How an entry relates to the backtest's; for the evaluation, never used to choose names.

    on_time  the filing day is the order's as_of: bought at the next open, as the backtest does
    late     the filing reached the panel after that evening's run, the entry is a session behind
    retry    an order for the name ended unfilled in the last 8 days (execute.blocking_orders),
             whatever the lag: the backtest would have been holding the name already
    """
    return "retry" if retry else "on_time" if lag_sessions == 0 else "late"


def candidates(store: PanelStore, as_of: pd.Timestamp, window: int = 3,
               sessions: list[dt.date] | None = None) -> pd.DataFrame:
    """Names with a qualifying Form 4 filing day among the last `window` trading days.

    sessions: the exchange calendar (the caller has the broker's). Without one the panel's own
    bar dates stand in, which are the same days up to the last completed bar.
    """
    start = (as_of - pd.Timedelta(days=120)).date().isoformat()
    close = store.bars_wide("close", start=start)
    vol = store.bars_wide("volume", start=start)
    if sessions is None:
        sessions = [d.date() for d in close.index]
    days = window_sessions(as_of.date(), window, sessions)
    if not days:
        return pd.DataFrame()
    ev = triggers(InsiderBuys().events(store, days[0].isoformat(), days[-1].isoformat()), days)
    if ev.empty:
        return ev
    flows = insider_flows(store, days[0].isoformat()).set_index(["date", "ticker"])["n_buyers"]   # for display only
    ev["n_buyers"] = [int(flows.get((pd.Timestamp(d), t), 0)) for d, t in zip(ev["trigger_filing_day"], ev["ticker"])]
    ev["last_filing"] = ev["trigger_filing_day"]

    adv = (close * vol).rolling(20).mean()
    day = close.index[close.index <= as_of][-1]
    ev = ev[ev["ticker"].isin(close.columns)].copy()
    ev["adv20"] = [float(adv.at[day, t]) if t in adv.columns else np.nan for t in ev["ticker"]]
    ev["last_close"] = [float(close.at[day, t]) if t in close.columns else np.nan for t in ev["ticker"]]
    ev = ev.dropna(subset=["adv20", "last_close"])
    ev["bucket"] = pd.cut(ev["adv20"], BUCKETS, labels=LABELS)
    ev["reason"] = np.where(ev["adv20"] < MIN_ADV, "adv_below_floor",
                            np.where(ev["adv20"] > MAX_ADV, "adv_above_ceiling", ""))
    ev["eligible"] = ev["reason"] == ""
    ev["bar_date"] = day.date()
    return ev.sort_values(["eligible", "buy_usd"], ascending=[False, False])


def expected_edge(store: PanelStore, bucket: str, year: int, spread_fraction: float = 0.5) -> dict:
    """S13's measured numbers for this bucket, so the brief can show what is expected."""
    row = store.con.execute("""SELECT median_spread_pct FROM spread_grid WHERE bucket = ? AND year = ?""",
                            [bucket, year]).fetchone()
    spread = float(row[0]) if row else float("nan")
    gross = {"small": 0.378, "mid": 0.190, "micro": 0.598, "large": 0.073}.get(bucket, float("nan"))
    return {"gross_pct_5d": gross, "quoted_spread_pct": spread,
            "net_at_half_spread_pct": gross - spread_fraction * spread}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--window", type=int, default=3)
    args = ap.parse_args()
    with PanelStore(read_only=True) as store:
        as_of = pd.Timestamp(args.date) if args.date else pd.Timestamp(dt.date.today())
        df = candidates(store, as_of, args.window)
        if df.empty:
            print("no qualifying filings in the window")
            return 0
        year = as_of.year
        out = []
        for r in df.itertuples():
            edge = expected_edge(store, str(r.bucket), year)
            out.append({"ticker": r.ticker, "kind": r.kind, "n_buyers": int(r.n_buyers),
                        "buy_usd": round(float(r.buy_usd)), "bucket": str(r.bucket),
                        "adv20_usd": round(float(r.adv20)), "limit_ref": round(float(r.last_close), 2),
                        "eligible": bool(r.eligible), "reason": r.reason,
                        "trigger_filing_day": str(r.trigger_filing_day), "lag_sessions": int(r.lag_sessions), **edge})
        print(json.dumps(out[:25], indent=1))
        print(f"{sum(o['eligible'] for o in out)} eligible of {len(out)} qualifying names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
