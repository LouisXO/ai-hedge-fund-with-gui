"""SIC code → Fama-French 12 industry, for sector-neutral factor scores.

Ranges follow Ken French's "12 Industry Portfolios" definition. Anything not
covered is "Other" (FF12 group 12), which is also where financials that
French puts in "Money" are NOT — those get their own group here (FF12 11).
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


def ff12(sic: float | int | None) -> str:
    if sic is None or pd.isna(sic):
        return "Other"
    s = int(sic)
    for name, ranges in FF12:
        if any(lo <= s <= hi for lo, hi in ranges):
            return name
    return "Other"


def industry_by_ticker(store) -> pd.Series:
    """ticker -> FF12 group, via fundamentals_pit's cik→ticker and the SIC parquet."""
    if not SIC_FILE.exists():
        raise FileNotFoundError(f"{SIC_FILE} missing: run python -m agent.sources.sec_sic")
    sic = pd.read_csv(SIC_FILE)[["cik", "sic"]]
    ct = store.con.execute("SELECT DISTINCT cik, ticker FROM fundamentals_pit WHERE ticker IS NOT NULL").df()
    m = ct.merge(sic, on="cik", how="left")
    m["group"] = m["sic"].map(ff12)
    return m.drop_duplicates("ticker").set_index("ticker")["group"]
