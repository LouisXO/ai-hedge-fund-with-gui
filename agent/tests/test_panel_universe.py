"""PanelStore + point-in-time membership: no network, temp DuckDB."""
import pandas as pd
import pytest

from agent.universe import data_ticker, intervals
from hedge_fund.features.factors import mom_12_1
from hedge_fund.features.panel import PanelStore

CHANGES = [
    ("2020-01-02", ["AAA", "BBB", "FB"]),
    ("2021-06-01", ["AAA", "CCC", "FB"]),          # BBB leaves, CCC joins
    ("2022-06-09", ["AAA", "CCC", "META"]),        # rename FB -> META
    ("2023-03-01", ["AAA", "BBB", "CCC", "META"]),  # BBB comes back
]


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    s.replace_membership("sp500", CHANGES)
    yield s
    s.close()


def test_members_on_a_date_are_the_latest_row_at_or_before_it(store):
    dates = pd.DatetimeIndex(["2019-12-31", "2020-01-02", "2021-05-31", "2021-06-01", "2022-06-10"])
    m = store.membership_mask(dates, ["AAA", "BBB", "CCC", "FB", "META"])
    assert not m.loc["2019-12-31"].any()                       # before the first row: nobody
    assert m.loc["2021-05-31", "BBB"] and not m.loc["2021-06-01", "BBB"]
    assert m.loc["2021-06-01", "CCC"]
    assert m.loc["2022-06-10", "META"] and not m.loc["2022-06-10", "FB"]


def test_intervals_handle_leave_and_return(store):
    ivs = [iv for iv in intervals(store.membership_changes()) if iv.ticker == "BBB"]
    spans = sorted((iv.start.date().isoformat(), iv.end.date().isoformat() if iv.end else None) for iv in ivs)
    assert spans == [("2020-01-02", "2021-06-01"), ("2023-03-01", None)]


def test_data_ticker_maps_renames_and_share_classes():
    assert data_ticker("FB") == "META"
    assert data_ticker("BRK.B") == "BRK-B"
    assert data_ticker("AAPL") == "AAPL"
    assert data_ticker("RTN") == "RTN"   # merger, deliberately not aliased to RTX


def test_bars_round_trip_wide(store):
    now = pd.Timestamp.now()
    df = pd.DataFrame({"ticker": ["AAA", "AAA", "CCC"],
                       "trade_date": pd.to_datetime(["2021-06-01", "2021-06-02", "2021-06-02"]).date,
                       "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
                       "close": [1.0, 2.0, 3.0], "adj_close": [0.9, 1.8, 3.0], "volume": [10.0, 20.0, 30.0],
                       "source": "test", "fetched_at": now})
    assert store.upsert_bars(df) == 3
    store.upsert_bars(df)                      # idempotent (primary key)
    wide = store.bars_wide("adj_close")
    assert wide.shape == (2, 2)
    assert wide.loc["2021-06-02", "CCC"] == 3.0
    assert pd.isna(wide.loc["2021-06-01", "CCC"])


def test_mom_12_1_only_uses_the_past():
    idx = pd.bdate_range("2020-01-01", periods=300)
    px = pd.DataFrame({"A": range(1, 301)}, index=idx, dtype=float)
    base = mom_12_1(px)
    bumped = px.copy()
    bumped.iloc[290:] *= 5                     # change the future only
    assert base.iloc[:290].equals(mom_12_1(bumped).iloc[:290])
    t = 280
    assert base.iloc[t, 0] == pytest.approx(px.iloc[t - 21, 0] / px.iloc[t - 252, 0] - 1)
