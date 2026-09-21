"""Paper execution: the engine's step as orders, idempotent ids, reconciliation, broker guards."""
import datetime as dt

import pandas as pd
import pytest

from agent import ledger
from agent.broker.alpaca import NotPaperAccount, PaperBroker
from agent.execute import BOOKS, client_id, plan_book, reconcile, sync_fills

AS_OF = dt.date(2026, 9, 21)
NEXT = dt.date(2026, 9, 22)


def _lot(t, qty, hold_until=None, px=10.0):
    return {"lot_id": f"x|{t}", "book": "long", "ticker": t, "qty": qty, "entry_day": AS_OF, "entry_px": px,
            "hold_until": hold_until}


def test_long_book_enters_top_n_in_rank_order_and_exits_outside_keep():
    cfg = dict(BOOKS["long"], max_positions=3)
    lots = [_lot("A", 100), _lot("B", 100), _lot("Z", 100)]      # Z fell out of the top 2N
    ranked = ["A", "C", "D", "E"]                                 # top N; A already held
    keep = {"A", "B", "C", "D", "E"}
    close = {t: 10.0 for t in "ABCDEZ"}
    orders = plan_book("long", cfg, lots, ranked, keep, AS_OF, NEXT, close, {}, cash_usd=1000.0, blocked=set())
    sells = [o for o in orders if o["side"] == "sell"]
    buys = [o for o in orders if o["side"] == "buy"]
    assert [o["ticker"] for o in sells] == ["Z"] and sells[0]["order_type"] == "market" and sells[0]["tif"] == "opg"
    assert [o["ticker"] for o in buys] == ["C"]                   # one slot freed by Z, highest-ranked new name
    b = buys[0]
    assert b["order_type"] == "limit" and b["tif"] == "opg" and b["limit_price"] == pytest.approx(10.30)
    # slot = equity / N = (1000 + 3*100*10) / 3; cash plan includes Z's proceeds
    assert b["qty"] == int(((1000 + 3000) / 3) // 10.30)


def test_no_entries_when_not_scored_and_no_exits_either():
    cfg = dict(BOOKS["long"], max_positions=3)
    lots = [_lot("A", 100)]
    orders = plan_book("long", cfg, lots, ["B"], set(), AS_OF, NEXT, {"A": 10, "B": 10}, {}, 5000.0, set(), scored=False)
    assert orders == []


def test_insider_book_exits_on_hold_until_and_caps_entry_at_one_spread():
    cfg = BOOKS["insider"]
    lots = [dict(_lot("H", 50, hold_until=NEXT), book="insider"), dict(_lot("K", 50, hold_until=dt.date(2026, 9, 25)), book="insider")]
    orders = plan_book("insider", cfg, lots, ["N"], set(), AS_OF, NEXT, {"H": 20.0, "K": 20.0, "N": 8.0},
                       {"N": 0.4}, cash_usd=28_000.0, blocked=set())
    assert [(o["ticker"], o["side"]) for o in orders] == [("H", "sell"), ("N", "buy")]
    assert orders[1]["limit_price"] == pytest.approx(round(8.0 * 1.004, 2))   # cents
    assert orders[1]["reason"] == "entry" and orders[0]["reason"] == "hold_expired"


def test_blocked_and_held_elsewhere_are_skipped_and_ids_are_deterministic():
    cfg = dict(BOOKS["long"], max_positions=5)
    orders = plan_book("long", cfg, [], ["A", "B", "C"], {"A", "B", "C"}, AS_OF, NEXT,
                       {"A": 10.0, "B": 10.0, "C": 10.0}, {}, 60_000.0, blocked={"B"})
    assert [o["ticker"] for o in orders] == ["A", "C"]
    assert orders[0]["client_order_id"] == client_id("long", AS_OF, "A", "buy") == "long|2026-09-21|A|buy"


def test_whole_shares_and_notional_cap():
    cfg = dict(BOOKS["long"], max_positions=1)
    orders = plan_book("long", cfg, [], ["X"], {"X"}, AS_OF, NEXT, {"X": 1000.0}, {}, 60_000.0, set())
    assert orders[0]["qty"] == 9                                  # min(slot 60k, cap 10k) / 1030 -> 9 whole shares
    orders = plan_book("long", cfg, [], ["X"], {"X"}, AS_OF, NEXT, {"X": 15_000.0}, {}, 60_000.0, set())
    assert orders == []                                           # cannot afford one share within the cap


def test_reconcile_flags_mismatch_and_unmanaged_positions():
    lots = [_lot("A", 100), _lot("B", 50)]
    positions = {"A": {"qty": "100"}, "B": {"qty": "49"}, "M": {"qty": "10"}}
    blocked, msgs = reconcile(lots, positions)
    assert blocked == {"B", "M"} and len(msgs) == 2


class FakeBroker:
    def __init__(self, orders):
        self.orders = orders

    def order_by_client_id(self, coid):
        return self.orders.get(coid)


def test_sync_fills_opens_and_closes_lots_and_moves_cash(tmp_path):
    con = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(con)
    con.execute("INSERT INTO agent_books VALUES ('insider', 30000, 30000, 20, ?, now())", [AS_OF])
    buy_id = client_id("insider", AS_OF, "N", "buy")
    con.execute("INSERT INTO agent_orders (client_order_id, book, ticker, side, qty, dry_run, status) VALUES (?, 'insider', 'N', 'buy', 10, FALSE, 'new')", [buy_id])
    cal = [dt.date(2026, 9, d) for d in (21, 22, 23, 24, 25, 28, 29, 30)]
    fb = FakeBroker({buy_id: {"id": "o1", "status": "filled", "filled_qty": "10", "filled_avg_price": "8.05",
                              "filled_at": "2026-09-22T13:30:01Z"}})
    st = sync_fills(con, fb, cal)
    assert st["filled"] == 1
    lot = con.execute("SELECT qty, entry_day, entry_px, hold_until, status FROM agent_lots").fetchone()
    assert lot[0] == 10 and lot[1] == dt.date(2026, 9, 22) and lot[2] == 8.05
    assert lot[3] == dt.date(2026, 9, 29) and lot[4] == "open"      # 5 sessions after the 9/22 fill
    assert con.execute("SELECT cash_usd FROM agent_books").fetchone()[0] == pytest.approx(30000 - 80.5)
    sell_id = client_id("insider", dt.date(2026, 9, 28), "N", "sell")
    con.execute("INSERT INTO agent_orders (client_order_id, book, ticker, side, qty, dry_run, status) VALUES (?, 'insider', 'N', 'sell', 10, FALSE, 'new')", [sell_id])
    fb.orders[sell_id] = {"id": "o2", "status": "filled", "filled_qty": "10", "filled_avg_price": "8.86",
                          "filled_at": "2026-09-29T13:30:01Z"}
    st = sync_fills(con, fb, cal)
    assert st["closed"] == 1
    lot = con.execute("SELECT status, exit_px, ret_pct FROM agent_lots").fetchone()
    assert lot[0] == "closed" and lot[2] == pytest.approx((8.86 / 8.05 - 1) * 100)
    assert con.execute("SELECT cash_usd FROM agent_books").fetchone()[0] == pytest.approx(30000 - 80.5 + 88.6)
    assert sync_fills(con, fb, cal)["checked"] == 0                 # final orders are not re-queried
    con.close()


def test_broker_refuses_anything_but_paper():
    with pytest.raises(NotPaperAccount):
        PaperBroker("AKXXXX", "s")                                  # live-style key
    with pytest.raises(NotPaperAccount):
        PaperBroker("PKXXXX", "s", base_url="https://api.alpaca.markets")
    b = PaperBroker("PKXXXX", "s")
    assert b.base.startswith("https://paper-api.alpaca.markets")
