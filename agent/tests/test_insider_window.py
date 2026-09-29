"""Insider candidates as the backtest defines them (S47): one filing day at a time, the $50M cap,
a window in trading days — and the entry classification that the evaluation groups by."""
import datetime as dt

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
    for t, px, vol in [("AAA", 20.0, 500_000), ("BBB", 50.0, 1_000_000), ("CCC", 10.0, 2_000_000)]:
        for d in SESSIONS:
            if d > TUE.date():
                break
            rows.append({"ticker": t, "trade_date": d, "open": px, "high": px, "low": px,
                         "close": px, "adj_close": px, "volume": float(vol), "source": "test",
                         "fetched_at": pd.Timestamp.now()})
    s.upsert_bars(pd.DataFrame(rows))
    yield s
    s.close()


def insider(store, ticker, owner, usd, day, code="P"):
    store.insert("insider_tx", pd.DataFrame([{
        "accession": f"{ticker}-{owner}-{day}-{usd}", "ticker": ticker, "issuer_cik": "1",
        "filing_date": pd.Timestamp(day).date(), "trans_date": pd.Timestamp(day).date(),
        "owner_name": owner, "relationship": "isOfficer", "officer_title": "CEO",
        "trans_code": code, "acq_disp": "A", "shares": usd / 10.0, "price": 10.0,
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


def test_trigger_is_the_most_recent_qualifying_day_and_ranks_by_its_amount(store):
    insider(store, "AAA", "X", 900_000, MON)
    insider(store, "AAA", "X", 300_000, TUE)
    insider(store, "BBB", "X", 500_000, MON)
    insider(store, "BBB", "Y", 1_000, TUE)               # Tuesday does not qualify by itself: Monday is the trigger
    df = signals_insider.candidates(store, TUE, 2, SESSIONS).set_index("ticker")
    assert df.loc["AAA", "trigger_filing_day"] == TUE.date() and df.loc["AAA", "lag_sessions"] == 0
    assert df.loc["AAA", "buy_usd"] == pytest.approx(300_000)
    assert df.loc["BBB", "trigger_filing_day"] == MON.date() and df.loc["BBB", "lag_sessions"] == 1
    assert list(df.index) == ["BBB", "AAA"]              # larger purchase first, as the backtest fills its slots


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
