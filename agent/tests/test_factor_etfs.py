"""S48 (audit long_strategy #6): factor ETFs go to index_daily through their own function, off the order path."""
from __future__ import annotations

import os

import pandas as pd
import pytest

from agent.backfill import FACTOR_ETFS, INDEX_SYMBOLS, backfill_factor_etfs
from hedge_fund.features.panel import PanelStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_download(present):
    days = pd.bdate_range("2024-01-02", periods=5, name="Date")
    cols = pd.MultiIndex.from_product([present, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    df = pd.DataFrame(10.0, index=days, columns=cols)
    calls = []

    def dl(symbols, start, end):
        calls.append((tuple(symbols), start, end))
        return df
    return dl, calls


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


def test_writes_every_etf_to_index_daily_and_reports_missing(store):
    present = [s for s in FACTOR_ETFS if s != "AVUV"]
    dl, calls = _fake_download(present)
    out = backfill_factor_etfs(store, download=dl, end="2024-01-10")
    assert calls == [(FACTOR_ETFS, "2012-01-01", "2024-01-10")]           # one request, ETFs only
    assert out["missing"] == ["AVUV"] and out["rows"] == 5 * len(present)
    got = dict(store.con.execute("SELECT symbol, count(*) FROM index_daily GROUP BY 1").fetchall())
    assert got == {s: 5 for s in present}


def test_the_research_etfs_stay_out_of_the_production_index_refresh():
    assert INDEX_SYMBOLS == ("SPY", "^VIX", "IWM", "QQQ")
    assert not set(FACTOR_ETFS) & set(INDEX_SYMBOLS)
    for f in ("agent/execute.py", "agent/daily.py"):
        src = open(os.path.join(ROOT, f)).read()
        assert "backfill_factor_etfs" not in src and "FACTOR_ETFS" not in src, f


def test_nothing_downloaded_writes_nothing(store):
    for dl in (_fake_download([])[0], lambda symbols, start, end: pd.DataFrame()):   # yfinance can return an empty frame
        assert backfill_factor_etfs(store, download=dl, end="2024-01-10") == {"rows": 0, "symbols": {}, "missing": list(FACTOR_ETFS)}
    assert store.con.execute("SELECT count(*) FROM index_daily").fetchone()[0] == 0
