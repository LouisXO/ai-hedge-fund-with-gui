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


@pytest.mark.parametrize("closed, days", [(False, 0), (True, 1)])
def test_default_end_leaves_out_an_open_session(store, monkeypatch, closed, days):
    # yf's end is exclusive: during the session the default must stop at yesterday (no in-progress bar).
    import datetime as dt

    import agent.backfill as bf
    monkeypatch.setattr(bf, "session_closed", lambda now=None: closed)
    dl, calls = _fake_download(list(FACTOR_ETFS))
    backfill_factor_etfs(store, download=dl)
    assert calls[0][2] == (dt.date.today() + dt.timedelta(days=days)).isoformat()


def test_nothing_downloaded_writes_nothing(store):
    for dl in (_fake_download([])[0], lambda symbols, start, end: pd.DataFrame()):   # yfinance can return an empty frame
        assert backfill_factor_etfs(store, download=dl, end="2024-01-10") == {"rows": 0, "symbols": {}, "missing": list(FACTOR_ETFS)}
    assert store.con.execute("SELECT count(*) FROM index_daily").fetchone()[0] == 0


def test_postclose_updates_the_factor_etfs_every_day_before_drift():
    # S54: the comment said "the weekly job", no job ever called it, and index_daily stopped at 2026-09-28
    lines = [l for l in open(os.path.join(ROOT, "agent/bin/postclose.sh")).read().splitlines() if l.startswith("$PY")]
    i = next(k for k, l in enumerate(lines) if "-m agent.backfill factors" in l)
    j = next(k for k, l in enumerate(lines) if "-m agent.drift" in l)
    assert i < j and "|| echo" in lines[i] and "non-fatal" in lines[i]


def test_the_factors_command_downloads_then_opens_the_panel(monkeypatch):
    import sys

    import agent.backfill as bf
    calls = []
    monkeypatch.setattr(bf, "backfill_factor_etfs", lambda: calls.append("etfs") or {"rows": 5, "symbols": {}, "missing": []})
    monkeypatch.setattr(bf, "PanelStore", lambda *a, **k: calls.append("panel"))     # the command opens nothing itself
    monkeypatch.setattr(sys, "argv", ["agent.backfill", "factors"])
    assert bf.main() == 0 and calls == ["etfs"]
    monkeypatch.setattr(bf, "backfill_factor_etfs", lambda: {"rows": 0, "symbols": {}, "missing": list(FACTOR_ETFS)})
    assert bf.main() == 1                                                           # nothing downloaded: the step says so
