"""S54 statistic correction: event_report's SPY benchmark covers the stock's window (open D+1 to the close of D+h).
Before, it ran from the close of D+1, so at h = 1 the benchmark was identically 0 and a stock that is SPY showed an
abnormal return at every horizon."""
import types

import numpy as np
import pandas as pd

from agent import event_report as er
from hedge_fund.features.panel import PanelStore


def _market():
    days = pd.bdate_range("2026-01-05", periods=30)
    rng = np.random.default_rng(0)
    spy_open = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 30)), index=days)
    spy_close = spy_open * (1 + rng.normal(0.001, 0.01, 30))          # SPY moves inside every session
    adj = pd.DataFrame({"TWIN": spy_close, "FLAT": 50.0}, index=days)
    adj_open = pd.DataFrame({"TWIN": spy_open, "FLAT": 50.0}, index=days)
    return types.SimpleNamespace(adj=adj, adj_open=adj_open, spy=spy_close), spy_open


def test_a_stock_that_is_spy_has_no_abnormal_return_at_any_horizon():
    m, spy_open = _market()
    for h in (1, 5, 10, 20):
        fwd, mkt = er.windows(m, spy_open, h)
        abn = (fwd["TWIN"] - mkt).dropna()
        assert len(abn) == 30 - h and np.allclose(abn, 0.0), h
        old = (m.spy.shift(-h) / m.spy.shift(-1) - 1) * 100             # the window before the fix
        assert not np.allclose((fwd["TWIN"] - old).dropna(), 0.0), h
    _, mkt1 = er.windows(m, spy_open, 1)
    assert mkt1.dropna().abs().max() > 0.1                              # the old h = 1 benchmark was identically 0


def test_index_open_is_on_the_adjusted_scale(tmp_path):
    days = pd.bdate_range("2026-01-05", periods=3)
    with PanelStore(tmp_path / "p.db") as s:
        s.upsert_index(pd.DataFrame([{"symbol": "SPY", "trade_date": d.date(), "open": 10.0, "high": 20.0, "low": 10.0,
                                      "close": 20.0, "adj_close": 10.0, "source": "test", "fetched_at": pd.Timestamp.now()}
                                     for d in days]))
        got = er.index_adj_open(s, "SPY", days)
    assert list(got) == [5.0, 5.0, 5.0]                                 # open x adj_close / close, as Market.adj_open


def test_the_twelve_old_reports_carry_the_registered_note_and_keep_their_numbers():
    """Registration S54 item 3: the affected old reports are annotated in the reports themselves, not rerun."""
    import glob
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = [f for p in ("s22_pead_small_*", "s23_insider_*", "s25_move_*", "s34_buyback_clean_*")
             for f in glob.glob(os.path.join(root, "site-data", "validation", f"{p}.md"))]
    assert len(files) == 12
    for f in files:
        text = open(f).read()
        assert text.split("\n")[2].startswith("> **Note (S54, 2026-10-21):**") and "h1 the benchmark is therefore 0" in text
        old = subprocess.run(["git", "-C", root, "show", f"2ee01e9:{os.path.relpath(f, root)}"], capture_output=True, text=True)
        if old.returncode == 0:                                     # every line of the report as written is still there
            assert set(old.stdout.split("\n")) <= set(text.split("\n"))
