"""Month-boundary rule for the live long book."""
import pandas as pd

from agent.books.live import is_rebalance_day


def test_first_run_after_a_month_end_rebalances():
    assert is_rebalance_day(pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01"), None)


def test_rebalances_once_per_month_end_only():
    me = pd.Timestamp("2026-09-30")
    assert not is_rebalance_day(me, pd.Timestamp("2026-09-30"), None)          # still September
    assert is_rebalance_day(me, pd.Timestamp("2026-10-01"), None)              # first October morning
    assert not is_rebalance_day(me, pd.Timestamp("2026-10-02"), me)            # already recorded
    assert not is_rebalance_day(pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-02"), me)  # mid-month


def test_year_boundary():
    assert is_rebalance_day(pd.Timestamp("2026-12-31"), pd.Timestamp("2027-01-04"), pd.Timestamp("2026-11-30"))
