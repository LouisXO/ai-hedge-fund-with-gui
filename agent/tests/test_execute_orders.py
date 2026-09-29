"""Order path and ledger (S48): orders written as they are sent, failed POSTs looked up, orphans claimed,
fills booked by increment, in-flight buys holding slots and cash, the pre-submit guard, split hints,
the cash check and the --cancel-open tool. A fake broker only; no network.
"""
import datetime as dt
import io
import json
import sys
import urllib.error

import pandas as pd
import pytest

from agent import execute, ledger
from agent.broker import alpaca
from agent.broker.alpaca import BrokerError, PaperBroker
from agent.execute import (BOOKS, NOT_SENT, SUBMIT_UNKNOWN, cash_check, claim_orphans, client_id, guard_buys,
                           inflight_buys, order_cap, plan_book, reconcile, send_orders, split_hints, sync_fills)

AS_OF = dt.date(2026, 9, 28)
CAL = [dt.date(2026, 9, d) for d in (21, 22, 23, 24, 25, 28, 29, 30)] + [dt.date(2026, 10, d) for d in (1, 2, 5, 6)]


class FakeBroker:
    """Holds orders by client id. `script` maps a client id to what its POST does: 'timeout_accepted' (the broker
    takes it but the answer is lost), 'timeout_lost' (never arrives), 'reject' (HTTP 403), 'down' (the POST and
    every later call fail). Fills are set by the test with fill()."""

    def __init__(self, script=None):
        self.orders: dict[str, dict] = {}
        self.script = script or {}
        self.down = False
        self.posts: list[str] = []
        self.canceled: list[str] = []

    def submit(self, symbol, side, qty, order_type, tif, client_order_id, limit_price=None):
        self.posts.append(client_order_id)
        how = self.script.get(client_order_id)
        if how == "down":
            self.down = True
        if self.down:
            raise BrokerError("POST /orders -> no answer: URLError: <urlopen error [Errno 51] Network is unreachable>")
        if how == "reject":
            raise BrokerError('POST /orders -> 403: {"message":"insufficient buying power"}', status=403)
        o = {"id": f"id-{client_order_id}", "client_order_id": client_order_id, "symbol": symbol, "side": side,
             "qty": str(qty), "type": order_type, "time_in_force": tif, "status": "accepted", "filled_qty": "0",
             "filled_avg_price": None, "filled_at": None, "updated_at": None, "submitted_at": "2026-09-28T23:10:00Z",
             "limit_price": None if limit_price is None else str(limit_price)}
        if how == "timeout_lost":
            raise BrokerError("POST /orders -> no answer: TimeoutError: timed out")
        self.orders[client_order_id] = o
        if how == "timeout_accepted":
            raise BrokerError("POST /orders -> no answer: TimeoutError: timed out")
        return dict(o)

    def fill(self, coid, qty, avg, status="filled", filled_at="2026-09-29T13:30:05Z", updated_at=None):
        self.orders[coid].update({"filled_qty": str(qty), "filled_avg_price": str(avg), "status": status,
                                  "filled_at": filled_at, "updated_at": updated_at})

    def order_by_client_id(self, coid):
        if self.down:
            raise BrokerError("GET /orders:by_client_order_id -> no answer: URLError")
        o = self.orders.get(coid)
        return dict(o) if o else None

    def orders_since(self, after, status="all"):
        return [dict(o) for o in self.orders.values()]

    def open_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] in ("new", "accepted", "partially_filled")]

    def cancel(self, order_id):
        self.canceled.append(order_id)


@pytest.fixture
def con(tmp_path):
    c = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(c)
    execute.ensure_books(c)
    yield c
    c.close()


def _order(book, ticker, side, qty, limit=None, ref=10.0, as_of=AS_OF):
    return {"client_order_id": client_id(book, as_of, ticker, side), "book": book, "as_of": as_of, "ticker": ticker,
            "side": side, "qty": qty, "order_type": "limit" if limit else "market", "tif": "day", "limit_price": limit,
            "ref_close": ref, "reason": "entry" if side == "buy" else "hold_expired"}


def _cash(con, book):
    return con.execute("SELECT cash_usd FROM agent_books WHERE book = ?", [book]).fetchone()[0]


def _lot(con, ticker):
    return con.execute("""SELECT qty, entry_px, status, exit_px, ret_pct, sold_qty FROM agent_lots WHERE ticker = ?""",
                       [ticker]).fetchone()


# ---------------------------------------------------------------- broker transport ----
@pytest.mark.parametrize("exc", [urllib.error.URLError("dns"), TimeoutError("timed out"), ConnectionResetError("reset")])
def test_transport_failures_become_broker_errors_without_a_status(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(alpaca.urllib.request, "urlopen", boom)
    with pytest.raises(BrokerError) as e:
        PaperBroker("PKX", "s").submit("AAA", "buy", 1, "market", "day", "long|2026-09-28|AAA|buy")
    assert e.value.status is None


def test_a_body_that_is_not_json_is_a_broker_error_and_a_404_lookup_is_none(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(alpaca.urllib.request, "urlopen", lambda *a, **k: Resp(b"<html>gateway</html>"))
    with pytest.raises(BrokerError):
        PaperBroker("PKX", "s").account()

    def not_found(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"message":"order not found"}'))
    monkeypatch.setattr(alpaca.urllib.request, "urlopen", not_found)
    assert PaperBroker("PKX", "s").order_by_client_id("x") is None


# ---------------------------------------------------------------- sending ------------
def test_each_order_is_in_the_ledger_as_it_returns_and_a_timeout_is_looked_up(con):
    """POST 2 times out after the broker took it: recorded accepted with its id. POST 3 times out and never
    arrived: not_sent (final), and a rerun sends that id again. POST 4 is refused: rejected, no id."""
    plans = [_order("long", t, "buy", 10, limit=10.3) for t in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    ids = [o["client_order_id"] for o in plans]
    fb = FakeBroker({ids[1]: "timeout_accepted", ids[2]: "timeout_lost", ids[3]: "reject"})
    sent, not_attempted = send_orders(con, fb, plans, submit=True)
    assert not_attempted == [] and len(sent) == 5
    rows = dict(con.execute("SELECT client_order_id, status FROM agent_orders").fetchall())
    assert rows == {ids[0]: "accepted", ids[1]: "accepted", ids[2]: NOT_SENT, ids[3]: "rejected", ids[4]: "accepted"}
    assert con.execute("SELECT alpaca_id FROM agent_orders WHERE client_order_id = ?", [ids[1]]).fetchone()[0] == f"id-{ids[1]}"
    assert con.execute("SELECT alpaca_id FROM agent_orders WHERE client_order_id = ?", [ids[3]]).fetchone()[0] is None
    assert "insufficient buying power" in sent[3]["error"]
    # not_sent and rejected are final: the sync does not ask about them, nothing waits on them
    assert sync_fills(con, fb, CAL)["checked"] == 3


def test_when_the_broker_stops_answering_the_rest_is_not_attempted(con):
    plans = [_order("insider", t, "buy", 10, limit=10.3) for t in ("AAA", "BBB", "CCC", "DDD")]
    ids = [o["client_order_id"] for o in plans]
    fb = FakeBroker({ids[1]: "down"})
    sent, not_attempted = send_orders(con, fb, plans, submit=True)
    assert [o["status"] for o in sent] == ["accepted", SUBMIT_UNKNOWN] and not_attempted == ids[2:]
    assert fb.posts == ids[:2]
    # submit_unknown holds its slot and cash until the next sync; the broker has no such order -> not_sent
    assert {f["ticker"] for f in inflight_buys(con, "insider")} == {"AAA", "BBB"}
    fb.down = False
    st = sync_fills(con, fb, CAL)
    assert st["not_sent"] == 1
    assert con.execute("SELECT status FROM agent_orders WHERE client_order_id = ?", [ids[1]]).fetchone()[0] == NOT_SENT
    assert {f["ticker"] for f in inflight_buys(con, "insider")} == {"AAA"}


def test_a_dry_run_writes_nothing(con):
    sent, _ = send_orders(con, FakeBroker(), [_order("long", "AAA", "buy", 1, limit=10.3)], submit=False)
    assert sent[0]["status"] == "dry_run"
    assert con.execute("SELECT count(*) FROM agent_orders").fetchone()[0] == 0


def test_orphans_at_the_broker_are_claimed_and_their_fills_booked(con):
    """A run died after its POSTs: the broker has two of our orders (one filled), the ledger none. The next run
    claims both, books the fill, and leaves a hand-placed order and orders it already knows alone."""
    fb = FakeBroker()
    for t in ("AAA", "BBB"):
        fb.submit(t, "buy", 10, "limit", "day", client_id("insider", AS_OF, t, "buy"), 10.3)
    fb.submit("ZZZ", "buy", 1, "market", "day", "my-own-order")
    fb.submit("KNOWN", "buy", 1, "market", "day", client_id("long", AS_OF, "KNOWN", "buy"))
    fb.submit("OLD", "buy", 1, "market", "day", client_id("retired", AS_OF, "OLD", "buy"))   # not one of our books
    send_orders(con, FakeBroker(), [_order("long", "KNOWN", "buy", 1)], submit=True)
    fb.fill(client_id("insider", AS_OF, "AAA", "buy"), 10, 10.05)
    claimed = claim_orphans(con, fb.orders_since(None))
    assert sorted(r["ticker"] for r in claimed) == ["AAA", "BBB"]
    assert all(r["status"] == "claimed" and r["reason"] == "claimed" for r in claimed)
    assert claim_orphans(con, fb.orders_since(None)) == []                 # claimed once
    before = _cash(con, "insider")
    st = sync_fills(con, fb, CAL)
    assert st["filled"] == 1
    assert _lot(con, "AAA")[:3] == (10, 10.05, "open")
    assert _cash(con, "insider") == pytest.approx(before - 100.5)
    assert con.execute("SELECT status FROM agent_orders WHERE ticker = 'BBB'").fetchone()[0] == "accepted"


def test_a_not_sent_order_the_broker_did_get_after_all_is_claimed(con):
    """The row keeps what the plan wrote: a late insider retry stays a late retry, and so does its lot."""
    coid = client_id("insider", AS_OF, "AAA", "buy")
    fb = FakeBroker({coid: "timeout_lost"})
    o = {**_order("insider", "AAA", "buy", 5, limit=10.3), "reason": "entry_retry",
         "trigger_filing_day": dt.date(2026, 9, 24), "lag_sessions": 1, "entry_kind": "late"}
    send_orders(con, fb, [o], submit=True)
    assert con.execute("SELECT status FROM agent_orders").fetchone()[0] == NOT_SENT
    fb.script.clear()
    fb.submit("AAA", "buy", 5, "limit", "day", coid, 10.3)                   # it arrived late
    claimed = claim_orphans(con, fb.orders_since(None))
    assert [(r["ticker"], r["reason"], r["status"]) for r in claimed] == [("AAA", "entry_retry", "claimed")]
    assert con.execute("""SELECT status, alpaca_id, reason, ref_close, trigger_filing_day, lag_sessions, entry_kind,
                                 applied_qty FROM agent_orders""").fetchone() == \
        ("claimed", f"id-{coid}", "entry_retry", 10.0, dt.date(2026, 9, 24), 1, "late", 0)
    fb.fill(coid, 5, 10.1)
    sync_fills(con, fb, CAL)
    assert con.execute("SELECT ticker, trigger_filing_day, lag_sessions, entry_kind FROM agent_lots").fetchone() == \
        ("AAA", dt.date(2026, 9, 24), 1, "late")
    assert con.execute("SELECT status, reason FROM agent_orders").fetchone() == ("filled", "entry_retry")


# ---------------------------------------------------------------- fills by increment -
def _sent(con, fb, o):
    send_orders(con, fb, [o], submit=True)
    return o["client_order_id"]


def test_a_buy_filled_in_parts_is_booked_once(con):
    """Audit scenario A: 40 of 100 seen first, then 100 of 100: the book pays for 100 shares once."""
    fb = FakeBroker()
    coid = _sent(con, fb, _order("insider", "PRT", "buy", 100, limit=10.3))
    cash0 = _cash(con, "insider")
    fb.fill(coid, 40, 10.0, status="partially_filled", filled_at=None, updated_at="2026-09-29T13:31:00Z")
    st = sync_fills(con, fb, CAL)
    assert st["filled"] == 1 and st["partial"] == 1
    assert _lot(con, "PRT")[:3] == (40, 10.0, "open")
    assert _cash(con, "insider") == pytest.approx(cash0 - 400)
    # the partial fill had no filled_at: its day comes from updated_at, and so does the hold
    assert con.execute("SELECT entry_day, hold_until FROM agent_lots").fetchone() == (dt.date(2026, 9, 29), dt.date(2026, 10, 6))
    assert sync_fills(con, fb, CAL)["filled"] == 0                           # seen again unchanged: nothing booked
    assert _cash(con, "insider") == pytest.approx(cash0 - 400)
    fb.fill(coid, 100, 10.06)                                                 # 60 more at 10.10: average 10.06
    sync_fills(con, fb, CAL)
    assert _lot(con, "PRT")[:3] == (100, 10.06, "open")
    assert _cash(con, "insider") == pytest.approx(cash0 - 1006)
    assert con.execute("SELECT applied_qty, applied_notional, status FROM agent_orders").fetchone() == pytest.approx((100, 1006, "filled"))
    assert sync_fills(con, fb, CAL)["checked"] == 0                           # final: not asked again
    assert con.execute("SELECT count(*) FROM agent_lots").fetchone()[0] == 1


def test_a_sell_filled_in_parts_closes_at_the_weighted_price(con):
    """Audit scenarios C and D: 100 shares bought at 10, sold 40 @ 12 then 60 @ 9. The lot closes at 10.20,
    +2.0%, and the book receives 1,020 once."""
    fb = FakeBroker()
    b = _sent(con, fb, _order("insider", "GSA", "buy", 100, limit=10.3))
    fb.fill(b, 100, 10.0, filled_at="2026-09-22T13:30:00Z")
    sync_fills(con, fb, CAL)
    cash0 = _cash(con, "insider")
    s = _sent(con, fb, _order("insider", "GSA", "sell", 100, as_of=dt.date(2026, 9, 28)))
    fb.fill(s, 40, 12.0, status="partially_filled", filled_at=None, updated_at="2026-09-29T13:30:02Z")
    sync_fills(con, fb, CAL)
    lot = _lot(con, "GSA")
    assert lot[0] == 60 and lot[2] == "open" and lot[5] == 40                  # the account still holds 60
    assert reconcile(execute.open_lots(con), {"GSA": {"qty": "60"}}) == (set(), [])
    assert _cash(con, "insider") == pytest.approx(cash0 + 480)
    sync_fills(con, fb, CAL)                                                  # same state again: nothing moves
    assert _cash(con, "insider") == pytest.approx(cash0 + 480)
    fb.fill(s, 100, 10.2)
    st = sync_fills(con, fb, CAL)
    assert st["closed"] == 1
    qty, entry, status, exit_px, ret, sold = _lot(con, "GSA")
    assert (qty, status, sold) == (100, "closed", 100)
    assert exit_px == pytest.approx(10.2) and ret == pytest.approx(2.0)
    assert _cash(con, "insider") == pytest.approx(cash0 + 1020)


def test_a_sell_left_partly_filled_is_finished_by_the_next_evenings_order(con):
    fb = FakeBroker()
    b = _sent(con, fb, _order("insider", "GSA", "buy", 100, limit=10.3))
    fb.fill(b, 100, 10.0, filled_at="2026-09-22T13:30:00Z")
    sync_fills(con, fb, CAL)
    s1 = _sent(con, fb, _order("insider", "GSA", "sell", 100, as_of=dt.date(2026, 9, 28)))
    fb.fill(s1, 40, 12.0, status="expired")
    sync_fills(con, fb, CAL)
    s2 = _sent(con, fb, _order("insider", "GSA", "sell", 60, as_of=dt.date(2026, 9, 29)))
    fb.fill(s2, 60, 9.0, filled_at="2026-09-30T13:30:00Z")
    sync_fills(con, fb, CAL)
    qty, _, status, exit_px, ret, _ = _lot(con, "GSA")
    assert (qty, status) == (100, "closed") and exit_px == pytest.approx(10.2) and ret == pytest.approx(2.0)


def test_the_migration_counts_what_the_old_code_already_booked(tmp_path):
    """The production ledger before S48: an order the old code synced while partially filled (40 booked). After
    the columns are added, the final fill books only the other 60."""
    c = ledger.connect(str(tmp_path / "old.db"))
    ledger.ensure_schema(c)
    for col in ("applied_qty", "applied_notional"):
        c.execute(f"ALTER TABLE agent_orders DROP COLUMN {col}")
    execute.ensure_books(c)
    coid = client_id("insider", AS_OF, "PRT", "buy")
    c.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, alpaca_id, status, filled_qty,
                 filled_avg_px, dry_run) VALUES (?, 'insider', ?, 'PRT', 'buy', 100, 'o1', 'partially_filled', 40, 10.0, FALSE)""",
              [coid, AS_OF])
    c.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, entry_day, entry_px, status, entry_order)
                 VALUES ('insider|PRT|2026-09-29', 'insider', 'PRT', 40, '2026-09-29', 10.0, 'open', ?)""", [coid])
    c.execute("UPDATE agent_books SET cash_usd = cash_usd - 400 WHERE book = 'insider'")
    ledger.ensure_schema(c)
    ledger.ensure_schema(c)                                                   # the backfill runs with the ALTER only
    assert c.execute("SELECT applied_qty, applied_notional FROM agent_orders").fetchone() == (40, 400)
    fb = FakeBroker()
    fb.orders[coid] = {"id": "o1", "status": "filled", "filled_qty": "100", "filled_avg_price": "10.06",
                       "filled_at": "2026-09-29T14:00:00Z"}
    sync_fills(c, fb, CAL)
    assert c.execute("SELECT qty, entry_px FROM agent_lots").fetchone() == (100, 10.06)
    assert _cash(c, "insider") == pytest.approx(30_000 - 1006)
    c.close()


# ---------------------------------------------------------------- in-flight buys -----
def test_a_second_run_the_same_evening_does_not_spend_the_same_cash_again(con):
    """Audit scenario F: 20 slots, 17 lots, $4,500 of cash. The first run buys 3; a second run, whose candidates
    no longer include those 3 (insider_targets skips names with working orders), buys nothing."""
    cfg = BOOKS["insider"]
    lots = [{"ticker": f"H{i}", "qty": 100, "entry_px": 15.0, "hold_until": dt.date(2026, 10, 5)} for i in range(17)]
    ref = {**{f"H{i}": 15.0 for i in range(17)}, **{f"C{i}": 20.0 for i in range(8)}}
    first = plan_book("insider", cfg, lots, [f"C{i}" for i in range(8)], set(), AS_OF, CAL[6], ref, {}, 4_500.0, set(), tif="day")
    assert [o["ticker"] for o in first] == ["C0", "C1", "C2"]
    send_orders(con, FakeBroker(), first, submit=True)
    flying = inflight_buys(con, "insider")
    assert {f["ticker"] for f in flying} == {"C0", "C1", "C2"}
    second = plan_book("insider", cfg, lots, [f"C{i}" for i in range(3, 8)], set(), AS_OF, CAL[6], ref, {}, 4_500.0, set(),
                       tif="day", inflight=flying)
    assert second == []
    # with free slots but the cash committed, still nothing: 3 working buys cost ~$4,470 of the $4,500
    fewer = lots[:10]
    third = plan_book("insider", cfg, fewer, [f"C{i}" for i in range(3, 8)], set(), AS_OF, CAL[6], ref, {}, 4_500.0, set(),
                      tif="day", inflight=flying)
    assert third == []
    # a working buy of a name that is ranked again is not planned twice
    again = plan_book("insider", cfg, fewer, ["C0", "C5"], set(), AS_OF, CAL[6], ref, {}, 60_000.0, set(), tif="day", inflight=flying)
    assert [o["ticker"] for o in again] == ["C5"]


def test_a_rerun_keeps_the_size_of_a_buy_funded_by_sells_already_sent(con):
    """Review finding (S48 fix): the long book has $1,000 and funds two entries with two rank_out sells. Run 1
    sends both sells and N1, then the broker stops answering (N2 submit_unknown -> not_sent at the sync). Run 2
    the same evening drops the sells as already sent; their proceeds still count, so N2 goes out at 39 shares
    as planned (the guard used to count only the buys in flight: N2 was cut to 0)."""
    con.execute("UPDATE agent_books SET cash_usd = 1000 WHERE book = 'long'")
    lots = [{"ticker": f"H{i}", "qty": 100, "entry_px": 20.0, "hold_until": None} for i in range(30)]
    ref = {**{f"H{i}": 20.0 for i in range(30)}, "N1": 50.0, "N2": 50.0}
    keep = {f"H{i}" for i in range(28)}                                      # H28, H29 rank out
    slots = {"long": (1_000.0 + 60_000.0) / 30}

    def plan(inflight):
        return plan_book("long", BOOKS["long"], lots, ["N1", "N2"], keep, AS_OF, CAL[6], ref, {}, 1_000.0, set(), True,
                         "day", inflight=inflight)

    fb = FakeBroker({client_id("long", AS_OF, "N2", "buy"): "down"})
    run1, skipped, notes = execute.final_orders(con, fb, plan([]), {"long": 1_000.0}, None, slots, ref)
    assert [(o["side"], o["ticker"], o["qty"]) for o in run1] == [("sell", "H28", 100), ("sell", "H29", 100),
                                                                   ("buy", "N1", 39), ("buy", "N2", 39)]
    assert skipped == [] and notes == []
    send_orders(con, fb, run1, submit=True)
    fb.down = False
    sync_fills(con, fb, CAL)
    assert [f["ticker"] for f in execute.inflight_sells(con)] == ["H28", "H29"]
    run2, skipped, notes = execute.final_orders(con, fb, plan(inflight_buys(con, "long")), {"long": 1_000.0},
                                                None, slots, ref)
    assert [(o["side"], o["ticker"], o["qty"]) for o in run2] == [("buy", "N2", 39)] and notes == []
    assert len(skipped) == 2                                                 # the two sells
    # a sell the broker never confirmed brings no money: with it, the same rerun cannot fund N2
    con.execute("UPDATE agent_orders SET alpaca_id = NULL, status = ? WHERE side = 'sell'", [SUBMIT_UNKNOWN])
    run3, _, notes = execute.final_orders(con, fb, plan(inflight_buys(con, "long")), {"long": 1_000.0}, None, slots, ref)
    assert [o["ticker"] for o in run3 if o["side"] == "buy"] == [] and "N2" in notes[0]


def test_a_sell_in_flight_and_planned_again_is_counted_once():
    sells_inflight = [{"book": "insider", "ticker": "S", "qty": 50, "ref_close": 20.0}]
    plans = [_order("insider", "S", "sell", 50, ref=20.0), _order("insider", "A", "buy", 200, limit=10.0)]
    out, notes = guard_buys(plans, {"insider": 1_100.0}, [], None, {"insider": 1_500.0}, None, sells_inflight)
    # 1,100 + 970 once = 2,070 for 2,000 of buys: nothing cut; counted twice it would be 3,040 and hide a cut below
    assert [o["qty"] for o in out] == [50, 200] and notes == []
    out, _ = guard_buys(plans, {"insider": 1_000.0}, [], None, {"insider": 1_500.0}, None, sells_inflight)
    assert out[1]["qty"] == 197


def test_a_buy_in_flight_holds_a_slot_unless_its_name_is_already_a_lot():
    """Review finding (S48 fix): slots, not only cash. With $60,000 of cash, 17 lots + 3 working buys fill the
    insider book's 20 slots: nothing is planned. A working buy of a name already held (the rest of a partial
    fill) takes no second slot: 19 lots + that buy leave one slot, and one name is bought."""
    cfg = BOOKS["insider"]
    lots = [{"ticker": f"H{i}", "qty": 10, "entry_px": 15.0, "hold_until": dt.date(2026, 10, 5)} for i in range(19)]
    ref = {**{f"H{i}": 15.0 for i in range(19)}, **{f"C{i}": 20.0 for i in range(8)}}
    flying = [{"book": "insider", "ticker": t, "qty": 5, "limit_price": 20.6, "ref_close": 20.0} for t in ("C0", "C1", "C2")]
    full = plan_book("insider", cfg, lots[:17], [f"C{i}" for i in range(3, 8)], set(), AS_OF, CAL[6], ref, {}, 60_000.0,
                     set(), tif="day", inflight=flying)
    assert full == []
    held = [{"book": "insider", "ticker": "H0", "qty": 5, "limit_price": 15.45, "ref_close": 15.0}]
    one = plan_book("insider", cfg, lots, [f"C{i}" for i in range(3, 8)], set(), AS_OF, CAL[6], ref, {}, 60_000.0,
                    set(), tif="day", inflight=held)
    assert [o["ticker"] for o in one] == ["C3"] and "slot_usd" not in one[0]


def test_the_unfilled_part_of_a_partial_buy_is_what_stays_in_flight(con):
    fb = FakeBroker()
    coid = _sent(con, fb, _order("insider", "PRT", "buy", 100, limit=10.3))
    fb.fill(coid, 40, 10.0, status="partially_filled")
    sync_fills(con, fb, CAL)
    assert inflight_buys(con, "insider") == [{"book": "insider", "ticker": "PRT", "qty": 60.0, "limit_price": 10.3, "ref_close": 10.0}]


# ---------------------------------------------------------------- pre-submit guard ---
def test_guard_trims_buys_from_the_lowest_priority_and_never_cuts_a_sell():
    slots = {"insider": 1_500.0, "long": 2_000.0}
    plans = [_order("insider", "S", "sell", 50, ref=20.0),                    # 1,000 x 0.97 = 970 of proceeds
             _order("insider", "A", "buy", 100, limit=10.0), _order("insider", "B", "buy", 100, limit=10.0),
             _order("insider", "C", "buy", 50, limit=10.0)]
    out, notes = guard_buys(plans, {"insider": 1_500.0}, [], None, slots)
    # 1,500 + 970 = 2,470 available for 2,500 of buys: C (last) gives up 3 shares
    assert [(o["ticker"], o["qty"]) for o in out] == [("S", 50), ("A", 100), ("B", 100), ("C", 47)] and len(notes) == 1
    out, _ = guard_buys(plans, {"insider": 1_000.0}, [], None, slots)        # 1,970: C dropped, B cut to 97
    assert [(o["ticker"], o["qty"]) for o in out] == [("S", 50), ("A", 100), ("B", 97)]
    # buys in flight use up the book's cash too
    out, _ = guard_buys(plans, {"insider": 1_500.0}, [{"book": "insider", "ticker": "W", "qty": 100, "limit_price": 10.0}],
                        None, slots)
    assert [(o["ticker"], o["qty"]) for o in out] == [("S", 50), ("A", 100), ("B", 47)]
    assert plans[3]["qty"] == 50                                              # the input is not changed


def test_guard_holds_all_books_to_the_broker_cash():
    slots = {"insider": 1_500.0, "long": 2_000.0}
    plans = [_order("long", "L1", "buy", 100, limit=10.0), _order("insider", "I1", "buy", 100, limit=10.0)]
    out, notes = guard_buys(plans, {"long": 5_000.0, "insider": 5_000.0}, [], 1_500.0, slots)
    assert [(o["ticker"], o["qty"]) for o in out] == [("L1", 100), ("I1", 50)] and "account" in notes[0]
    out, _ = guard_buys(plans, {"long": 5_000.0, "insider": 5_000.0}, [], 5_000.0, slots)
    assert [o["qty"] for o in out] == [100, 100]


def test_one_order_is_capped_at_one_and_a_half_slots_and_10k():
    assert order_cap(2_000.0) == 3_000.0 and order_cap(10_000.0) == 10_000.0
    plans = [_order("long", "BIG", "buy", 500, limit=10.0)]                  # $5,000 against a $2,000 slot
    out, notes = guard_buys(plans, {"long": 60_000.0}, [], None, {"long": 2_000.0})
    assert out[0]["qty"] == 300 and "order cap" in notes[0]
    out, _ = guard_buys([_order("core", "SPY", "buy", 20, limit=600.0)], {"core": 20_000.0}, [], None, {"core": 20_000.0})
    assert out[0]["qty"] == 16                                                # $10,000 ceiling over a $30,000 cap


def test_the_run_cap_cuts_buys_never_sells():
    plans = [_order("long", f"B{i}", "buy", 1) for i in range(3)] + [_order("insider", f"S{i}", "sell", 1) for i in range(3)]
    kept = execute.cap_orders(plans, 4)
    assert [o["ticker"] for o in kept] == ["B0", "S0", "S1", "S2"]


# ---------------------------------------------------------------- reconcile ----------
def test_whole_multiples_are_reported_as_possible_splits():
    lots = [{"ticker": t, "qty": 100} for t in ("S", "R", "X")]
    positions = {"S": {"qty": "300"}, "R": {"qty": "10"}, "X": {"qty": "150"}}
    blocked, msgs = reconcile(lots, positions)
    assert blocked == {"S", "R", "X"}
    assert msgs == ["S: lots 100 vs alpaca 300 (possible split 3:1)", "R: lots 100 vs alpaca 10 (possible reverse split 1:10)",
                    "X: lots 100 vs alpaca 150"]
    assert [h["hint"] for h in split_hints(lots, positions)] == ["possible reverse split 1:10", "possible split 3:1"]
    assert split_hints(lots, {"S": {"qty": "100"}, "R": {"qty": "100"}, "X": {"qty": "2500"}}) == []   # 25:1 is out of range


def test_a_reverse_split_that_paid_the_fraction_in_cash_is_reported():
    """100 shares 1:3 -> 33 and a cash payment: reported. 5 of 100 fits 1:17..1:20 alike: 1:20 is exact and wins;
    4 of 100 fits 1:21..1:25 only, out of range: nothing."""
    assert execute.split_hint(100, 33) == "possible reverse split 1:3, fraction paid in cash"
    assert execute.split_hint(100, 5) == "possible reverse split 1:20"
    assert execute.split_hint(100, 4) is None and execute.split_hint(100, 60) is None
    _, msgs = reconcile([{"ticker": "V", "qty": 100}], {"V": {"qty": "33"}})
    assert msgs == ["V: lots 100 vs alpaca 33 (possible reverse split 1:3, fraction paid in cash)"]


def test_cash_check_compares_books_with_the_broker_only_when_nothing_is_in_flight(con):
    total = sum(b["alloc_usd"] for b in BOOKS.values())
    assert cash_check(con, total + 0.4)["ok"] is True
    off = cash_check(con, total + 25.0)
    assert off["ok"] is False and off["diff"] == 25.0
    send_orders(con, FakeBroker(), [_order("long", "AAA", "buy", 1, limit=10.3)], submit=True)
    wait = cash_check(con, total + 25.0)
    assert wait["ok"] is None and wait["orders_in_flight"] == 1


# ---------------------------------------------------------------- --cancel-open -------
class CancelBroker(FakeBroker):
    def account(self):
        return {"account_number": "PA123", "equity": "100000", "cash": "100000"}


def test_cancel_open_lists_without_confirm_and_cancels_only_ours_with_it(monkeypatch, capsys, tmp_path):
    fb = CancelBroker()
    fb.submit("AAA", "buy", 10, "limit", "day", client_id("insider", AS_OF, "AAA", "buy"), 10.3)
    fb.submit("BBB", "sell", 5, "market", "day", client_id("long", AS_OF, "BBB", "sell"))
    fb.submit("MINE", "buy", 1, "market", "day", "placed-by-hand")
    monkeypatch.setattr(execute.broker_mod, "from_env", lambda: fb)
    monkeypatch.setattr(sys, "argv", ["execute", "--cancel-open", "--optradar-db", str(tmp_path / "none.db")])
    assert execute.main() == 0
    out = capsys.readouterr().out
    assert "2 open order(s)" in out and "--confirm" in out and "placed-by-hand" not in out
    assert fb.canceled == [] and not (tmp_path / "none.db").exists()
    monkeypatch.setattr(sys, "argv", ["execute", "--cancel-open", "--confirm"])
    assert execute.main() == 0
    assert sorted(fb.canceled) == sorted([f"id-{client_id('insider', AS_OF, 'AAA', 'buy')}", f"id-{client_id('long', AS_OF, 'BBB', 'sell')}"])


# ---------------------------------------------------------------- the nightly run ----
from agent.tests.test_insider_window import MON, SESSIONS, TUE, insider, store  # noqa: E402,F401  (store is a fixture)


def test_nightly_submit_claims_first_records_each_order_and_a_rerun_sends_only_what_the_broker_lacks(store, tmp_path, monkeypatch):
    """execute.main --submit, insider book, Tuesday 17:00 ET. The broker holds last night's CCC buy, filled, that
    the ledger lacks: it is claimed before the sync, so its lot and cash are there before planning. AAA's POST
    times out after the broker took it (accepted), BBB's is refused (rejected). A rerun sends only BBB again."""
    import types
    from agent.books.data import Market

    alloc = sum(b["alloc_usd"] for b in BOOKS.values())
    orphan = client_id("insider", MON.date(), "CCC", "buy")
    fb = FakeBroker({client_id("insider", TUE.date(), "AAA", "buy"): "timeout_accepted",
                     client_id("insider", TUE.date(), "BBB", "buy"): "reject"})
    fb.submit("CCC", "buy", 10, "limit", "day", orphan, 10.3)
    fb.fill(orphan, 10, 10.0, filled_at="2026-09-22T13:30:00Z")
    fb.account = lambda: {"account_number": "PA1", "equity": str(alloc), "cash": str(alloc - 100.0)}
    fb.calendar = lambda start, end: list(SESSIONS)
    fb.positions = lambda: {"CCC": {"qty": "10"}}

    class Now(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 22, 17, 0, tzinfo=tz)

    def market(_store, _start):
        close = store.bars_wide("close")
        adv = (close * store.bars_wide("volume")).rolling(20).mean()
        return Market(close, close, close, adv, close.notna(), pd.Series(1.0, index=close.index), {})

    monkeypatch.setattr(execute.broker_mod, "from_env", lambda: fb)
    monkeypatch.setattr(execute, "dt", types.SimpleNamespace(date=dt.date, time=dt.time, timedelta=dt.timedelta, datetime=Now))
    class Kept:                                                               # two runs: the store outlives the first
        def __enter__(self):
            return store

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(execute, "PanelStore", lambda read_only=False: Kept())
    monkeypatch.setattr(execute, "load_market", market)
    db = str(tmp_path / "l.db")
    monkeypatch.setattr(sys, "argv", ["execute", "--submit", "--no-update", "--book", "insider", "--optradar-db", db,
                                      "--out-dir", str(tmp_path)])
    insider(store, "AAA", "X", 400_000, TUE)
    insider(store, "BBB", "X", 300_000, MON)
    assert execute.main() == 0
    out = json.load(open(tmp_path / "exec_2026-09-22.json"))
    assert out["claimed_orders"] == [orphan] and out["reconcile"] == [] and out["claim_error"] is None
    assert out["cash_check"]["ok"] is True and out["cash_check"]["diff"] == 0.0
    assert {o["ticker"]: o["status"] for o in out["orders"]} == {"AAA": "accepted", "BBB": "rejected"}
    c = ledger.connect(db, read_only=True)
    assert c.execute("SELECT ticker, qty, entry_px, status FROM agent_lots").fetchall() == [("CCC", 10, 10.0, "open")]
    assert dict(c.execute("SELECT ticker, status FROM agent_orders").fetchall()) == {"CCC": "filled", "AAA": "accepted", "BBB": "rejected"}
    c.close()
    fb.posts.clear()
    assert execute.main() == 0                                                # the same evening, again
    assert fb.posts == [client_id("insider", TUE.date(), "BBB", "buy")]
    out = json.load(open(tmp_path / "exec_2026-09-22.json"))
    assert [o["ticker"] for o in out["orders"]] == ["BBB"]                   # AAA is working: not a candidate again
    assert out["cash_check"]["ok"] is None                                    # AAA is working: not compared
