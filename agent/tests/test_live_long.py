"""Once-per-bar rule for the live long book (signal-driven cadence)."""
import pandas as pd

from agent.books.live import should_score


def test_scores_each_new_bar_once():
    bar = pd.Timestamp("2026-09-30")
    assert should_score(bar, None)
    assert not should_score(bar, bar)                          # rerun the same morning: idempotent
    assert should_score(pd.Timestamp("2026-10-01"), bar)       # next completed bar
