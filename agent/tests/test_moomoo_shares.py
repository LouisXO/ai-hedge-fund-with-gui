"""S47b item 1: share counts from moomoo snapshots, the override window, the audit's market-cap check.

No network: the snapshot client is a fake; the store is a temporary panel.db."""
import builtins
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from agent import audit
from agent.books.data import fundamentals as read_fundamentals
from agent.sources import moomoo_shares as M
from hedge_fund.features.panel import PanelStore

# moomoo on 2026-09-29: issued_shares, last_price, total_market_val (ADR counts are in ADS)
SNAP = {"TSM": (5.19e9, 290.0, 5.19e9 * 290.0), "BABA": (2.49e9, 180.0, 2.49e9 * 180.0),
        "AAPL": (14.59e9, 255.0, 14.59e9 * 255.0), "GMRS": (54.0e6, 10.0, 54.0e6 * 10.0),
        "MCHB": (221.4e6, 3.0, 221.4e6 * 3.0)}


class FakeQuotes:
    """get_market_snapshot as the SDK has it: (ret, frame); one unknown code fails the whole request."""
    def __init__(self, snap=SNAP, unknown=()):
        self.snap, self.unknown, self.calls = snap, set(unknown), []

    def snapshot(self, codes):
        self.calls.append(list(codes))
        if any(c[3:] in self.unknown for c in codes):
            raise M.SnapshotError(f"unknown stock {codes}")
        rows = [{"code": c, "issued_shares": self.snap.get(c[3:], (1e8, 10.0, 1e9))[0],
                 "last_price": self.snap.get(c[3:], (1e8, 10.0, 1e9))[1],
                 "total_market_val": self.snap.get(c[3:], (1e8, 10.0, 1e9))[2]} for c in codes]
        return pd.DataFrame(rows)


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


# ---- fetch: batches, pacing, one bad code ----------------------------------------------------------

def test_fetch_batches_of_200_with_us_codes():
    q = FakeQuotes()
    tickers = [f"T{i}" for i in range(450)] + ["BRK-B"]
    got = M.fetch(q, tickers, clock=Clock())
    assert [len(c) for c in q.calls] == [200, 200, 51]
    assert q.calls[0][0] == "US.T0" and "US.BRK.B" in q.calls[2]                  # moomoo writes class shares with a dot
    assert len(got) == 451 and set(got.columns) >= {"ticker", "issued_shares", "last_price", "total_market_val"}
    assert "BRK-B" in set(got["ticker"])                                              # mapped back to our spelling


def test_fetch_paces_to_60_requests_per_30_seconds():
    clock = Clock()
    M.fetch(FakeQuotes(), [f"T{i}" for i in range(61)], batch=1, clock=clock)
    assert len(clock.slept) == 1 and clock.slept[0] == pytest.approx(30.0)            # the 61st request waits


def test_one_unknown_code_does_not_sink_its_batch():
    q = FakeQuotes(unknown={"GONE"})
    got = M.fetch(q, ["AAPL", "GONE", "TSM", "BABA"], clock=Clock())
    assert sorted(got["ticker"]) == ["AAPL", "BABA", "TSM"]


def test_missing_sdk_says_which_python_to_use(monkeypatch):
    real = builtins.__import__

    def no_moomoo(name, *a, **k):
        if name == "moomoo" or name.startswith("moomoo."):
            raise ImportError("No module named 'moomoo'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_moomoo)
    with pytest.raises(RuntimeError, match=r"/Users/louis/\.moomoo/venv/bin/python.*PYTHONPATH"):
        M.MoomooQuotes()


# ---- snapshot -> override rows ----------------------------------------------------------------------

def test_rows_use_issued_shares_and_skip_inconsistent_snapshots():
    snap = pd.DataFrame([
        {"ticker": "TSM", "issued_shares": 5.19e9, "last_price": 290.0, "total_market_val": 5.19e9 * 290.0 * 1.005},
        {"ticker": "BAD", "issued_shares": 1e8, "last_price": 10.0, "total_market_val": 1.2e9},     # 20% off
        {"ticker": "NIL", "issued_shares": 0.0, "last_price": 10.0, "total_market_val": 1e9},
        {"ticker": "NAN", "issued_shares": np.nan, "last_price": 10.0, "total_market_val": 1e9}])
    rows, skipped = M.override_rows(snap, xbrl={"TSM": 25.9e9}, as_of=dt.date(2026, 9, 29))
    assert rows.to_dict("records") == [{"ticker": "TSM", "shares": 5.19e9, "source": "moomoo_snapshot",
                                        "as_of": dt.date(2026, 9, 29), "xbrl_shares": 25.9e9}]
    assert set(skipped) == {"BAD", "NIL", "NAN"} and "1%" in skipped["BAD"]


def test_targets_are_liquid_flagged_review_names_plus_the_top_60():
    review = pd.DataFrame({"ticker": ["GMRS", "MCHB", "THIN", "OKAY", "TSM"],
                           "shares_flag": ["mismatch", "stale", "ads", "", "ads"]})
    adv = pd.Series({"GMRS": 8e6, "MCHB": 6e6, "THIN": 1e6, "OKAY": 9e9, "TSM": 5e9})
    got = M.select_targets(review, adv, top=["AAPL", "GMRS"])
    assert got == ["AAPL", "GMRS", "MCHB", "TSM"]                                     # THIN illiquid, OKAY unflagged


# ---- writing: one row per ticker, latest wins, newer manual rows stay ------------------------------

def rows_of(store):
    return store.con.execute("SELECT * FROM shares_override ORDER BY ticker").df().set_index("ticker")


def test_write_replaces_older_rows_and_keeps_newer_manual_ones(store):
    store.con.execute("CREATE TABLE shares_override(ticker VARCHAR PRIMARY KEY, shares DOUBLE, source VARCHAR, "
                      "as_of DATE, xbrl_shares DOUBLE)")
    store.con.execute("INSERT INTO shares_override VALUES ('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL),"
                      "('ERIE', 5.2e7, 'manual', DATE '2026-10-05', NULL), ('STZ', 1.7e8, 'yfinance_current', DATE '2026-09-22', NULL)")
    new = pd.DataFrame({"ticker": ["V", "ERIE", "TSM"], "shares": [1.9e9, 5.3e7, 5.19e9], "source": M.SOURCE,
                        "as_of": [dt.date(2026, 9, 29)] * 3, "xbrl_shares": [np.nan, np.nan, 25.9e9]})
    assert M.write_overrides(store, new) == 2
    got = rows_of(store)
    assert got.loc["V", "shares"] == 1.9e9 and got.loc["V", "source"] == M.SOURCE        # older row replaced
    assert got.loc["ERIE", "source"] == "manual"                                         # newer manual row kept
    assert got.loc["STZ", "source"] == "yfinance_current" and got.loc["TSM", "shares"] == 5.19e9
    rerun = new.assign(shares=[1.91e9, 5.3e7, 5.2e9])                                    # same day again: latest wins
    M.write_overrides(store, rerun)
    got = rows_of(store)
    assert got.loc["V", "shares"] == 1.91e9 and got.loc["TSM", "shares"] == 5.2e9 and len(got) == 4


def test_write_creates_the_table(store):
    new = pd.DataFrame({"ticker": ["TSM"], "shares": [5.19e9], "source": M.SOURCE,
                        "as_of": [dt.date(2026, 9, 29)], "xbrl_shares": [np.nan]})
    assert M.write_overrides(store, new) == 1 and rows_of(store).loc["TSM", "shares"] == 5.19e9


# ---- books: which rows the override reaches ----------------------------------------------------------

def test_override_reaches_missing_or_flagged_rows_filed_within_120_days_before_as_of(store):
    fund = pd.DataFrame({
        "ticker": ["GMRS"] * 5 + ["AAPL", "V", "V"],
        "shares": [np.nan, 5.0e6, 5.0e6, 5.0e6, np.nan, 14.7e9, np.nan, 0.0],
        "shares_flag": ["mismatch", "mismatch", "unconfirmed", "", "stale", "", "", ""],
        "filed": pd.to_datetime(["2026-05-01", "2026-06-01", "2026-08-01", "2026-08-15", "2026-10-01",
                                 "2026-08-01", "2026-06-30", "2026-09-01"])})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    store.con.execute("CREATE TABLE shares_override(ticker VARCHAR PRIMARY KEY, shares DOUBLE, source VARCHAR, "
                      "as_of DATE, xbrl_shares DOUBLE)")
    store.con.execute("INSERT INTO shares_override VALUES ('GMRS', 54.0e6, 'moomoo_snapshot', DATE '2026-09-29', 5e6),"
                      "('AAPL', 14.59e9, 'moomoo_snapshot', DATE '2026-09-29', 14.7e9),"
                      "('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL)")
    got = read_fundamentals(store)["shares"].tolist()
    assert np.isnan(got[0])              # filed 151 days before as_of: left as built
    assert got[1:3] == [54.0e6, 54.0e6]  # flagged, within 120 days (a count is replaced, not only filled)
    assert got[3] == 5.0e6               # unflagged with a count: never overridden
    assert np.isnan(got[4])              # filed after as_of: left as built
    assert got[5] == 14.7e9              # AAPL's own count stays
    assert got[6:] == [1.877e9, 1.877e9]  # yfinance_current under the same rule; 0 shares is missing


# ---- audit: our market cap against moomoo's ----------------------------------------------------------

def test_market_cap_cross_check_flags_names_off_by_more_than_30pct():
    ours = pd.Series({"AAPL": 14.7e9 * 255, "TSM": 25.9e9 * 290, "GMRS": 5.0e6 * 10, "NEW": 1e9})
    close = pd.Series({"AAPL": 255.0, "TSM": 290.0, "GMRS": 10.0, "NEW": 10.0})
    ref = pd.Series({"AAPL": 14.59e9, "TSM": 5.19e9, "GMRS": 54.0e6})
    off, missing = audit.mcap_vs_moomoo(ours, close, ref)
    assert sorted(off) == ["GMRS", "TSM"] and missing == ["NEW"]
    assert off["TSM"] == pytest.approx(25.9 / 5.19)
