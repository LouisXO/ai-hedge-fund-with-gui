"""S24 — long book construction variants, pre-registered 2026-09-22 before any run.

v1 (live): four-family equal-weight composite, scored daily, enter rank<=30,
exit rank>60, equal-dollar slots, no stops. Its 2017–2026 record is alpha2
+12.6%/yr (t 2.25) with beta 1.05 / size beta 0.76, MaxDD −49%, and 2026's
gain concentrated in five names. Each variant changes ONE thing:

  n50         30 → 50 slots (keep 100): concentration
  secneutral  z-scores within Fama-French 12 industries: no theme bets
  invvol      position size ∝ 1 / trailing-252d vol, mean 1/N, clipped to [0.5/N, 2/N]
  stop20      sell when a position is 20% below its highest close since entry
  momcrash    momentum family dropped while SPY is >20% below its 2-year high (Daniel–Moskowitz)
  issuance    net share issuance added to the quality family (Pontiff–Woodgate)
  combo       n50 + secneutral + invvol (the construction changes together)

Scoring is done once per day per scoring configuration and reused by the
construction variants, so the runs differ only where the variant says.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR

CRASH_LOOKBACK = 504
CRASH_DD = 0.20
VOL_WINDOW = 252


def spy_in_crash_regime(market: Market) -> pd.Series:
    dd = market.spy / market.spy.rolling(CRASH_LOOKBACK, min_periods=120).max() - 1
    return dd < -CRASH_DD


def daily_scores(market: Market, fund: pd.DataFrame, start: str, end: str, groups: pd.Series | None = None,
                 issuance: bool = False, momcrash: bool = False, every: int = 1, norm: str = "winsor_z") -> dict[pd.Timestamp, pd.Series]:
    """day -> composite score (Series over the day's universe), with n_families >= 3."""
    days = market.adj.loc[start:end].index[::every]
    tradable = market.tradable(ADV_FLOOR, np.inf)
    crash = spy_in_crash_regime(market) if momcrash else None
    out = {}
    for d in days:
        ok = tradable.loc[d]
        universe = ok[ok].index
        fs = factor_scores(market, fund, d, universe, groups=groups, issuance=issuance,
                           drop_momentum=bool(crash.get(d, False)) if momcrash else False, norm=norm)
        fs = fs[fs["n_families"] >= 3]
        out[d] = fs["composite"].dropna().sort_values(ascending=False)
    return out


def targets_from_scores(scores: dict[pd.Timestamp, pd.Series], top_n: int, keep_mult: int = 2) -> dict[pd.Timestamp, list[str]]:
    return {d: s.head(keep_mult * top_n).index.tolist() for d, s in scores.items()}


def invvol_sizes(market: Market, targets: dict[pd.Timestamp, list[str]], top_n: int,
                 lo: float = 0.5, hi: float = 2.0) -> dict[pd.Timestamp, dict[str, float]]:
    """Fraction of equity per name: (1/vol) scaled so the mean over the day's list is 1/N, clipped."""
    vol = np.log(market.adj / market.adj.shift(1)).rolling(VOL_WINDOW, min_periods=120).std()
    out = {}
    for d, names in targets.items():
        v = vol.loc[d].reindex(names)
        inv = (1 / v).replace([np.inf, -np.inf], np.nan)
        if inv.notna().sum() < 5:
            continue
        w = inv / inv.mean()                                  # mean 1 across the list
        w = w.clip(lo, hi).fillna(1.0) / top_n                 # mean ≈ 1/N, bounded
        out[d] = w.to_dict()
    return out
