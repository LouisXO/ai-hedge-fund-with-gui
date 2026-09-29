"""Insider candidates as the backtest defines them (S47): one filing day at a time, the $50M cap,
purchases priced like the market, a listed name, a window in trading days — and the entry
classification that the evaluation groups by."""
import datetime as dt

import duckdb
import pandas as pd
import pytest

from agent import ledger, signals_insider
from agent.brief import _eval_progress
from agent.events.insider import InsiderBuys
from agent.execute import insider_targets
from hedge_fund.features.panel import PanelStore

# 2026-09-07 is Labor Day: the exchange calendar, not weekdays
SESSIONS = [d.date() for d in pd.bdate_range("2026-08-03", "2026-10-09") if d.date() != dt.date(2026, 9, 7)]
FRI, MON, TUE = pd.Timestamp("2026-09-18"), pd.Timestamp("2026-09-21"), pd.Timestamp("2026-09-22")


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "p.db")
    rows = []
    # all ~ $10M-$50M ADV: inside the book's range, so only the filing rules decide
    # THIN trades $2.7M a day until a $27M Tuesday lifts its 20-day mean over the $3M floor
    for t, px, vol in [("AAA", 20.0, 500_000), ("BBB", 50.0, 1_000_000), ("CCC", 10.0, 2_000_000), ("THIN", 3.0, 900_000)]:
        for d in SESSIONS:
            if d > TUE.date():
                break
            v = vol * 10 if (t == "THIN" and d == TUE.date()) else vol
            rows.append({"ticker": t, "trade_date": d, "open": px, "high": px, "low": px,
                         "close": px, "adj_close": px, "volume": float(v), "source": "test",
                         "fetched_at": pd.Timestamp.now()})
    s.upsert_bars(pd.DataFrame(rows))
    listed(s, list(PX))
    yield s
    s.close()


PX = {"AAA": 20.0, "BBB": 50.0, "CCC": 10.0, "THIN": 3.0}           # the insiders pay the bar's price


def listed(store, tickers, ipo="2010-01-04", delisted=None):
    """listing_status rows: the backtest's tradable mask (and so the live list) needs a listed name."""
    store.insert("listing_status", pd.DataFrame([
        {"symbol": t, "name": t, "exchange": "NYSE", "asset_type": "Stock", "ipo_date": pd.Timestamp(ipo).date(),
         "delisting_date": pd.Timestamp(delisted).date() if delisted else None,
         "status": "Delisted" if delisted else "Active", "fetched_at": pd.Timestamp.now()} for t in tickers]))


def insider(store, ticker, owner, usd, day, code="P", price=None, trans_day=None):
    price = price or PX[ticker]
    store.insert("insider_tx", pd.DataFrame([{
        "accession": f"{ticker}-{owner}-{day}-{usd}", "ticker": ticker, "issuer_cik": "1",
        "filing_date": pd.Timestamp(day).date(), "trans_date": pd.Timestamp(trans_day or day).date(),
        "owner_name": owner, "relationship": "isOfficer", "officer_title": "CEO",
        "trans_code": code, "acq_disp": "A", "shares": usd / price, "price": price,
        "value_usd": float(usd), "shares_after": 0.0, "source": "test", "fetched_at": pd.Timestamp.now()}]))


def test_one_buyer_on_each_of_two_days_is_not_a_cluster(store):
    """EU, DUOT, DFDV in the first paper week: two days merged made a 'cluster' the backtest never saw."""
    insider(store, "AAA", "X", 100_000, MON)
    insider(store, "AAA", "Y", 10_000, TUE)
    insider(store, "BBB", "X", 150_000, MON)             # $300k over two days, $150k on each
    insider(store, "BBB", "X", 150_000, TUE)
    insider(store, "CCC", "X", 5_000, TUE)               # a real cluster, both on Tuesday
    insider(store, "CCC", "Y", 5_000, TUE)
    df = signals_insider.candidates(store, TUE, 2, SESSIONS)
    assert list(df["ticker"]) == ["CCC"] and df.iloc[0]["kind"] == "cluster" and df.iloc[0]["n_buyers"] == 2


def test_a_single_trade_above_50m_is_not_counted(store):
    insider(store, "AAA", "X", 60_000_000, TUE)          # a parsing error in the backtest's eyes
    insider(store, "BBB", "X", 60_000_000, TUE)
    insider(store, "BBB", "X", 300_000, TUE)             # the same filing day also has a real purchase
    df = signals_insider.candidates(store, TUE, 2, SESSIONS)
    assert list(df["ticker"]) == ["BBB"] and df.iloc[0]["buy_usd"] == pytest.approx(300_000)


def test_monday_window_holds_friday_and_not_thursday(store):
    insider(store, "AAA", "X", 400_000, FRI)
    insider(store, "BBB", "X", 400_000, "2026-09-17")
    df = signals_insider.candidates(store, MON, 2, SESSIONS)
    assert list(df["ticker"]) == ["AAA"]
    assert df.iloc[0]["trigger_filing_day"] == FRI.date() and df.iloc[0]["lag_sessions"] == 1
    assert signals_insider.window_sessions(MON.date(), 2, SESSIONS) == [FRI.date(), MON.date()]
    # a holiday is skipped the same way: Tuesday 9/08 looks back to Friday 9/04
    assert signals_insider.window_sessions(dt.date(2026, 9, 8), 2, SESSIONS) == [dt.date(2026, 9, 4), dt.date(2026, 9, 8)]


def test_trigger_is_the_earliest_qualifying_day_and_the_latest_one_ranks(store):
    """Owner, S47 addendum (a): the backtest bought after Monday's filing and held through Tuesday's,
    so an entry made tonight is a session late even though Tuesday qualifies too. The ordering is
    what it was: the most recent qualifying day's amount."""
    insider(store, "AAA", "X", 900_000, MON)
    insider(store, "AAA", "X", 300_000, TUE)
    insider(store, "BBB", "X", 500_000, MON)
    insider(store, "BBB", "Y", 1_000, TUE)               # Tuesday does not qualify by itself: Monday is the trigger
    df = signals_insider.candidates(store, TUE, 2, SESSIONS).set_index("ticker")
    assert df.loc["AAA", "trigger_filing_day"] == MON.date() and df.loc["AAA", "lag_sessions"] == 1
    assert df.loc["AAA", "last_filing"] == TUE.date() and df.loc["AAA", "buy_usd"] == pytest.approx(300_000)
    assert df.loc["BBB", "trigger_filing_day"] == MON.date() and df.loc["BBB", "lag_sessions"] == 1
    assert list(df.index) == ["BBB", "AAA"]              # larger purchase first, as the backtest fills its slots


def test_adv_range_is_tested_on_the_filing_day(store):
    """MX, filed 2026-09-25 under the floor and over it on 09-28: not an entry in the backtest, not one here."""
    insider(store, "THIN", "X", 400_000, MON)
    df = signals_insider.candidates(store, TUE, 2, SESSIONS).set_index("ticker")
    assert not df.loc["THIN", "eligible"] and df.loc["THIN", "reason"] == "adv_below_floor"
    assert df.loc["THIN", "adv20"] == pytest.approx(2.7e6)
    insider(store, "THIN", "X", 300_000, TUE)            # a filing on the day it is liquid enough is an entry
    df = signals_insider.candidates(store, TUE, 2, SESSIONS).set_index("ticker")
    assert df.loc["THIN", "eligible"] and df.loc["THIN", "trigger_filing_day"] == TUE.date()
    assert df.loc["THIN", "adv20"] == pytest.approx((19 * 2.7e6 + 27e6) / 20)


def test_an_eligible_filing_day_is_preferred_to_a_later_one_outside_the_range(store):
    # Monday $27M: 20-day mean $3.9M, inside. Tuesday $2.7B: the mean jumps over the $100M ceiling.
    store.con.execute("UPDATE bars SET volume = 9000000 WHERE ticker = 'THIN' AND trade_date = ?", [MON.date()])
    store.con.execute("UPDATE bars SET volume = 900000000 WHERE ticker = 'THIN' AND trade_date = ?", [TUE.date()])
    insider(store, "THIN", "X", 400_000, MON)
    insider(store, "THIN", "X", 900_000, TUE)
    df = signals_insider.candidates(store, TUE, 2, SESSIONS).set_index("ticker")
    assert df.loc["THIN", "eligible"] and df.loc["THIN", "trigger_filing_day"] == MON.date() and df.loc["THIN", "lag_sessions"] == 1
    assert df.loc["THIN", "buy_usd"] == pytest.approx(400_000)


def test_without_a_calendar_the_panel_bar_dates_are_the_sessions(store):
    insider(store, "AAA", "X", 400_000, FRI)
    df = signals_insider.candidates(store, MON, 2)
    assert list(df["ticker"]) == ["AAA"] and df.iloc[0]["lag_sessions"] == 1


def test_every_candidate_is_a_backtest_event_in_the_window(store):
    insider(store, "AAA", "X", 100_000, MON)
    insider(store, "AAA", "Y", 200_000, TUE)
    insider(store, "BBB", "X", 250_000, MON)
    insider(store, "CCC", "X", 1_000, MON)
    insider(store, "CCC", "Y", 1_000, MON)
    insider(store, "CCC", "Z", 900_000, TUE, code="S")   # a sale never makes a buyer
    ev = InsiderBuys().events(store, "2026-09-21", "2026-09-22")
    ev = ev[ev["date"].isin([MON, TUE])]
    df = signals_insider.candidates(store, TUE, 2, SESSIONS)
    assert set(df["ticker"]) == set(ev["ticker"]) == {"BBB", "CCC"}
    assert set(zip(df["ticker"], df["trigger_filing_day"])) <= {(t, d.date()) for t, d in zip(ev["ticker"], ev["date"])}


def test_entry_kind():
    assert signals_insider.entry_kind(0, False) == "on_time"
    assert signals_insider.entry_kind(1, False) == "late"
    assert signals_insider.entry_kind(0, True) == "retry" and signals_insider.entry_kind(1, True) == "retry"


@pytest.fixture
def con(tmp_path):
    c = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(c)
    yield c
    c.close()


def test_insider_targets_classify_each_name(store, con):
    insider(store, "AAA", "X", 400_000, TUE)             # filed today
    insider(store, "BBB", "X", 300_000, MON)             # filed yesterday, first seen tonight
    insider(store, "CCC", "X", 500_000, MON)             # ordered last night, the order expired unfilled
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, dry_run, status, filled_qty)
                   VALUES ('insider|2026-09-21|CCC|buy', 'insider', '2026-09-21', 'CCC', 'buy', 10, FALSE, 'expired', 0)""")
    names, retries, info = insider_targets(store, TUE, con, SESSIONS)
    assert names == ["CCC", "AAA", "BBB"] and retries == {"CCC"}
    assert info["AAA"] == {"trigger_filing_day": TUE.date(), "lag_sessions": 0, "entry_kind": "on_time"}
    assert info["BBB"] == {"trigger_filing_day": MON.date(), "lag_sessions": 1, "entry_kind": "late"}
    assert info["CCC"] == {"trigger_filing_day": MON.date(), "lag_sessions": 1, "entry_kind": "retry"}


def test_insider_targets_skip_a_name_that_filled(store, con):
    insider(store, "AAA", "X", 400_000, MON)
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, dry_run, status, filled_qty)
                   VALUES ('insider|2026-09-21|AAA|buy', 'insider', '2026-09-21', 'AAA', 'buy', 10, FALSE, 'filled', 10)""")
    assert insider_targets(store, TUE, con, SESSIONS) == ([], set(), {})


def test_eval_progress_shows_total_and_on_time(con):
    con.execute("INSERT INTO agent_books VALUES ('insider', 30000, 30000, 20, '2026-09-21', now())")
    for i, kind in enumerate(["on_time", "on_time", "late", "retry", None]):
        con.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, status, entry_kind)
                       VALUES (?, 'insider', ?, 1, 'closed', ?)""", [f"l{i}", f"T{i}", kind])
    con.execute("INSERT INTO agent_lots (lot_id, book, ticker, qty, status, entry_kind) VALUES ('o', 'insider', 'O', 1, 'open', 'on_time')")
    line = _eval_progress(con)
    assert "insider 平仓 5/200,其中按时入场 2(或" in line
    assert "long 平仓 0/100(或" in line                  # the classification is the insider book's


def test_window_sessions_take_timestamps_too():
    assert signals_insider.window_sessions(MON.date(), 2, [pd.Timestamp(d) for d in SESSIONS]) == [FRI.date(), MON.date()]


def add_bars(store, ticker, px, vol=500_000.0, last=TUE.date()):
    store.upsert_bars(pd.DataFrame([{"ticker": ticker, "trade_date": d, "open": px, "high": px, "low": px, "close": px,
                                     "adj_close": px, "volume": vol, "source": "test", "fetched_at": pd.Timestamp.now()}
                                    for d in SESSIONS if d <= last]))


def test_trigger_day_must_be_listed_as_in_the_backtest(store):
    """Owner, S47 addendum (c): the trigger day passes the backtest's own listing mask (Market.listed)."""
    from agent import s11_insider_wide
    from agent.books import data
    assert signals_insider.listed_mask is s11_insider_wide.listed_mask is data.listed_mask
    add_bars(store, "NEW", 20.0)                         # trades, but listing_status has no row for it
    add_bars(store, "OLD", 20.0)
    listed(store, ["OLD"], ipo="2001-01-02", delisted=FRI)   # delisted on Friday: Friday is in, Monday out
    insider(store, "NEW", "X", 400_000, MON, price=20.0)
    insider(store, "OLD", "X", 400_000, MON, price=20.0)
    insider(store, "OLD", "Y", 300_000, FRI, price=20.0)
    df = signals_insider.candidates(store, MON, 2, SESSIONS).set_index("ticker")
    assert not df.loc["NEW", "eligible"] and df.loc["NEW", "reason"] == "not_listed"
    assert df.loc["OLD", "eligible"] and df.loc["OLD", "trigger_filing_day"] == FRI.date() and df.loc["OLD", "lag_sessions"] == 1
    assert df.loc["OLD", "last_filing"] == FRI.date()   # Monday is no entry, so Friday also sets the order


def test_a_purchase_priced_off_the_market_does_not_count_nct(store):
    """S47 addendum item 13: NCT filed a $650k buy at $0.40 on 2026-09-25; the stock closed at $4.43."""
    from agent.books.data import insider_flows
    for d, lo, hi in [("2026-09-23", 5.35, 6.1044), ("2026-09-24", 4.53, 5.36), ("2026-09-25", 4.0, 4.5999)]:
        store.upsert_bars(pd.DataFrame([{"ticker": "NCT", "trade_date": pd.Timestamp(d).date(), "open": hi, "high": hi, "low": lo,
                                         "close": 4.43, "adj_close": 4.43, "volume": 1e6, "source": "test",
                                         "fetched_at": pd.Timestamp.now()}]))
    insider(store, "NCT", "Zhu Muchun", 650_000, "2026-09-25", price=0.40)
    f = insider_flows(store, "2026-09-25").set_index("ticker")
    assert f.loc["NCT", "n_buyers"] == 0 and f.loc["NCT", "buy_usd"] == 0
    assert InsiderBuys().events(store, "2026-09-25", "2026-09-25").empty          # the backtest drops it too: one code path
    insider(store, "NCT", "Other", 300_000, "2026-09-25", price=4.43)           # a buy at the market's price still counts
    ev = InsiderBuys().events(store, "2026-09-25", "2026-09-25")
    assert list(ev["ticker"]) == ["NCT"] and ev.iloc[0]["strength"] == pytest.approx(300_000)


def test_price_band_edges_the_bar_used_and_no_bar(store):
    from agent.books.data import insider_flows
    # AAA trades at 20.00 flat: a purchase counts between 16.00 and 25.00, both ends included
    insider(store, "AAA", "IN_LO", 100_000, MON, price=16.0)
    insider(store, "AAA", "IN_HI", 100_000, MON, price=25.0)
    insider(store, "AAA", "OUT_LO", 100_000, MON, price=15.99)
    insider(store, "AAA", "OUT_HI", 100_000, MON, price=25.01)
    insider(store, "AAA", "WEEKEND", 100_000, MON, price=20.0, trans_day="2026-09-19")   # a Saturday: Friday's bar
    insider(store, "AAA", "SELLER", 5_000_000, MON, code="S", price=1.0)                 # sales are not checked
    insider(store, "ZZZ", "NOBAR", 900_000, MON, price=20.0)                              # no bar at all: never counts
    f = insider_flows(store, MON.date().isoformat()).set_index("ticker")
    assert f.loc["AAA", "n_buyers"] == 3 and f.loc["AAA", "buy_usd"] == pytest.approx(300_000)
    assert f.loc["AAA", "sell_usd"] == pytest.approx(5_000_000)
    assert f.loc["ZZZ", "n_buyers"] == 0 and f.loc["ZZZ", "buy_usd"] == 0


def test_a_transaction_dated_after_its_filing_is_priced_by_the_filing_day(store):
    """No bar after the filing date is ever read: a mistyped future trans_date must not reach tomorrow's price."""
    from agent.books.data import insider_flows
    store.con.execute("UPDATE bars SET low = 100, high = 100 WHERE ticker = 'AAA' AND trade_date = ?", [TUE.date()])
    insider(store, "AAA", "X", 400_000, MON, price=100.0, trans_day=TUE)
    assert insider_flows(store, MON.date().isoformat()).set_index("ticker").loc["AAA", "n_buyers"] == 0   # Monday's 20.00


def test_a_late_entry_left_unfilled_is_retried_once_three_sessions_back(store, con):
    """Owner, S47 addendum (d): Friday's filing reached the panel on Monday morning, Monday's order
    expired; Tuesday's window for that name still holds Friday. Every other name looks back two sessions."""
    insider(store, "AAA", "X", 400_000, FRI)
    insider(store, "BBB", "X", 400_000, FRI)             # never ordered: on Tuesday Friday is out of its window
    insider(store, "CCC", "X", 400_000, FRI)             # two unfilled orders: not chased
    for as_of, t in [("2026-09-21", "AAA"), ("2026-09-18", "CCC"), ("2026-09-21", "CCC")]:
        con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, dry_run, status, filled_qty)
                       VALUES (?, 'insider', ?, ?, 'buy', 10, FALSE, 'expired', 0)""", [f"insider|{as_of}|{t}|buy", as_of, t])
    names, retries, info = insider_targets(store, TUE, con, SESSIONS)
    assert names == ["AAA"] and retries == {"AAA"}
    assert info["AAA"] == {"trigger_filing_day": FRI.date(), "lag_sessions": 2, "entry_kind": "retry"}


def test_no_filings_or_no_calendar_give_no_targets(store, con):
    """The insider branch runs before the other books' orders are sent: an empty day must not raise."""
    assert insider_targets(store, TUE, con, SESSIONS) == ([], set(), {})       # insider_tx is empty
    insider(store, "AAA", "X", 400_000, TUE)
    assert insider_targets(store, TUE, con, []) == ([], set(), {})             # no sessions to look back over


def test_eval_progress_on_a_ledger_without_the_columns(tmp_path):
    c = ledger.connect(str(tmp_path / "old.db"))
    c.execute("""CREATE TABLE agent_books (book VARCHAR PRIMARY KEY, alloc_usd DOUBLE, cash_usd DOUBLE, max_positions INT,
                                           started DATE, updated TIMESTAMP)""")
    c.execute("""CREATE TABLE agent_lots (lot_id VARCHAR PRIMARY KEY, book VARCHAR, ticker VARCHAR, qty DOUBLE, status VARCHAR,
                                          entry_order VARCHAR)""")
    c.execute("INSERT INTO agent_lots VALUES ('l', 'insider', 'T', 1, 'closed', NULL)")
    line = _eval_progress(c)
    assert "insider 平仓 1/200(或" in line and "按时入场" not in line
    c.close()


def test_eval_progress_counts_only_entries_ordered_from_evaluate_from(con, tmp_path):
    """Owner, S47 addendum (f): the evaluation starts with the first run on corrected data; the
    9/28 orders sent by the old code (DMRA, LLYVK, GPI) fill on 9/29 but do not count."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("evaluate_from: 2026-09-29\nevaluate_at:\n  insider:\n    n_closed_lots: 200\n    or_date: 2027-06-30\n")
    for as_of, t, kind in [("2026-09-28", "DMRA", None), ("2026-09-29", "X", "on_time"), ("2026-09-30", "Y", "late")]:
        coid = f"insider|{as_of}|{t}|buy"
        con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, dry_run, entry_kind)
                       VALUES (?, 'insider', ?, ?, 'buy', 1, FALSE, ?)""", [coid, as_of, t, kind])
        con.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, status, entry_order, entry_kind)
                       VALUES (?, 'insider', ?, 1, 'closed', ?, ?)""", [f"l{t}", t, coid, kind])
    con.execute("INSERT INTO agent_lots (lot_id, book, ticker, qty, status) VALUES ('lz', 'insider', 'Z', 1, 'closed')")  # no entry order
    line = _eval_progress(con, str(cfg))
    assert line.startswith("评估点(预注册,之前不做判决;只计 2026-09-29 起下单的入场):")
    assert "insider 平仓 2/200,其中按时入场 1(或" in line
    assert "insider 平仓 4/200,其中按时入场 1(或" in _eval_progress(con)       # without the key: every closed lot, as before


class Recording:
    """A ledger connection that logs each statement and can make ALTER fail, or do nothing."""
    def __init__(self, con, alter=None):
        self.con, self.alter, self.sql = con, alter, []

    def execute(self, sql, *args):
        self.sql.append(sql)
        if sql.startswith("ALTER") and self.alter == "fail":
            raise duckdb.IOException("IO Error: No space left on device")
        if sql.startswith("ALTER") and self.alter == "noop":
            return None
        return self.con.execute(sql, *args)


def test_schema_alters_only_what_is_missing_and_never_hides_a_failure(con):
    """Owner, S47 addendum (e): sync_fills needs the columns, so a failed ALTER must stop the run loudly."""
    rec = Recording(con)
    ledger.ensure_schema(rec)
    assert not [s for s in rec.sql if s.startswith("ALTER")]              # all present: nothing altered
    con.execute("ALTER TABLE agent_lots DROP COLUMN entry_kind")
    with pytest.raises(duckdb.IOException):
        ledger.ensure_schema(Recording(con, "fail"))
    with pytest.raises(RuntimeError, match="agent_lots.entry_kind"):
        ledger.ensure_schema(Recording(con, "noop"))
    rec = Recording(con)
    ledger.ensure_schema(rec)
    assert [s for s in rec.sql if s.startswith("ALTER")] == ["ALTER TABLE agent_lots ADD COLUMN entry_kind VARCHAR"]


def test_nightly_run_passes_the_calendar_and_writes_the_classification_on_the_order(store, tmp_path, monkeypatch):
    """execute.main, dry run, insider book only: the broker's calendar reaches insider_targets, its three
    results are unpacked and the planned buy carries its classification (nothing is sent)."""
    import json
    import sys
    import types
    from agent import execute
    from agent.books.data import Market

    class Broker:
        def account(self):
            return {"equity": "100000", "cash": "100000"}

        def calendar(self, start, end):
            return list(SESSIONS)

        def positions(self):
            return []

        def open_orders(self):
            return []

        def order_by_client_id(self, coid):
            return None

    class Now(dt.datetime):                              # Tuesday 17:00 ET: the 16:10 PT run after that session
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 22, 17, 0, tzinfo=tz)

    def market(_store, _start):
        close = store.bars_wide("close")
        adv = (close * store.bars_wide("volume")).rolling(20).mean()
        return Market(close, close, close, adv, close.notna(), pd.Series(1.0, index=close.index), {})

    seen = {}
    real = execute.insider_targets

    def spy(store_, day, con, sessions, *a, **k):
        seen["sessions"] = sessions
        return real(store_, day, con, sessions, *a, **k)

    monkeypatch.setattr(execute.broker_mod, "from_env", Broker)
    monkeypatch.setattr(execute, "dt", types.SimpleNamespace(date=dt.date, time=dt.time, timedelta=dt.timedelta, datetime=Now))
    monkeypatch.setattr(execute, "PanelStore", lambda read_only=False: store)
    monkeypatch.setattr(execute, "load_market", market)
    monkeypatch.setattr(execute, "insider_targets", spy)
    monkeypatch.setattr(sys, "argv", ["execute", "--no-update", "--book", "insider", "--optradar-db", str(tmp_path / "l.db"),
                                      "--out-dir", str(tmp_path)])
    insider(store, "AAA", "X", 400_000, TUE)
    insider(store, "BBB", "X", 300_000, MON)
    assert execute.main() == 0
    assert seen["sessions"] == SESSIONS
    out = json.load(open(tmp_path / "exec_2026-09-22.json"))
    buys = {o["ticker"]: o for o in out["orders"] if o["side"] == "buy"}
    assert set(buys) == {"AAA", "BBB"} and all(o["status"] == "dry_run" for o in buys.values())
    assert (buys["AAA"]["entry_kind"], buys["AAA"]["lag_sessions"], buys["AAA"]["trigger_filing_day"]) == ("on_time", 0, "2026-09-22")
    assert (buys["BBB"]["entry_kind"], buys["BBB"]["lag_sessions"], buys["BBB"]["trigger_filing_day"]) == ("late", 1, "2026-09-21")
