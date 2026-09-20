"""Point-in-time fundamentals: Q4 derivation, filing-date visibility, factor z-scores."""
import numpy as np
import pandas as pd
import pytest

from agent.books import fundamentals as F
from agent.books.factors import _z, latest_before


def facts(rows):
    df = pd.DataFrame(rows, columns=["cik", "tag", "period_start", "period_end", "is_instant", "val", "form", "filed"])
    for c in ("period_start", "period_end", "filed"):
        df[c] = pd.to_datetime(df[c]).dt.date
    return df


def test_q4_is_annual_minus_three_quarters_and_known_at_the_10k():
    f = facts([
        (1, "NetIncomeLoss", "2024-01-01", "2024-03-31", False, 10, "10-Q", "2024-05-01"),
        (1, "NetIncomeLoss", "2024-04-01", "2024-06-30", False, 20, "10-Q", "2024-08-01"),
        (1, "NetIncomeLoss", "2024-07-01", "2024-09-30", False, 30, "10-Q", "2024-11-01"),
        (1, "NetIncomeLoss", "2024-01-01", "2024-12-31", False, 100, "10-K", "2025-02-20"),
    ])
    q = F.quarterly_flows(f, "ni", ["NetIncomeLoss"]).set_index("period_end")
    assert q.loc[pd.Timestamp("2024-12-31").date(), "ni"] == 40          # 100 - (10+20+30)
    assert str(q.loc[pd.Timestamp("2024-12-31").date(), "filed"]) == "2025-02-20"


def test_restated_value_does_not_leak_backwards():
    # the same quarter re-reported later with a different number: the first filing's value is what was known
    f = facts([
        (1, "NetIncomeLoss", "2024-01-01", "2024-03-31", False, 10, "10-Q", "2024-05-01"),
        (1, "NetIncomeLoss", "2024-01-01", "2024-03-31", False, 12, "10-Q", "2025-05-01"),
    ])
    q = F.quarterly_flows(f, "ni", ["NetIncomeLoss"])
    assert q["ni"].tolist() == [10]


def test_latest_before_uses_filing_date_not_period_end():
    fund = pd.DataFrame({"ticker": ["A", "A"], "filed": pd.to_datetime(["2024-11-01", "2025-02-20"]),
                         "ni_ttm": [1.0, 2.0]})
    assert latest_before(fund, pd.Timestamp("2025-01-31")).loc["A", "ni_ttm"] == 1.0   # 10-K not filed yet
    assert latest_before(fund, pd.Timestamp("2025-03-01")).loc["A", "ni_ttm"] == 2.0
    assert "A" not in latest_before(fund, pd.Timestamp("2026-06-01")).index          # stale > 200 days


def test_z_winsorizes_and_needs_breadth():
    s = pd.Series(np.r_[np.zeros(30), 1000.0])
    z = _z(s)
    assert z.max() < 6                                   # the outlier was clipped
    assert _z(pd.Series([1.0, 2.0, 3.0])).isna().all()   # too few names to standardize


def test_factor_scores_reject_negative_equity_and_shells():
    """A loss over negative equity must not become a high 'ROE' (the S16 quality-book bug)."""
    from agent.books.factors import factor_scores
    idx = pd.bdate_range("2025-01-01", periods=300)
    names = [f"T{i:02d}" for i in range(40)] + ["SHELL"]
    px = pd.DataFrame(50.0, index=idx, columns=names)

    class M:                                  # the two frames factor_scores reads
        adj = px
        close = px
    fund = pd.DataFrame({"ticker": names, "filed": pd.Timestamp("2025-11-01"),
                         "ni_ttm": [1e7] * 40 + [-5e7], "rev_ttm": 1e8, "gp_ttm": 4e7, "cogs_ttm": 6e7,
                         "cfo_ttm": 1e7, "assets": [1e9] * 40 + [2e7], "equity": [4e8] * 40 + [-3e7],
                         "shares": [1e7] * 40 + [1e9], "assets_1y": 9e8})
    fs = factor_scores(M, fund, idx[-1], pd.Index(names))
    assert pd.isna(fs.loc["SHELL", "quality"])          # excluded, not ranked first
    assert fs.loc["T00", "n_families"] >= 3
