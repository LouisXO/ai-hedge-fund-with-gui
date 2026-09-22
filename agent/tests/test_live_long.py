"""Once-per-bar rule for the live long book (signal-driven cadence)."""
import pandas as pd
import pytest

from agent.books.live import should_score


def test_scores_each_new_bar_once():
    bar = pd.Timestamp("2026-09-30")
    assert should_score(bar, None)
    assert not should_score(bar, bar)                          # rerun the same morning: idempotent
    assert should_score(pd.Timestamp("2026-10-01"), bar)       # next completed bar


def test_engine_sizes_and_stop_rule():
    """S24: per-name sizing scales the entry; a 20% drawdown from the high sells at the next open."""
    import numpy as np
    from agent.books.data import Market
    from agent.books.engine import simulate
    idx = pd.bdate_range("2026-01-05", periods=60)     # long enough for the metrics' bootstrap
    px = pd.DataFrame(10.0, index=idx, columns=["A", "B"])
    px.loc[idx[3]:, "A"] = 12.0                       # A rallies to 12 (new high)
    px.loc[idx[6]:, "A"] = 9.0                        # then drops 25% from that high -> stop
    adv = pd.DataFrame(5e6, index=idx, columns=["A", "B"])
    m = Market(px, px, px, adv, pd.DataFrame(True, index=idx, columns=["A", "B"]), pd.Series(100.0, index=idx), {})
    targets = {d: ["A", "B"] for d in idx}
    sizes = {d: {"A": 0.30, "B": 0.10} for d in idx}
    res = simulate(m, targets, str(idx[0].date()), str(idx[-1].date()), max_positions=2, hold_days=None,
                   exec_frac=0.0, sizes=sizes, stop_pct=20.0)
    a = [t for t in res.trades if t.ticker == "A"]
    assert len(a) == 1 and a[0].exit_day == idx[7]                     # flagged at idx[6]'s close, sold next open
    assert a[0].ret_pct == pytest.approx(-10.0)                         # 10 -> 9
    # sizing: A got 30% of equity, B 10% -> exposure on day 1 is 40%
    assert res.exposure.iloc[1] == pytest.approx(0.40, abs=0.01)
    # A re-enters after the stop because it is still in the target list (no cooling-off rule in v1)
