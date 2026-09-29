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


def test_command_line_dry_run_writes_nothing_and_a_run_writes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "panel.db"
    with PanelStore(path) as s:
        fund = pd.DataFrame({"ticker": ["GMRS", "TSM"], "shares": [np.nan, 25.9e9], "shares_flag": ["mismatch", ""],
                             "filed": pd.to_datetime(["2026-08-12", "2026-04-16"])})
        s.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    monkeypatch.setattr(M, "PanelStore", lambda *a, **k: PanelStore(path, **k))
    fake = lambda host, port: type("C", (FakeQuotes,), {"close": lambda self: None})(unknown={"GONE"})
    assert M.main(["--dry-run", "--tickers", "GMRS,TSM", "GONE"], client_factory=fake) == 0
    out = capsys.readouterr().out
    assert "targets 3, answered 2, usable 2" in out and "skip GONE" in out and "dry run" in out
    with PanelStore(path, read_only=True) as s:
        assert "shares_override" not in s.con.execute("SHOW TABLES").df()["name"].tolist()
    assert M.main(["--tickers", "GMRS", "TSM"], client_factory=fake) == 0
    with PanelStore(path, read_only=True) as s:
        got = s.con.execute("SELECT * FROM shares_override").df().set_index("ticker")
    assert got.loc["GMRS", "shares"] == 54.0e6 and got.loc["TSM", "xbrl_shares"] == 25.9e9
    assert (got["as_of"] == pd.Timestamp(dt.date.today())).all()


# ---- review fixes: the override log, splits, hand-checked names, same-day sources, the audit ------------

OV_DDL = ("CREATE TABLE shares_override(ticker VARCHAR PRIMARY KEY, shares DOUBLE, source VARCHAR, "
          "as_of DATE, xbrl_shares DOUBLE)")


def moomoo_rows(day, **counts):
    return pd.DataFrame({"ticker": list(counts), "shares": list(counts.values()), "source": M.SOURCE,
                         "as_of": [day] * len(counts), "xbrl_shares": np.nan})


def test_same_day_row_of_another_source_stays_in_both_tables(store):
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('ERIE', 5.23e7, 'manual', DATE '2026-09-29', NULL)")
    assert M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 29), ERIE=4.62e7)) == 0
    assert rows_of(store).loc["ERIE", "source"] == "manual"
    log = store.con.execute("SELECT * FROM shares_override_log").df()
    assert log["source"].tolist() == ["manual"] and log["shares"].tolist() == [5.23e7]


def test_log_keeps_every_row_the_current_table_had(store):
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL)")
    M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 29), V=1.93e9))
    M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 30), V=1.92e9))
    M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 30), V=1.925e9))                # a rerun replaces its own day
    log = store.con.execute("SELECT source, CAST(as_of AS VARCHAR) d, shares FROM shares_override_log ORDER BY as_of").df()
    assert log.values.tolist() == [["yfinance_current", "2026-09-22", 1.877e9], [M.SOURCE, "2026-09-29", 1.93e9],
                                   [M.SOURCE, "2026-09-30", 1.925e9]]
    assert rows_of(store).loc["V", "shares"] == 1.925e9 and len(rows_of(store)) == 1


def test_hand_checked_names_stay_fetch_targets_after_moomoo_replaced_them(store):
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL),"
                      "('STZ', 1.71e8, 'yfinance_current', DATE '2026-09-22', NULL)")
    M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 29), V=1.93e9, TSM=5.19e9))
    assert rows_of(store).loc["V", "source"] == M.SOURCE
    assert M.hand_checked(store) == ["STZ", "V"]                                          # TSM is moomoo only
    empty = pd.DataFrame(columns=["ticker", "shares_flag"])
    assert M.select_targets(empty, pd.Series(dtype=float), top=["AAPL"], checked=M.hand_checked(store)) == ["AAPL", "STZ", "V"]


def test_a_row_takes_the_earliest_override_in_its_window_and_keeps_it(store):
    fund = pd.DataFrame({"ticker": ["V", "V", "V", "STZ"], "shares": [np.nan, np.nan, np.nan, np.nan],
                         "shares_flag": ["", "", "", ""], "shares_asof": pd.NaT,
                         "filed": pd.to_datetime(["2026-04-29", "2026-07-28", "2026-10-27", "2026-07-01"])})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL),"
                      "('STZ', 1.71e8, 'yfinance_current', DATE '2026-09-22', NULL)")
    for day, n in ((dt.date(2026, 9, 29), 1.93e9), (dt.date(2026, 10, 28), 1.92e9), (dt.date(2027, 2, 1), 1.90e9)):
        M.write_overrides(store, moomoo_rows(day, V=n))                                    # daily fetch, as_of moves on
    got = read_fundamentals(store)
    assert np.isnan(got.loc[0, "shares"])                     # filed 2026-04-29: nothing within 120 days after it
    assert got.loc[1, "shares"] == 1.877e9                    # 2026-07-28: the yfinance row of 09-22, not the 2027 one
    assert got.loc[1, "shares_asof"] == pd.Timestamp("2026-09-22")
    assert got.loc[2, "shares"] == 1.92e9                     # 2026-10-27: the first snapshot after it
    assert got.loc[3, "shares"] == 1.71e8                     # STZ's hand-checked count, still in the log only


def test_no_override_across_a_split_between_filing_and_as_of(store):
    fund = pd.DataFrame({"ticker": ["CTNT", "FWD", "OK"], "shares": [9.0e8, np.nan, np.nan],
                         "shares_flag": ["mismatch", "", ""], "filed": pd.to_datetime(["2026-08-13"] * 3)})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    days = pd.bdate_range("2026-08-10", "2026-09-29")
    px = pd.concat([pd.DataFrame({"ticker": t, "trade_date": days, "close": 1.0,
                                  "adj_close": np.where(days < pd.Timestamp("2026-09-28"), k, 1.0)})
                    for t, k in (("CTNT", 150.0), ("FWD", 0.5), ("OK", 0.99))])       # 1:150 reverse, 2:1 forward, a dividend
    store.con.execute("INSERT INTO bars (ticker, trade_date, close, adj_close) "
                      "SELECT ticker, CAST(trade_date AS DATE), close, adj_close FROM px")
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('CTNT', 6.0e6, 'moomoo_snapshot', DATE '2026-09-29', NULL),"
                      "('FWD', 2.0e8, 'moomoo_snapshot', DATE '2026-09-29', NULL), ('OK', 3.0e7, 'moomoo_snapshot', DATE '2026-09-29', NULL)")
    got = read_fundamentals(store).set_index("ticker")["shares"]
    assert got["CTNT"] == 9.0e8 and np.isnan(got["FWD"])      # as built: the count is on the post-split basis
    assert got["OK"] == 3.0e7


def test_audit_compares_the_top_60_with_moomoo_rows_only(store, monkeypatch):
    import agent.books.data as data
    import agent.books.factors as factors
    names = ["X2"] + [f"T{i:02d}" for i in range(62)]
    day, prev = pd.Timestamp("2026-09-28"), pd.Timestamp("2026-09-25")
    close = pd.DataFrame({t: [20.0, 10.0] for t in names}, index=[prev, day])       # our cap uses the day's close
    market = type("Mk", (), {"adj": close, "close": close,
                             "tradable": lambda self, lo, hi: close.notna()})()
    fs = pd.DataFrame({"composite": np.arange(len(names), 0, -1, dtype=float), "mcap": 1e9,
                       "n_families": [2] + [3] * 62, "value": 0.0, "quality": 0.0, "momentum": 0.0, "lowvol": 0.0},
                      index=names)                                                   # X2 ranks first on 2 families only
    monkeypatch.setattr(data, "load_market", lambda store, start: market)
    monkeypatch.setattr(factors, "factor_scores", lambda *a, **k: fs)
    fund = pd.DataFrame({"ticker": names, "shares": 1e8, "shares_flag": "", "equity": 5e8, "assets": 1e9,
                         "shares_asof": pd.Timestamp("2026-06-30"), "filed": pd.Timestamp("2026-08-01")})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('T00', 2.0e8, 'moomoo_snapshot', DATE '2026-09-29', NULL),"
                      "('T01', 1.0e8, 'yfinance_current', DATE '2026-09-22', NULL),"
                      "('T02', 1.0e8, 'moomoo_snapshot', DATE '2026-09-29', NULL)")
    rep = audit.Report()
    audit.audit_factors(store, rep)
    detail = next(d for sec, check, st, d in rep.rows if "vs moomoo" in check)
    assert detail.startswith("1 apart {'T00': 0.5}")         # T02 matches at the day's close (at 09-25's it would be 0.5)
    assert "58 without a moomoo count" in detail and "'T01'" in detail and "X2" not in detail


def test_a_count_fixed_by_hand_in_shares_override_wins_over_the_log(store):
    fund = pd.DataFrame({"ticker": ["ERIE"], "shares": [np.nan], "shares_flag": ["stale"],
                         "filed": pd.to_datetime(["2026-09-01"])})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    M.write_overrides(store, moomoo_rows(dt.date(2026, 9, 29), ERIE=4.62e7))            # one class only
    store.con.execute("UPDATE shares_override SET shares = 5.23e7, source = 'manual' WHERE ticker = 'ERIE'")
    assert read_fundamentals(store)["shares"].tolist() == [5.23e7]


def test_default_targets_add_the_hand_checked_names(store, monkeypatch, tmp_path):
    import agent.books.data as data
    import agent.books.fundamentals as fundamentals
    import agent.books.live as live
    store.con.execute("INSERT INTO bars (ticker, trade_date, close, adj_close) VALUES ('AAPL', DATE '2026-09-28', 1, 1)")
    day = pd.Timestamp("2026-09-28")
    market = type("Mk", (), {"adj": pd.DataFrame(index=[day]), "adv20": pd.DataFrame({"GMRS": [8e6]}, index=[day])})()
    monkeypatch.setattr(data, "load_market", lambda store, start: market)
    monkeypatch.setattr(live, "day_scores", lambda store, market, day: pd.Series([2.0, 1.0], index=["AAPL", "MU"]))
    monkeypatch.setattr(fundamentals, "review_path", lambda store: tmp_path / "shares_review.csv")
    pd.DataFrame({"ticker": ["GMRS"], "shares_flag": ["mismatch"]}).to_csv(tmp_path / "shares_review.csv", index=False)
    store.con.execute(OV_DDL)
    store.con.execute("INSERT INTO shares_override VALUES ('V', 1.877e9, 'yfinance_current', DATE '2026-09-22', NULL)")
    assert M.default_targets(store) == ["AAPL", "GMRS", "MU", "V"]
