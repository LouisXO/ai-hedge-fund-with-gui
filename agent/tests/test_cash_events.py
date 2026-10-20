"""The broker's DIV and FEE activities booked into the books' cash (agent/cash_events.py), the corporate-actions
entitlement (agent/dividends.py), the auction basis that takes the booked cash out again, and the readers that must not
count a dividend twice. A temp ledger and fake brokers / fetchers only; no network, no panel."""
import datetime as dt
import io
import json
import urllib.error

import pandas as pd
import pytest

from agent import attribution, auction_basis, cash_events, dividends, evaluate, execute, health, ledger
from agent.broker import alpaca
from agent.broker.alpaca import BrokerError, PaperBroker

D = dt.date
SESSIONS = [d.date() for d in pd.bdate_range("2026-09-14", "2026-10-30")]


def fee(day, sub, usd, aid=None):
    nxt = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return {"id": aid or f"{day.replace('-', '')}000000000::fee-{sub}", "activity_type": "FEE", "activity_sub_type": sub,
            "date": day, "created_at": f"{nxt}T00:07:08Z", "net_amount": str(usd), "status": "executed",
            "description": f"{sub} fee for proceed of $1000 on {day} by PA1"}


def div(day, sym, rate, qty, rec, aid=None, desc=None):
    return {"id": aid or f"{day.replace('-', '')}000000000::div-{sym}", "activity_type": "DIV", "activity_sub_type": "CDIV",
            "date": day, "created_at": f"{day}T16:38:17Z", "net_amount": str(round(rate * qty, 2)), "symbol": sym,
            "per_share_amount": str(rate), "status": "executed",
            "description": desc if desc is not None else f"Cash DIV @ {rate}, Pos QTY: {qty}, Rec Date: {rec}"}


class Broker:
    """activities() as the paper account answers: filtered by type when asked, every type otherwise."""

    def __init__(self, acts, fail=False, fail_unfiltered=False):
        self.acts, self.fail, self.fail_unfiltered, self.calls = list(acts), fail, fail_unfiltered, []

    def activities(self, types, after):
        self.calls.append((types, after))
        if self.fail or (types is None and self.fail_unfiltered):
            raise BrokerError("GET /account/activities -> no answer: URLError")
        return [dict(a) for a in self.acts if types is None or a["activity_type"] in types]


@pytest.fixture
def con(tmp_path):
    c = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(c)
    execute.ensure_books(c)
    yield c
    c.close()


def lot(con, book, ticker, qty, entry, exit_=None, dry=False):
    coid = f"{book}|{entry}|{ticker}|buy"
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, status, filled_qty, filled_avg_px,
                                             filled_at, dry_run) VALUES (?, ?, ?, ?, 'buy', ?, 'filled', ?, 10.0, ?, ?)""",
                [coid, book, entry, ticker, qty, qty, f"{entry} 09:31:00", dry])
    con.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, entry_day, exit_day, status, entry_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [f"{book}|{ticker}|{entry}", book, ticker, qty, entry, exit_, "closed" if exit_ else "open", coid])


def fill(con, book, ticker, side, qty, px, day, dry=False):
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, status, filled_qty, filled_avg_px,
                                             filled_at, dry_run) VALUES (?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?)""",
                [f"{book}|{day}|{ticker}|{side}|x", book, day, ticker, side, qty, qty, px, f"{day} 09:30:05", dry])


def nav(con, days, books=("long", "insider", "core")):
    for d in days:
        for b in books:
            con.execute("INSERT INTO agent_book_nav VALUES (?, ?, 1000, 0, 1000, 0, 90000)", [d, b])


def cash(con, book):
    return con.execute("SELECT cash_usd FROM agent_books WHERE book = ?", [book]).fetchone()[0]


# ---------------------------------------------------------------- allocation (pure) ----
def test_cents_add_up_exactly_by_largest_remainder_and_ties_go_by_book_name():
    s = cash_events.split_cents(-0.39, {"long": 16660.77, "insider": 1888.52})
    assert s == {"insider": -0.04, "long": -0.35} and round(sum(s.values()), 2) == -0.39
    assert cash_events.split_cents(-0.01, {"long": 1.0, "insider": 1.0}) == {"insider": -0.01, "long": 0.0}   # tie: by name
    assert cash_events.split_cents(0.10, {"a": 1, "b": 1, "c": 1}) == {"a": 0.04, "b": 0.03, "c": 0.03}
    for amt in (-0.07, 68.34, 0.01, -12.35):
        w = {"long": 3.3, "insider": 1.1, "core": 0.7}
        assert round(sum(cash_events.split_cents(amt, w).values()), 2) == amt
    assert cash_events.split_cents(-0.3, {"long": 0.0, "insider": 0.0}) is None and cash_events.split_cents(1.0, {}) is None


def test_description_parsing_and_the_cash_day_rule():
    d = "Cash DIV @ 0.51, Pos QTY: 134, Rec Date: 2026-09-28"
    assert cash_events.parse_record_date(d) == D(2026, 9, 28) and cash_events.parse_pos_qty(d) == 134.0
    assert cash_events.parse_record_date("REG fee for proceed of $1 on 2026-09-30") is None
    # DIV credited in the morning: the same session; FEE posted in the evening: the next session (Friday -> Monday)
    assert cash_events.cash_day("DIV", D(2026, 10, 14), SESSIONS, D(2026, 10, 30)) == D(2026, 10, 14)
    assert cash_events.cash_day("FEE", D(2026, 10, 16), SESSIONS, D(2026, 10, 30)) == D(2026, 10, 19)
    # never later than the run that books it: tonight's late run books today's fee into today's row
    assert cash_events.cash_day("FEE", D(2026, 10, 20), SESSIONS, D(2026, 10, 20)) == D(2026, 10, 20)
    # a day before the calendar: weekdays
    assert cash_events.cash_day("FEE", D(2026, 9, 4), SESSIONS, D(2026, 10, 30)) == D(2026, 9, 7)


# ---------------------------------------------------------------- DIV holder by record date ----
def test_dividend_goes_to_the_lot_held_over_the_record_date_even_when_sold_before_the_pay_date(con):
    lot(con, "long", "BZ", 134, D(2026, 9, 22), D(2026, 9, 30))            # BZ: record 9/28, sold 9/30, paid 10/14
    lot(con, "insider", "BZ", 50, D(2026, 9, 28), D(2026, 10, 2))           # bought ON the record date: not a holder
    lot(con, "long", "MRP", 70, D(2026, 9, 30))                             # open
    nav(con, [D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15)])
    b = Broker([div("2026-10-14", "BZ", 0.51, 134, "2026-09-28"), div("2026-10-15", "MRP", 0.79, 70, "2026-10-05"),
                div("2026-10-15", "XYZ", 1.0, 10, "2026-10-01")])          # no lot: unallocated, not booked
    long0, ins0 = cash(con, "long"), cash(con, "insider")
    r = cash_events.sync(con, b, SESSIONS, D(2026, 10, 16))
    assert [x["ticker"] for x in r["booked"]] == ["BZ", "MRP"] and r["booked"][0]["books"] == {"long": 68.34}
    assert [u["ticker"] for u in r["unallocated"]] == ["XYZ"] and "no lot of XYZ" in r["unallocated"][0]["why"]
    assert cash(con, "long") == pytest.approx(long0 + 123.64) and cash(con, "insider") == ins0
    rows = con.execute("SELECT ticker, book, record_date, cash_day, basis, note FROM agent_cash_events ORDER BY ticker").fetchall()
    assert rows == [("BZ", "long", D(2026, 9, 28), D(2026, 10, 14), "record_date_holder", None),
                    ("MRP", "long", D(2026, 10, 5), D(2026, 10, 15), "record_date_holder", None)]


def test_a_ticker_in_two_books_over_the_record_date_shares_by_qty_and_a_pos_qty_mismatch_is_noted(con):
    lot(con, "long", "AB", 30, D(2026, 9, 22))
    lot(con, "insider", "AB", 10, D(2026, 9, 23))
    r = cash_events.sync(con, Broker([div("2026-10-14", "AB", 0.25, 45, "2026-10-01")]), SESSIONS, D(2026, 10, 14))
    assert r["booked"][0]["books"] == {"insider": 2.81, "long": 8.44}           # 11.25 by 30:10
    assert r["notes"] == ["AB 2026-10-14: broker Pos QTY 45, lots 40"]
    assert {x[0] for x in con.execute("SELECT basis FROM agent_cash_events").fetchall()} == {"record_date_holder_by_qty"}


NOV = [d.date() for d in pd.bdate_range("2026-09-14", "2026-11-30") if d.date() != D(2026, 11, 26)]     # Thanksgiving closed


def test_a_weekend_record_date_pays_the_seller_on_the_ex_date_not_its_buyer(con):
    lot(con, "long", "XYZ", 100, D(2026, 10, 1), D(2026, 10, 30))           # sold at the open of Friday, the T+1 ex-date
    lot(con, "insider", "XYZ", 40, D(2026, 10, 30))                         # bought that Friday: settles Monday, after Saturday
    r = cash_events.sync(con, Broker([div("2026-11-16", "XYZ", 0.5, 100, "2026-10-31")]), NOV, D(2026, 11, 16))
    assert r["booked"][0]["books"] == {"long": 50.0} and r["unallocated"] == [] and r["notes"] == []
    assert cash_events.record_cut(D(2026, 10, 31), NOV) == D(2026, 10, 30)
    assert cash_events.record_cut(D(2026, 11, 26), NOV) == D(2026, 11, 25)  # a holiday: the session before
    assert cash_events.record_cut(D(2026, 9, 28), NOV) == D(2026, 9, 28)    # a session: itself
    assert cash_events.record_cut(D(2026, 9, 6), NOV) == D(2026, 9, 4)      # before the calendar: weekdays


def test_a_partly_sold_lot_counts_the_shares_it_held_at_the_ex_date_whatever_it_sells_later(con):
    nav(con, [D(2026, 10, 20)])
    lot(con, "long", "XYZ", 300, D(2026, 10, 1))                             # 300 over the ex-date 10/05
    lot(con, "insider", "ABC", 300, D(2026, 9, 28))                          # 100 sold before the ex-date, 200 after it
    acts = [ca("x1", "XYZ", 0.5, "2026-10-05", "2026-10-20"), ca("a1", "ABC", 0.5, "2026-10-05", "2026-10-20")]

    def usd():
        rows = dividends.update(con, None, fetch=lambda *a: acts)
        return rows.set_index("ticker")["usd"].to_dict()
    fill(con, "insider", "ABC", "sell", 100, 10.0, "2026-10-01")
    con.execute("UPDATE agent_lots SET qty = 200, sold_qty = 100 WHERE ticker = 'ABC'")             # as _apply_sell leaves it
    assert usd() == {"XYZ": 150.0, "ABC": 100.0}
    fill(con, "long", "XYZ", "sell", 100, 10.0, "2026-10-09")                # a DAY sell fills a third: the open lot keeps 200
    con.execute("UPDATE agent_lots SET qty = 200, sold_qty = 100 WHERE ticker = 'XYZ'")
    assert usd() == {"XYZ": 150.0, "ABC": 100.0}
    fill(con, "long", "XYZ", "sell", 200, 10.0, "2026-10-12")                # the rest: closed, qty = all it sold
    fill(con, "insider", "ABC", "sell", 200, 10.0, "2026-10-08")
    con.execute("UPDATE agent_lots SET qty = 300, sold_qty = 300, exit_day = '2026-10-12', status = 'closed' WHERE ticker = 'XYZ'")
    con.execute("UPDATE agent_lots SET qty = 300, sold_qty = 300, exit_day = '2026-10-08', status = 'closed' WHERE ticker = 'ABC'")
    assert usd() == {"XYZ": 150.0, "ABC": 100.0}
    assert dict(con.execute("SELECT ticker, qty FROM agent_dividends").fetchall()) == {"XYZ": 300.0, "ABC": 200.0}
    r = cash_events.sync(con, Broker([div("2026-10-20", "ABC", 0.5, 200, "2026-10-05")]), SESSIONS, D(2026, 10, 20))
    assert r["booked"][0]["books"] == {"insider": 100.0} and r["notes"] == []        # 200 held at the record date, as the broker says


def test_a_dividend_the_broker_posts_late_counts_from_the_day_it_posted_it(con):
    lot(con, "long", "STX", 2, D(2026, 9, 22))
    nav(con, [D(2026, 10, 7), D(2026, 10, 26), D(2026, 10, 27)])
    late = dict(div("2026-10-07", "STX", 0.74, 2, "2026-09-24"), created_at="2026-10-27T16:01:00Z")
    r = cash_events.sync(con, Broker([late]), SESSIONS, D(2026, 10, 28))
    assert r["booked"][0]["cash_day"] == "2026-10-27" and r["booked"][0]["restated_rows"] == 1
    got = dict(con.execute("SELECT as_of, cash_usd FROM agent_book_nav WHERE book = 'long'").fetchall())
    assert got == pytest.approx({D(2026, 10, 7): 1000.0, D(2026, 10, 26): 1000.0, D(2026, 10, 27): 1001.48})
    assert cash_events.created_day({"created_at": "2026-10-01T00:07:08Z"}) == D(2026, 9, 30)   # an evening fee: its trade day, ET
    assert cash_events.created_day({}) is None and cash_events.created_day({"created_at": "?"}) is None
    assert cash_events.cash_day("FEE", D(2026, 9, 30), SESSIONS, D(2026, 10, 30), D(2026, 9, 30)) == D(2026, 10, 1)


def test_only_executed_activities_are_booked_and_a_cancel_after_booking_is_listed_not_undone(con):
    lot(con, "long", "MRP", 70, D(2026, 9, 30))
    nav(con, [D(2026, 10, 15), D(2026, 10, 16)])
    paid = div("2026-10-15", "MRP", 0.79, 70, "2026-10-05")
    gone = dict(div("2026-10-16", "MRP", 0.1, 70, "2026-10-06", aid="c1"), status="canceled")
    odd = dict(div("2026-10-16", "MRP", 0.2, 70, "2026-10-06", aid="c2"), status="correct")
    r = cash_events.sync(con, Broker([paid, gone, odd]), SESSIONS, D(2026, 10, 16))
    assert [x["id"] for x in r["booked"]] == [paid["id"]] and cash(con, "long") == pytest.approx(execute.BOOKS["long"]["alloc_usd"] + 55.3)
    assert [(c["id"], c["booked"]) for c in r["canceled"]] == [("c1", False)]
    assert [(u["id"], u["why"]) for u in r["unallocated"]] == [("c2", "status correct")]

    def state():
        return (con.execute("SELECT * FROM agent_books ORDER BY book").fetchall(),
                con.execute("SELECT * FROM agent_book_nav ORDER BY as_of, book").fetchall(),
                con.execute("SELECT * FROM agent_cash_events ORDER BY activity_id, book").fetchall())
    before = state()
    r = cash_events.sync(con, Broker([dict(paid, status="canceled")]), SESSIONS, D(2026, 10, 19))
    assert [(c["id"], c["booked"]) for c in r["canceled"]] == [(paid["id"], True)] and r["booked"] == [] and state() == before
    assert cash_events.summary_line(r).endswith(", canceled after booking 1")


def test_without_rec_date_the_cached_corporate_action_then_the_activity_date_gives_the_record_date(con):
    lot(con, "long", "KB", 100, D(2026, 9, 22), D(2026, 10, 5))
    dividends.ensure_tables(con)
    dividends.upsert_actions(con, [("cash_dividends", {"id": "ca1", "symbol": "KB", "rate": 0.4, "ex_date": "2026-09-30",
                                                       "record_date": "2026-09-30", "payable_date": "2026-10-16"})])
    r = cash_events.sync(con, Broker([div("2026-10-16", "KB", 0.4, 100, None, desc="Cash DIV @ 0.4")]), SESSIONS, D(2026, 10, 16))
    assert r["booked"][0]["books"] == {"long": 40.0} and r["notes"] == ["KB 2026-10-16: record date from the corporate action"]
    # no action cached: the pay date itself, after the lot was sold -> unallocated
    r = cash_events.sync(con, Broker([div("2026-10-20", "KB", 0.4, 100, None, aid="x2", desc="Cash DIV @ 0.4")]), SESSIONS, D(2026, 10, 20))
    assert r["booked"] == [] and "held over the record date 2026-10-20 (activity date)" in r["unallocated"][0]["why"]


# ---------------------------------------------------------------- FEE by the day's fills ----
def test_fees_share_by_the_trade_days_fills_reg_by_sells_cat_by_shares(con):
    fill(con, "long", "A", "sell", 100, 166.6077, "2026-09-30")              # sell notional 16,660.77
    fill(con, "insider", "B", "sell", 10, 188.852, "2026-09-30")             # 1,888.52
    fill(con, "long", "C", "buy", 644, 25.0, "2026-09-30")
    fill(con, "core", "SPY", "buy", 12, 700.0, "2026-09-30", dry=True)        # a dry run is not a fill
    fill(con, "core", "SPY", "buy", 12, 700.0, "2026-10-01")                 # another day
    b = Broker([fee("2026-09-30", "REG", -0.39), fee("2026-09-30", "CAT", -0.03), fee("2026-09-29", "CAT", -0.01),
                fee("2026-09-30", "TAF", -0.10)])
    r = cash_events.sync(con, b, SESSIONS, D(2026, 10, 1))
    got = {x["sub_type"]: x["books"] for x in r["booked"]}
    assert got["REG"] == {"insider": -0.04, "long": -0.35}                   # the buy carries no REG weight
    assert got["CAT"] == {"long": -0.03}                                     # shares 744 : 10: 2.96 + 0.04 cents, the remainder to long
    assert got["TAF"] == {"long": -0.09, "insider": -0.01}                   # any other sub type: notional both sides
    assert [u["date"] for u in r["unallocated"]] == ["2026-09-29"] and "no fill on 2026-09-29" in r["unallocated"][0]["why"]
    assert r["booked"][0]["cash_day"] == "2026-10-01"
    total = con.execute("SELECT sum(net_usd) FROM agent_cash_events").fetchone()[0]
    assert round(total, 2) == -0.52


# ---------------------------------------------------------------- idempotency, restatement ----
def test_a_second_sync_books_nothing_and_changes_nothing(con):
    lot(con, "long", "BZ", 134, D(2026, 9, 22), D(2026, 9, 30))
    fill(con, "long", "BZ", "sell", 134, 14.25, "2026-09-30")
    nav(con, [D(2026, 10, 13), D(2026, 10, 14)])
    b = Broker([fee("2026-09-30", "REG", -0.05), div("2026-10-14", "BZ", 0.51, 134, "2026-09-28")])
    first = cash_events.sync(con, b, SESSIONS, D(2026, 10, 15))
    assert len(first["booked"]) == 2

    def state():
        return (con.execute("SELECT * FROM agent_books ORDER BY book").fetchall(),
                con.execute("SELECT * FROM agent_book_nav ORDER BY as_of, book").fetchall(),
                con.execute("SELECT * FROM agent_cash_events ORDER BY activity_id, book").fetchall())
    before = state()
    again = cash_events.sync(con, b, SESSIONS, D(2026, 10, 16))
    assert again["booked"] == [] and again["n_already"] == 2 and state() == before


def test_every_nav_row_from_the_cash_day_on_is_restated_the_run_day_row_and_later_ones_too(con):
    lot(con, "long", "BZ", 134, D(2026, 9, 22), D(2026, 9, 30))
    fill(con, "long", "BZ", "sell", 134, 14.25, "2026-10-09")
    days = [D(2026, 10, 9), D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15), D(2026, 10, 16)]
    nav(con, days)                                                           # 10/15: the 13:25 sync's row; 10/16: a later bar's
    b = Broker([fee("2026-10-09", "REG", -0.10), div("2026-10-14", "BZ", 0.51, 134, "2026-09-28")])
    r = cash_events.sync(con, b, SESSIONS, D(2026, 10, 15))
    assert {x["kind"]: x["cash_day"] for x in r["booked"]} == {"FEE": "2026-10-12", "DIV": "2026-10-14"}
    assert {x["kind"]: x["restated_rows"] for x in r["booked"]} == {"FEE": 5, "DIV": 3}
    got = dict(con.execute("SELECT as_of, cash_usd FROM agent_book_nav WHERE book = 'long'").fetchall())
    assert got == pytest.approx({D(2026, 10, 9): 1000.0, D(2026, 10, 12): 999.9, D(2026, 10, 13): 999.9, D(2026, 10, 14): 1068.24,
                                 D(2026, 10, 15): 1068.24, D(2026, 10, 16): 1068.24})
    eq = dict(con.execute("SELECT as_of, equity_usd - cash_usd FROM agent_book_nav WHERE book = 'long'").fetchall())
    assert set(eq.values()) == {0.0}                                         # equity moved with cash
    assert set(r[0] for r in con.execute("SELECT DISTINCT account_equity_usd FROM agent_book_nav").fetchall()) == {90000.0}
    assert dict(con.execute("SELECT as_of, cash_usd FROM agent_book_nav WHERE book = 'insider'").fetchall())[D(2026, 10, 14)] == 1000.0


def test_a_sync_on_a_ledger_whose_run_day_row_exists_keeps_every_equity_auction_and_mark_books_agrees(con):
    """The CLI after the evening's runs, or a run that stops between the sync and mark_books: the run_day row exists
    and nothing rewrites it. Rows written by mark_books, then a real sync, then the auction basis on both ledgers."""
    lot(con, "long", "BZ", 134, D(2026, 9, 22), D(2026, 9, 30))
    fill(con, "long", "BZ", "sell", 134, 14.25, "2026-09-30")
    days = [D(2026, 9, 30), D(2026, 10, 1), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15), D(2026, 10, 16)]
    for d in days:
        execute.mark_books(con, d, {}, 100_000.0)                            # as the runs wrote them, before the money

    def navs():
        return dict(((d, b), (c, e)) for d, b, c, e in con.execute("SELECT as_of, book, cash_usd, equity_usd FROM agent_book_nav").fetchall())

    def auction():
        n = con.execute("SELECT as_of, book, equity_usd FROM agent_book_nav").df()
        n["as_of"] = pd.to_datetime(n["as_of"]).dt.date
        rows = auction_basis.nav_rows(n, pd.DataFrame(columns=["book", "d", "adj"]), {}, pd.DataFrame(), cash_events.load(con))
        return {(r[0], r[1]): r[auction_basis.NAV_COLS.index("equity_auction")] for r in rows}
    before, nav0 = auction(), navs()
    b = Broker([fee("2026-09-30", "REG", -0.10), div("2026-10-14", "BZ", 0.51, 134, "2026-09-28")])
    r = cash_events.sync(con, b, SESSIONS, D(2026, 10, 15))
    assert {x["kind"]: (x["cash_day"], x["restated_rows"]) for x in r["booked"]} == {"FEE": ("2026-10-01", 5), "DIV": ("2026-10-14", 3)}
    moved = {k: round(v[0] - nav0[k][0], 2) for k, v in navs().items() if k[1] == "long"}
    assert moved == {(D(2026, 9, 30), "long"): 0.0, (D(2026, 10, 1), "long"): -0.1, (D(2026, 10, 13), "long"): -0.1,
                     (D(2026, 10, 14), "long"): 68.24, (D(2026, 10, 15), "long"): 68.24, (D(2026, 10, 16), "long"): 68.24}
    assert auction() == pytest.approx(before)                                # the restated cash is taken out again, run_day row too
    after = navs()
    execute.mark_books(con, D(2026, 10, 15), {}, 100_000.0)                  # the run's own row afterwards: the same values
    assert navs() == pytest.approx(after)


def test_in_live_running_nothing_is_restated(con):
    lot(con, "long", "MRP", 70, D(2026, 9, 30))
    nav(con, [D(2026, 10, 14)])
    r = cash_events.sync(con, Broker([div("2026-10-15", "MRP", 0.79, 70, "2026-10-05")]), SESSIONS, D(2026, 10, 15))
    assert r["booked"][0]["cash_day"] == "2026-10-15" and r["booked"][0]["restated_rows"] == 0


def test_unhandled_types_are_reported_not_booked_and_a_failed_report_keeps_the_booking(con):
    lot(con, "long", "MRP", 70, D(2026, 9, 30))
    acts = [{"id": "j1", "activity_type": "JNLC", "date": "2026-09-21", "net_amount": "100000"},
            {"id": "f1", "activity_type": "FILL", "transaction_time": "2026-10-14T13:30:06Z", "symbol": "MRP"},
            {"id": "i1", "activity_type": "INT", "date": "2026-10-01", "net_amount": "1.23", "description": "interest"},
            div("2026-10-15", "MRP", 0.79, 70, "2026-10-05")]
    r = cash_events.sync(con, Broker(acts), SESSIONS, D(2026, 10, 15))
    assert [u["type"] for u in r["unhandled"]] == ["INT"] and len(r["booked"]) == 1 and r["unhandled_error"] is None
    assert cash_events.summary_line(r) == "cash events booked 1 (net $55.30), unallocated 0, unhandled 1"
    b = Broker(acts + [div("2026-10-16", "MRP", 0.1, 70, "2026-10-06", aid="d2")], fail_unfiltered=True)
    r = cash_events.sync(con, b, SESSIONS, D(2026, 10, 16))
    assert len(r["booked"]) == 1 and r["unhandled_error"].startswith("BrokerError")
    assert b.calls[0][0] == list(cash_events.DIV_TYPES + cash_events.FEE_TYPES) and b.calls[0][1] == cash_events.START


# ---------------------------------------------------------------- execute keeps going ----
def test_execute_records_a_failed_cash_events_sync_and_goes_on(con, capsys):
    r = execute.soft_cash_events(con, Broker([], fail=True), SESSIONS, D(2026, 10, 15))
    assert r["error"].startswith("BrokerError: GET /account/activities -> no answer")
    assert "cash events not booked" in capsys.readouterr().out
    assert cash_events.summary_line(r).startswith("cash events not booked: BrokerError")
    assert execute.soft_cash_events(con, object(), SESSIONS, D(2026, 10, 15))["error"].startswith("AttributeError")
    assert con.execute("SELECT count(*) FROM agent_cash_events").fetchone()[0] == 0
    total = sum(b["alloc_usd"] for b in execute.BOOKS.values())
    assert execute.cash_check(con, total)["ok"] is True                         # the ledger is usable afterwards


# ---------------------------------------------------------------- broker transport ----
def test_activities_pages_by_the_last_id_and_sends_after_in_utc(monkeypatch):
    seen = []

    def req(self, method, path, params=None, body=None):
        seen.append(dict(params))
        n = 100 if len(seen) == 1 else 3
        return [{"id": f"p{len(seen)}-{i}", "activity_type": "FEE"} for i in range(n)]
    monkeypatch.setattr(PaperBroker, "_req", req)
    out = PaperBroker("PKX", "s").activities(["DIV", "FEE"], dt.datetime(2026, 9, 19))
    assert len(out) == 103 and seen[0]["after"] == "2026-09-19T00:00:00Z" and seen[0]["activity_types"] == "DIV,FEE"
    assert seen[1]["page_token"] == "p1-99" and seen[0]["page_size"] == 100 and seen[0]["direction"] == "asc"
    seen.clear()
    PaperBroker("PKX", "s").activities(None, dt.datetime(2026, 9, 19, 16, 10, tzinfo=execute.ET))
    assert "activity_types" not in seen[0] and seen[0]["after"] == "2026-09-19T20:10:00Z"
    seen.clear()
    PaperBroker("PKX", "s").fills(dt.datetime(2026, 9, 19))
    assert seen[0]["activity_types"] == "FILL"


def test_activities_is_a_get(monkeypatch):
    methods = []

    def urlopen(req, timeout):
        methods.append(req.get_method())
        raise urllib.error.HTTPError(req.full_url, 422, "bad", {}, io.BytesIO(b'{"message":"invalid activity type: SSO"}'))
    monkeypatch.setattr(alpaca.urllib.request, "urlopen", urlopen)
    with pytest.raises(BrokerError) as e:
        PaperBroker("PKX", "s").activities(["SSO"], dt.datetime(2026, 9, 19))
    assert methods == ["GET"] and e.value.status == 422


# ---------------------------------------------------------------- corporate actions ----
def ca(i, sym, rate, ex, pay, rec=None, special=False, kind="cash_dividends"):
    x = {"id": i, "symbol": sym, "ex_date": ex, "record_date": rec or ex, "payable_date": pay, "special": special, "foreign": False}
    if kind == "cash_dividends":
        x["rate"] = rate
    return kind, x


def test_entitlement_from_cached_actions_sticks_when_a_fetch_fails_and_waits_for_the_ex_date(con):
    lot(con, "long", "PSEC", 881, D(2026, 9, 22))
    lot(con, "long", "BZ", 134, D(2026, 9, 22), D(2026, 9, 30))
    lot(con, "insider", "GOLD", 10, D(2026, 9, 14), D(2026, 9, 18))
    lot(con, "long", "NCT", 25, D(2026, 9, 15), D(2026, 9, 24))
    nav(con, [D(2026, 10, 20)])                                               # the last session
    acts = [ca("p1", "PSEC", 0.035, "2026-09-28", "2026-10-21"), ca("p2", "PSEC", 0.035, "2026-10-28", "2026-11-18"),   # future
            ca("b1", "BZ", 0.51, "2026-09-28", "2026-10-14"), ca("b2", "BZ", 0.51, "2026-12-28", "2027-01-14"),
            ca("g1", "GOLD", 1.0, "2026-09-16", "2026-09-28", special=True), ca("g2", "GOLD", 0.2, "2026-09-16", "2026-09-28"),
            ca("n1", "NCT", None, "2026-09-17", "2026-09-17", kind="reverse_splits")]
    asked = []

    def fetch(symbols, start, end):
        asked.append((symbols, start, end))
        return acts
    rows = dividends.update(con, None, fetch=fetch)
    assert asked[0][0] == ["BZ", "GOLD", "NCT", "PSEC"] and asked[0][1] == D(2026, 9, 4)
    assert asked[0][2] == dt.date.today() + dt.timedelta(days=365)           # Alpaca's end is the pay date, not the ex-date
    assert rows.attrs["fetch_error"] is None
    cash_ = rows[rows["kind"] == "dividend"].set_index("ticker")
    assert cash_["usd"].to_dict() == {"BZ": 68.34, "GOLD": 12.0, "PSEC": 30.84}          # PSEC 10/28 not reached yet
    assert cash_.loc["GOLD", "per_share"] == pytest.approx(1.2) and bool(cash_.loc["GOLD", "special"])
    assert cash_.loc["GOLD", "ca_id"] == "g1,g2" and cash_.loc["BZ", "pay_date"] == D(2026, 10, 14)
    assert cash_.loc["BZ", "source"] == "alpaca_ca" and cash_.loc["BZ", "record_date"] == D(2026, 9, 28)
    nct = rows[rows["ticker"] == "NCT"].iloc[0]
    assert (nct["kind"], nct["usd"]) == ("split_or_special", 0.0)
    assert [c["ca_type"] for c in rows.attrs["to_check"]] == ["reverse_splits"]
    n_tab = con.execute("SELECT count(*) FROM agent_dividends").fetchone()[0]

    def down(symbols, start, end):
        raise RuntimeError("corporate actions HTTP 0: timed out")
    again = dividends.update(con, None, fetch=down)                          # yesterday's knowledge stays
    assert again.attrs["fetch_error"] == "RuntimeError: corporate actions HTTP 0: timed out"
    assert again[again["kind"] == "dividend"]["usd"].sum() == pytest.approx(111.18)
    assert con.execute("SELECT count(*) FROM agent_dividends").fetchone()[0] == n_tab
    assert con.execute("SELECT count(*) FROM agent_corp_actions").fetchone()[0] == len(acts)
    con.execute("INSERT INTO agent_book_nav VALUES ('2026-10-28', 'long', 1, 0, 1, 0, 1)")
    later = dividends.update(con, None, fetch=down)                          # 10/28 is now a session: counted
    assert later.set_index(["ticker", "ex_date"]).loc[("PSEC", D(2026, 10, 28)), "usd"] == 30.84


def test_auction_basis_reports_a_failed_fetch_as_its_dividend_error(monkeypatch, con):
    class Store:
        def __init__(self, read_only=True):
            pass

        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(auction_basis, "PanelStore", Store)
    monkeypatch.setattr(dividends, "fetch_alpaca", lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    lot(con, "long", "X", 1, D(2026, 9, 22))
    rows, err = auction_basis.soft_dividends(con)
    assert err == "corporate actions fetch failed, cache kept: RuntimeError: HTTP 500" and rows.empty


def test_factor_steps_no_action_explains_are_listed_to_check_never_counted():
    steps = pd.DataFrame([{"ticker": "BZ", "ex_date": D(2026, 9, 29), "per_share": 0.0, "kind": "basis_break"},   # 1 weekday off 9/28
                          {"ticker": "BZ", "ex_date": D(2026, 10, 9), "per_share": 0.2, "kind": "dividend"}])
    acts = pd.DataFrame([{"symbol": "BZ", "ex_date": D(2026, 9, 28)}])
    left = dividends.unexplained_steps(steps, acts)
    assert list(left["ex_date"]) == [D(2026, 10, 9)]
    assert dividends.unexplained_steps(steps, pd.DataFrame(columns=["symbol", "ex_date"])).shape[0] == 2


# ---------------------------------------------------------------- auction basis, readers ----
def test_auction_basis_takes_the_booked_cash_out_so_a_restatement_changes_no_equity_auction():
    days = [D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15)]
    old = pd.DataFrame([{"as_of": d, "book": "long", "equity_usd": 50_000.0 + i} for i, d in enumerate(days)])
    fills = pd.DataFrame([{"book": "long", "d": D(2026, 10, 13), "adj": 7.0}])
    fees = {("long", D(2026, 10, 13)): 0.05}
    div = pd.DataFrame([{"book": "long", "kind": "dividend", "ex_date": D(2026, 9, 28), "usd": 68.34}])
    ev = pd.DataFrame([{"book": "long", "kind": "DIV", "net_usd": 68.34, "cash_day": D(2026, 10, 14)},
                       {"book": "long", "kind": "FEE", "net_usd": -0.39, "cash_day": D(2026, 10, 13)}])
    before = auction_basis.nav_rows(old, fills, fees, div)                   # the ledger before the catch-up
    new = old.copy()                                                         # restated: cash_day <= as_of
    new["equity_usd"] += [-0.39, 68.34 - 0.39, 68.34 - 0.39]
    after = auction_basis.nav_rows(new, fills, fees, div, ev)
    col = {c: i for i, c in enumerate(auction_basis.NAV_COLS)}
    for b, a in zip(before, after):
        assert a[col["equity_auction"]] == pytest.approx(b[col["equity_auction"]])
        assert a[col["equity_auction_tr"]] == pytest.approx(b[col["equity_auction_tr"]])
    assert [a[col["div_cash_cum"]] for a in after] == [0.0, 68.34, 68.34]
    assert [a[col["fee_cash_cum"]] for a in after] == [0.39, 0.39, 0.39]
    assert after[1][col["equity_auction"]] == pytest.approx(50_001 + 68.34 - 0.39 - 68.34 + 0.39 + 7.0 - 0.05)


def test_broker_fees_and_dividends_against_the_model_and_the_entitlement():
    div = pd.DataFrame([{"lot_id": "l|STX", "book": "long", "ticker": "STX", "ex_date": D(2026, 9, 24), "record_date": D(2026, 9, 24),
                         "pay_date": D(2026, 10, 7), "usd": 1.48, "kind": "dividend"},
                        {"lot_id": "l|BZ", "book": "long", "ticker": "BZ", "ex_date": D(2026, 9, 28), "record_date": D(2026, 9, 28),
                         "pay_date": D(2026, 10, 14), "usd": 68.34, "kind": "dividend"},
                        {"lot_id": "l|PSEC", "book": "long", "ticker": "PSEC", "ex_date": D(2026, 9, 28), "record_date": D(2026, 9, 28),
                         "pay_date": D(2026, 10, 21), "usd": 30.84, "kind": "dividend"}])
    ev = pd.DataFrame([{"book": "long", "kind": "DIV", "ticker": "BZ", "net_usd": 68.34, "cash_day": D(2026, 10, 14),
                        "record_date": D(2026, 9, 28), "activity_date": D(2026, 10, 14)},
                       {"book": "insider", "kind": "FEE", "ticker": None, "net_usd": -0.04, "cash_day": D(2026, 10, 1),
                        "record_date": None, "activity_date": D(2026, 9, 30)}])
    fees = {("insider", D(2026, 9, 25)): 0.02, ("insider", D(2026, 9, 30)): 0.05}
    f, d = auction_basis.broker_vs_model(div, ev, fees, D(2026, 10, 20))
    assert f["broker_total"] == 0.04 and f["broker_by_book"] == {"insider": 0.04} and f["model_by_book"] == {"insider": 0.07}
    assert f["broker_first_day"] == "2026-09-30" and f["model_since_broker_first_day"] == 0.05
    assert d["received_usd"] == 68.34 and d["entitled_by_book"] == {"long": 100.66}
    assert [u["ticker"] for u in d["unpaid_past_pay_date"]] == ["STX"] and d["unpaid_usd"] == 1.48     # PSEC not due yet


def _auction_ledger(con):
    """BZ (ex 9/28, $68.34) paid 10/14 and booked by the 16:10 run, after the 13:25 auction rebuild (its 10/14 row has
    div_cash_cum 0); PSEC (ex 10/01, $30.84) entitled, not paid yet."""
    con.execute(auction_basis.DDL)
    auction_basis.ensure_table(con)
    con.execute("INSERT INTO agent_book_nav VALUES ('2026-10-13', 'long', 2900, 50000, 52900, 30, 1), "
                "('2026-10-14', 'long', 2968.34, 50000, 52968.34, 30, 1)")
    con.execute(f"""INSERT INTO agent_auction_nav ({', '.join(auction_basis.NAV_COLS)}) VALUES
                    ('2026-10-13', 'long', 52900, 0, 52900, 0, 0, 99.18, 52999.18, 0, 0),
                    ('2026-10-14', 'long', 52900, 0, 52900, 0, 0, 99.18, 52999.18, 0, 0)""")
    cash_events.ensure_table(con)
    con.execute("""INSERT INTO agent_cash_events (activity_id, book, kind, ticker, activity_date, record_date, net_usd, cash_day)
                   VALUES ('a1', 'long', 'DIV', 'BZ', '2026-10-14', '2026-09-28', 68.34, '2026-10-14')""")
    dividends.ensure_tables(con)
    con.execute("""INSERT INTO agent_dividends (lot_id, book, ticker, ex_date, per_share, qty, usd, kind) VALUES
                   ('l1', 'long', 'BZ', '2026-09-28', 0.51, 134, 68.34, 'dividend'),
                   ('l2', 'long', 'PSEC', '2026-10-01', 0.035, 881, 30.84, 'dividend')""")


def test_evaluate_counts_a_paid_dividend_once_even_when_it_was_booked_after_the_auction_rebuild(con):
    _auction_ledger(con)
    nav_, _, _ = evaluate.load_books(con)
    assert list(nav_["sim"]) == [52900.0, 52968.34]                         # what the paper account shows
    assert list(nav_["div_cash_cum"]) == [0.0, 68.34]                       # from agent_cash_events, not the 13:25 row
    assert list(nav_["sim_tr"]) == pytest.approx([52999.18, 52999.18])      # each dividend on its ex-date, once
    assert evaluate.BASIS_LABEL["sim"] == "模拟器口径(含券商实付的分红和费用)"


def test_attribution_reads_the_total_return_simulator_equity_on_one_basis_for_every_row(con):
    _auction_ledger(con)
    con.execute("INSERT INTO agent_book_nav VALUES ('2026-10-15', 'long', 3000, 50000, 53000, 30, 1)")   # no auction row
    got = attribution.book_equity(con, D(2026, 10, 13), D(2026, 10, 15)).sort_values("as_of")
    assert list(got["equity_usd"]) == pytest.approx([52999.18, 52999.18, 53030.84])
    con.execute("DROP TABLE agent_dividends")                                 # none yet: the paid cash still comes out
    got = attribution.book_equity(con, D(2026, 10, 13), D(2026, 10, 15)).sort_values("as_of")
    assert list(got["equity_usd"]) == pytest.approx([52900.0, 52900.0, 52931.66])


def test_review_auction_table_difference_adds_up(tmp_path, monkeypatch):
    from agent import review
    v = {"equity_sim": 52973.99, "equity_auction": 53562.78, "adj_cum": 712.91, "n_fills": 40, "fees_cum": 1.12,
         "div_cash_cum": 123.64, "fee_cash_cum": 0.64}
    assert v["equity_sim"] - v["div_cash_cum"] + v["fee_cash_cum"] + v["adj_cum"] - v["fees_cum"] == pytest.approx(v["equity_auction"])
    (tmp_path / "auction_basis.json").write_text(json.dumps({"books": {"long": v}, "fills": {}, "missed": []}))
    monkeypatch.setattr(review, "OUT_DIR", str(tmp_path))
    html = review.auction_block({})
    assert "<td>+589</td><td>+713</td><td>-123.64</td><td>-0.48</td>" in html and "券商分红移出" in html


# ---------------------------------------------------------------- health ----
def _sql(db, *stmts):
    """Write with a connection of its own and close it: the check opens the file read-only in this process."""
    c = ledger.connect(db)
    try:
        for q in stmts:
            c.execute(q) if isinstance(q, str) else q(c)
    finally:
        c.close()


def test_health_cash_events_check(tmp_path):
    db = str(tmp_path / "h.db")
    _sql(db, ledger.ensure_schema)
    out = tmp_path / "out"
    out.mkdir()
    now = dt.datetime(2026, 10, 20, 17, 0)

    def run(name, gen, ce):
        (out / name).write_text(json.dumps({"generated_at": gen, **({"cash_events": ce} if ce is not None else {})}))

    def check():
        return health.check_cash_events(now, str(out), db, str(tmp_path / "ab.json"))
    r = check()
    assert r["level"] == "warn" and "没有找到执行记录" in r["detail"]
    run("exec_2026-10-19.json", "2026-10-19T16:14:00", None)
    assert "里没有现金事件记录" in check()["detail"]
    ok = {"booked": [{"books": {"long": 68.34}}], "unallocated": [], "unhandled": [], "unhandled_error": None}
    run("sync_2026-10-20.json", "2026-10-20T13:31:00", ok)
    r = check()
    assert (r["level"], r["detail"]) == ("ok", "sync_2026-10-20.json 新入账 1 笔($68.34),账本里共 0 笔券商分红和费用流水,没有分不下去的流水")
    bad = dict(ok, unallocated=[{"ticker": "XYZ", "type": "DIV", "date": "2026-10-20", "net_usd": 10.0, "why": "no lot of XYZ"}],
               unhandled=[{"type": "INT", "date": "2026-10-01", "net_usd": 1.23}])
    run("exec_2026-10-20.json", "2026-10-20T16:13:00", bad)                  # the newest run wins
    r = check()
    assert r["level"] == "bad" and "1 笔券商流水分不到书里" in r["detail"] and "XYZ DIV 2026-10-20 $10.00(no lot of XYZ)" in r["detail"]
    assert "1 笔没有处理的流水类型:INT 2026-10-01 $1.23" in r["detail"]
    run("exec_2026-10-20.json", "2026-10-20T16:13:00", {"error": "BrokerError: no answer"})
    r = check()
    assert r["level"] == "warn" and "现金事件没有同步:BrokerError" in r["detail"]
    # an entitled dividend 13 days past its pay date that the broker never paid; one paid; one not 5 days past yet
    run("exec_2026-10-20.json", "2026-10-20T16:13:00", ok)
    _sql(db, dividends.ensure_tables, cash_events.ensure_table,
         """INSERT INTO agent_dividends (lot_id, book, ticker, ex_date, per_share, qty, usd, kind, pay_date, record_date) VALUES
            ('l1', 'long', 'STX', '2026-09-24', 0.74, 2, 1.48, 'dividend', '2026-10-07', '2026-09-24'),
            ('l2', 'long', 'BZ', '2026-09-28', 0.51, 134, 68.34, 'dividend', '2026-10-14', '2026-09-28'),
            ('l3', 'insider', 'KBDC', '2026-09-30', 0.4, 114, 45.6, 'dividend', '2026-10-16', '2026-09-30')""",
         """INSERT INTO agent_cash_events (activity_id, book, kind, ticker, activity_date, record_date, net_usd, cash_day)
            VALUES ('a1', 'long', 'DIV', 'BZ', '2026-10-14', '2026-09-28', 68.34, '2026-10-14')""")
    r = check()
    assert r["level"] == "warn" and "STX $1.48(支付日 10-07)" in r["detail"] and "BZ" not in r["detail"] and "KBDC" not in r["detail"]
    _sql(db, "DELETE FROM agent_dividends WHERE ticker = 'STX'")
    assert check()["detail"].endswith("账本里共 1 笔券商分红和费用流水,没有分不下去的流水")
    (tmp_path / "ab.json").write_text(json.dumps({"dividends": {"error": "corporate actions fetch failed, cache kept: HTTP 500"}}))
    r = check()
    assert r["level"] == "warn" and "分红数据有问题(按缓存的公司行动计算):corporate actions fetch failed" in r["detail"]


def test_health_warns_on_a_booked_activity_the_broker_canceled(tmp_path):
    db = str(tmp_path / "h.db")
    _sql(db, ledger.ensure_schema, cash_events.ensure_table)
    out = tmp_path / "out"
    out.mkdir()
    ce = {"booked": [], "unallocated": [], "unhandled": [], "unhandled_error": None,
          "canceled": [{"id": "d1", "type": "DIV", "ticker": "MRP", "date": "2026-10-15", "net_usd": 55.3, "booked": True},
                       {"id": "d2", "type": "DIV", "ticker": "MRP", "date": "2026-10-16", "net_usd": 7.0, "booked": False}]}
    (out / "exec_2026-10-20.json").write_text(json.dumps({"generated_at": "2026-10-20T16:13:00", "cash_events": ce}))
    r = health.check_cash_events(dt.datetime(2026, 10, 20, 17, 0), str(out), db, str(tmp_path / "ab.json"))
    assert r["level"] == "warn" and "1 笔已入账的流水被券商撤销" in r["detail"] and "MRP DIV 2026-10-15 $55.30" in r["detail"]
    assert "2026-10-16" not in r["detail"]                                    # never booked: nothing to undo
