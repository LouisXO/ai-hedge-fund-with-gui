"""Large-cap check: one ticker per company, foreign filers out, cap- vs equal-weighted returns, movers (no network)."""
import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from agent import market_breadth as mb


def test_universe_keeps_one_ticker_per_company_and_leaves_foreign_filers_out():
    last = pd.DataFrame({"close": [100.0, 99.0, 50.0, 10.0], "dv": [5e9, 1e9, 2e9, 3e8]}, index=["GOOGL", "GOOG", "TSM", "SMALL"])
    shares = pd.Series({"GOOGL": 12e9, "GOOG": 12e9, "TSM": 26e9, "SMALL": 1e8})
    cik = pd.Series({"GOOGL": 1, "GOOG": 1, "TSM": 2, "SMALL": 3})
    u = mb.universe(last, shares, cik, foreign={2}, n=10)
    assert list(u.index) == ["GOOGL", "SMALL"]                                  # GOOG: same CIK, less traded; TSM: 20-F
    assert u.loc["GOOGL", "mcap"] == pytest.approx(1.2e12)


def _frame(days: int, prices: dict[str, list[float]]) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-01", periods=days)
    return pd.DataFrame(prices, index=idx)


def test_cap_weighted_return_and_contributions_add_up():
    n = 30
    base = {"BIG": [100.0] * (n - 1) + [102.0], "MID": [50.0] * (n - 1) + [49.0], "TINY": [10.0] * (n - 1) + [11.0]}
    adj = close = _frame(n, base)
    uni = pd.DataFrame({"shares": [10.0, 4.0, 2.0], "mcap": [1020.0, 196.0, 22.0]}, index=["BIG", "MID", "TINY"])
    idx = _frame(n, {"SPY": list(np.linspace(100, 101, n))})
    out = mb.compute(adj, close, uni, idx, extra=())
    w = np.array([1000.0, 200.0, 20.0]) / 1220.0                                # market caps at the previous close
    r = np.array([0.02, -0.02, 0.10])
    assert out["breadth"]["cw"]["r1"] == pytest.approx(float((w * r).sum()))
    assert out["breadth"]["ew"]["r1"] == pytest.approx(float(r.mean()))
    bp = sum(m["bp"] for m in out["movers"]["up"] + out["movers"]["down"])
    assert bp / 1e4 == pytest.approx(out["breadth"]["cw"]["r1"])
    assert [m["ticker"] for m in out["movers"]["down"]] == ["MID"]
    assert out["breadth"]["pct_up"] == pytest.approx(2 / 3)
    assert out["breadth"]["new_highs"] == 2 and out["breadth"]["new_lows"] == 1    # BIG and TINY above every earlier close
    assert out["leaders"][0]["ticker"] == "BIG" and out["leaders"][0]["mcap_b"] == pytest.approx(1020.0 / 1e9)
    assert out["indexes"]["SPY"]["date"] == str(adj.index[-1].date())


def test_earnings_calendar_gives_each_leader_its_next_report_and_the_week_ahead():
    text = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
            "BIG,Big Co,2026-10-08,2026-09-30,1.0,USD,post-market\n"
            "BIG,Big Co,2027-01-20,2026-12-31,1.1,USD,post-market\n"
            "MID,Mid Co,2026-10-30,2026-09-30,0.5,USD,pre-market\n"
            "BRK.B,Berkshire,2026-10-07,2026-09-30,,USD,\n")
    cal = mb.parse_calendar(text)
    out = {"leaders": [{"ticker": "BIG"}, {"ticker": "MID"}], "extra": []}
    out = mb.add_earnings(out, cal, ["BIG", "MID", "BRK.B"], dt.date(2026, 10, 5))
    assert out["leaders"][0]["next_earnings"] == "2026-10-08" and out["leaders"][1]["next_earnings"] == "2026-10-30"
    assert [e["ticker"] for e in out["earnings_soon"]] == ["BRK.B", "BIG"]          # bars' BRK.B matches the calendar; date order


def test_a_throttle_answer_is_not_a_calendar():
    with pytest.raises(RuntimeError):
        mb.parse_calendar('{"Information": "rate limit"}')


def test_nan_becomes_null():
    assert mb._clean({"a": [math.nan, 1.0], "b": {"c": float("nan")}}) == {"a": [None, 1.0], "b": {"c": None}}
