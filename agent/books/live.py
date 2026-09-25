"""The long book, live: what the composite would hold from the next open.

Cadence is signal-driven, not calendar-driven (decided 2026-09-20: "long
term" means the holding period, not month-ends). Every morning the
universe is scored on the last completed bar and the top KEEP_MULT*N
names are recorded with their rank. Read with the engine's slot rule that
gives: enter when a name ranks inside the top N and a slot is free, keep
it while it stays inside the top 2N, sell when it falls out — exactly
agent/books/long_term.daily_composite, which is what the backtest ran.

Recording the ranked list daily (rather than a held set) keeps the shadow
record stateless: holdings are reconstructed by replaying the engine on
the recorded lists, so the live record and the backtest use one code path.

Shorting was allowed but not favoured, so momentum stays a component of
the composite and there is no long-short book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market, fundamentals
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR, TOP_N
from hedge_fund.features.panel import PanelStore

SIGNAL = "composite_long"
SIGNAL_V2 = "composite_long_v2"      # v1 + momentum-crash filter (S24), shadow only — pre-registered 2026-09-22
KEEP_MULT = 2


def should_score(last_bar: pd.Timestamp, last_recorded: pd.Timestamp | None) -> bool:
    """Once per completed bar: skip if this bar's list is already recorded (reruns are idempotent)."""
    return last_recorded is None or last_recorded.normalize() != last_bar.normalize()


def day_scores(store: PanelStore, market: Market, day: pd.Timestamp, drop_momentum: bool = False) -> pd.DataFrame:
    fund = fundamentals(store)
    if fund is None:
        return pd.DataFrame()
    tradable = market.tradable(ADV_FLOOR, np.inf)
    ok = tradable.loc[day]
    universe = ok[ok].index
    fs = factor_scores(market, fund, day, universe, drop_momentum=drop_momentum)
    fs = fs[fs["n_families"] >= 3]
    return fs.sort_values("composite", ascending=False)


def crash_regime(market: Market, day: pd.Timestamp) -> bool:
    """S24 momcrash: SPY more than 20% below its 2-year high as of `day` (data through day only)."""
    from agent.books.long_v2 import spy_in_crash_regime
    r = spy_in_crash_regime(market)
    return bool(r.get(day, False))


def targets(store: PanelStore, market: Market, day: pd.Timestamp, top_n: int = TOP_N, v2: bool = False) -> list[dict]:
    """The day's ranked list: top KEEP_MULT*N. Ranks <= N are entry candidates, the rest are keep-only.

    v2=True is the shadow line: identical to v1 except that in a crash regime the momentum
    family is dropped from the composite. Outside a crash the two lists are the same."""
    crash = crash_regime(market, day) if v2 else False
    fs = day_scores(store, market, day, drop_momentum=crash)
    if fs.empty:
        return []
    out = []
    for rank, (ticker, row) in enumerate(fs.head(KEEP_MULT * top_n).iterrows(), 1):
        out.append({"ticker": ticker, "signal_name": SIGNAL_V2 if v2 else SIGNAL, "side": "L", "rank": rank,
                    "value": float(row["composite"]), "instrument": "stock",
                    "limit_ref": float(market.close.at[day, ticker]),
                    "spread_pct": market.spread_pct(ticker, day),
                    "gate_passed": rank <= top_n,                   # False = keep-only zone (N < rank <= 2N)
                    "gate_reason": f"v{row['value']:+.2f} q{row['quality']:+.2f} "
                                                        f"m{row['momentum']:+.2f} lv{row['lowvol']:+.2f}" + (" crash:no-mom" if crash else ""),
                    "expected_net_pct": None, "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None})
    return out


SIGNAL_LC_QLV = "largecap_qlv"        # S37: quality + low volatility within market cap >= $10B — shadow only, never traded


def largecap_qlv_targets(store: PanelStore, market: Market, day: pd.Timestamp, top_n: int = TOP_N) -> list[dict]:
    """The S37 large-cap defensive line as a daily ranked list (top 2N), recorded next to v1, not traded."""
    from agent.s37_largecap import large_universe
    fund = fundamentals(store)
    if fund is None:
        return []
    u = large_universe(market, fund, day)
    if len(u) < 50:
        return []
    fs = factor_scores(market, fund, day, u)
    fs = fs[fs["n_families"] >= 3]
    sc = fs[["quality", "lowvol"]].mean(axis=1).dropna().sort_values(ascending=False)
    out = []
    for rank, (t, v) in enumerate(sc.head(KEEP_MULT * top_n).items(), 1):
        row = fs.loc[t]
        out.append({"ticker": t, "signal_name": SIGNAL_LC_QLV, "side": "L", "rank": rank, "value": float(v), "instrument": "stock",
                    "limit_ref": float(market.close.at[day, t]), "spread_pct": market.spread_pct(t, day), "gate_passed": rank <= top_n,
                    "gate_reason": f"q{row['quality']:+.2f} lv{row['lowvol']:+.2f} mcap${row['mcap'] / 1e9:.0f}B",
                    "expected_net_pct": None, "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None})
    return out
