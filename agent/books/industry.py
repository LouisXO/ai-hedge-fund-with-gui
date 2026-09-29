"""SIC code → Fama-French 12 industry, for sector-neutral factor scores.

Ranges follow Ken French's "12 Industry Portfolios" definition. A SIC code not
covered is "Other" (FF12 group 12), which is also where financials that
French puts in "Money" are NOT — those get their own group here (FF12 11).
A ticker without a SIC code is "Unclassified" (since 2026-09-29, S48): it is not
evidence of being in French's "Other" group.

The groups feed the v2 shadow line's 20% industry cap and the weekly attribution;
the traded long v1 book does not use them.
"""
from __future__ import annotations

import pandas as pd

from hedge_fund.paths import AGENT_DIR

SIC_FILE = AGENT_DIR / "company_sic.csv"

FF12 = [
    ("NoDur", [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)]),
    ("Durbl", [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716), (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999)]),
    ("Manuf", [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629), (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)]),
    ("Enrgy", [(1200, 1399), (2900, 2999)]),
    ("Chems", [(2800, 2829), (2840, 2899)]),
    ("BusEq", [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)]),
    ("Telcm", [(4800, 4899)]),
    ("Utils", [(4900, 4949)]),
    ("Shops", [(5000, 5999), (7200, 7299), (7600, 7699)]),
    ("Hlth", [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)]),
    ("Money", [(6000, 6999)]),
]


UNCLASSIFIED = "Unclassified"


def ff12(sic: float | int | None) -> str:
    if sic is None or pd.isna(sic):
        return UNCLASSIFIED
    s = int(sic)
    for name, ranges in FF12:
        if any(lo <= s <= hi for lo, hi in ranges):
            return name
    return "Other"


def industry_by_ticker(store) -> pd.Series:
    """ticker -> FF12 group, via fundamentals_pit's cik→ticker and the SIC file.

    A ticker can map to several CIKs (475 of 7,758 on 2026-09-28: reused symbols, share classes,
    successor companies). Each ticker takes the CIK of its most recent filing (ties: the larger
    CIK), so the mapping is the same on every run; before 2026-09-29 an unordered DISTINCT +
    drop_duplicates picked one at random and the industry layer moved by ~$9 between runs.
    """
    if not SIC_FILE.exists():
        raise FileNotFoundError(f"{SIC_FILE} missing: run python -m agent.sources.sec_sic")
    sic = pd.read_csv(SIC_FILE)[["cik", "sic"]].drop_duplicates("cik")
    ct = store.con.execute("""SELECT ticker, cik FROM (
                                  SELECT ticker, cik, row_number() OVER (PARTITION BY ticker ORDER BY max(filed) DESC, cik DESC) AS k
                                  FROM fundamentals_pit WHERE ticker IS NOT NULL GROUP BY ticker, cik)
                              WHERE k = 1 ORDER BY ticker""").df()
    m = ct.merge(sic, on="cik", how="left")
    m["group"] = m["sic"].map(ff12)
    return m.set_index("ticker")["group"]
