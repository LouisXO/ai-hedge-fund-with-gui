"""S48 (audit long_strategy #1, #7): engine.metrics' calendar-year returns and turnover denominator."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from agent.books.engine import Result, metrics


def _result(nav: pd.Series, turnover_on_capital: float = 10.0) -> Result:
    spy = pd.Series(np.linspace(100_000, 120_000, len(nav)), index=nav.index)
    return Result(nav=nav, spy_nav=spy, trades=[], exposure=pd.Series(1.0, index=nav.index),
                  turnover=turnover_on_capital)


def test_by_year_includes_each_years_first_trading_day():
    days = pd.bdate_range("2019-12-02", "2021-01-29")
    nav = pd.Series(100_000.0, index=days)
    first_2020 = days[days.year == 2020][0]
    first_2021 = days[days.year == 2021][0]
    nav[nav.index >= first_2020] *= 1.10          # +10% on the first trading day of 2020, flat otherwise
    nav[nav.index >= first_2021] *= 0.95          # -5% on the first trading day of 2021
    m = metrics(_result(nav), years=(days[-1] - days[0]).days / 365.25)
    assert m["by_year"] == {2019: 0.0, 2020: 0.10, 2021: -0.05}
    chained = np.prod([1 + v for v in m["by_year"].values()]) - 1
    assert math.isclose(chained, m["total_return_pct"] / 100, abs_tol=1e-6)
    assert math.isclose(np.prod([1 + v for v in m["spy_by_year"].values()]) - 1, m["spy_total_pct"] / 100, abs_tol=1e-3)


def test_turnover_uses_average_nav():
    days = pd.bdate_range("2017-01-02", periods=500)
    nav = pd.Series(np.linspace(100_000, 300_000, len(days)), index=days)     # mean NAV = 2x the capital
    m = metrics(_result(nav, turnover_on_capital=9.8), years=(days[-1] - days[0]).days / 365.25)
    assert math.isclose(m["turnover_ann_on_capital"], 9.8)
    assert math.isclose(m["turnover_ann"], 9.8 / 2.0, rel_tol=1e-9)
