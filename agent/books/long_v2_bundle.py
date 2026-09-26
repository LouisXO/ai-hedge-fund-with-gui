"""The long book's v2 rule bundle — definitions, pre-registered 2026-09-26 before any run (S44).

The user fixed the bundle's content on 2026-09-26; anything added later is v3. Four rules:

  floor2    do not ENTER a name whose raw close on the signal day is < $2            (S40)
  jump5     do not ENTER a name with a big-move day in the last 5 sessions, signal
            day included; big move = |abnormal return vs SPY| >= max(5%, 3 sd)       (S25 / S33)
  cap20     do not ENTER a name whose Fama-French 12 industry already holds
            floor(20% x slots) positions in the book (6 of 30, 3 of 15). "Other" is
            a group like any other; a ticker without a SIC code is "Other".
  clusters  the book is two half-books with half the capital and half the slots each.
            Every scored name (>= 3 factor families, as in v1) belongs to exactly one:
              M  momentum-led   momentum z > value z, or value missing
              V  value-led      otherwise
            Inside a cluster names are ranked by the v1 composite; enter at rank <= 15,
            keep through rank 30, sell below — v1's slot rule at half size.

  bundle    clusters, with floor2 + jump5 + cap20 (3 per industry per half-book) applied
            to entries in both halves.

All four only change which names are ENTERED. Exits stay v1's (rank falls out of the keep
zone); a held name is never sold because of a veto or the cap.

Shadow lines recorded from the 2026-09-25 list (first fills at the 2026-09-28 open), all
replayed by agent/books/engine.simulate on the recorded lists, zero cost, fills at the open:
  v1c (control: v1 rules, same start), floor2, jump5, cap20, clusters, bundle.
The control exists because the traded paper book started on 09-22 with its own fills and
path; v2 must be compared with v1 under identical mechanics and an identical start.

Reading (fixed now): at the long book's evaluation point (agent/config.yaml) the bundle is
adopted as v2 only if BOTH
  (a) backtest (S44, 2017-01-01 → 2026-08-31, same engine settings as S33/S40): alpha2 is
      >= 0.5%/yr above base with the NW t not lower; and
  (b) shadow record: bundle NAV return minus v1c NAV return over the shadow window is > 0
      and the daily difference has a NW t >= 1 (a direction check — a few months cannot give
      t >= 2 on a difference of about 1%/yr, and we say so now rather than later).
Single rules are reported for attribution; only the bundle is a candidate. Holm budget:
+3 backtest variants (cap20, clusters, bundle; floor2 and jump5 were counted in S33/S40).
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from agent.books.data import Market
from agent.books.factors import factor_scores
from agent.books.long_term import ADV_FLOOR, TOP_N

FLOOR_USD = 2.0
JUMP_WINDOW = 5
CAP_SHARE = 0.20
HALF_N = TOP_N // 2
KEEP_MULT = 2
SIGNAL_M = "long_v2_clM"
SIGNAL_V = "long_v2_clV"
LINES = ["v1c", "floor2", "jump5", "cap20", "clusters", "bundle"]


def scored(market: Market, fund: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """v1's scored universe for the day (families + composite), best first."""
    ok = market.tradable(ADV_FLOOR, np.inf).loc[day]
    fs = factor_scores(market, fund, day, ok[ok].index)
    fs = fs[(fs["n_families"] >= 3) & fs["composite"].notna()]
    return fs.sort_values("composite", ascending=False)


def cluster_of(fs: pd.DataFrame) -> pd.Series:
    m_led = (fs["momentum"] > fs["value"]) | fs["value"].isna()
    return pd.Series(np.where(m_led & fs["momentum"].notna(), "M", "V"), index=fs.index)


def cluster_lists(fs: pd.DataFrame, half_n: int = HALF_N) -> dict[str, list[str]]:
    """{'M': [...], 'V': [...]}: each the cluster's top KEEP_MULT x half_n by composite."""
    cl = cluster_of(fs)
    return {c: fs.index[(cl == c).to_numpy()][:KEEP_MULT * half_n].tolist() for c in ("M", "V")}


def veto_frame(market: Market, floor: bool, jump: bool) -> pd.DataFrame:
    """days x tickers, True = do not enter on the list of that day."""
    from agent.s33_negative_filters import big_move_days, fresh
    v = pd.DataFrame(False, index=market.adj.index, columns=market.adj.columns)
    if floor:
        v |= (market.close < FLOOR_USD).fillna(False)
    if jump:
        v |= fresh(big_move_days(market), JUMP_WINDOW)
    return v


def cap_rule(groups: pd.Series, slots: int) -> Callable:
    """engine.simulate(entry_ok=...): refuse an entry once the name's industry holds floor(20% x slots)."""
    limit = int(np.floor(CAP_SHARE * slots + 1e-9))
    g = groups.to_dict()

    def ok(ticker: str, positions: dict) -> bool:
        mine = g.get(ticker, "Other")
        return sum(1 for t in positions if g.get(t, "Other") == mine) < limit
    return ok
