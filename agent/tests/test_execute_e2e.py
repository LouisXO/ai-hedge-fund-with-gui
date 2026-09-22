"""End-to-end: plan → submit through a fake broker → fills → lots → NAV, on a temp ledger.

No network, no panel: the market inputs are the small dicts the planner
takes. What this guards is the wiring between the pieces of agent/execute.py
that the real run exercises every evening (S20), and the invariants:
idempotent client ids, whole shares, book cash moves with fills, lots
reconcile to positions.
"""
import datetime as dt

import pandas as pd
import pytest

from agent import ledger
from agent.execute import BOOKS, client_id, ensure_books, mark_books, open_lots, plan_book, reconcile, sync_fills


class FakeBroker:
    """Accepts every order; fills buys at ref close + 0.2%, sells at ref close - 0.2%, at the next open."""

    def __init__(self, ref_close: dict[str, float], fill_day: dt.date):
        self.ref, self.fill_day = ref_close, fill_day
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, float] = {}

    def submit(self, symbol, side, qty, order_type, tif, client_order_id, limit_price=None):
        assert qty >= 1 and int(qty) == qty and tif in ("opg", "day")
        px = self.ref[symbol] * (1.002 if side == "buy" else 0.998)
        if side == "buy" and limit_price is not None and px > limit_price:
            status, fq, fpx = "expired", 0, None
        else:
            status, fq, fpx = "filled", qty, px
            self.positions[symbol] = self.positions.get(symbol, 0) + (qty if side == "buy" else -qty)
        o = {"id": f"id-{client_order_id}", "status": status, "filled_qty": str(fq), "filled_avg_price": fpx,
             "filled_at": f"{self.fill_day.isoformat()}T13:30:00Z" if fq else None}
        self.orders[client_order_id] = o
        return dict(o, status="accepted", filled_qty="0", filled_avg_price=None)   # what Alpaca returns at submission

    def order_by_client_id(self, coid):
        return self.orders.get(coid)

    def alpaca_positions(self):
        return {s: {"qty": str(q)} for s, q in self.positions.items() if q}


@pytest.fixture
def con(tmp_path):
    c = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(c)
    yield c
    c.close()


def test_two_evenings_end_to_end(con):
    cal = [dt.date(2026, 9, d) for d in (21, 22, 23, 24, 25, 28, 29, 30)]
    ref = {"AAA": 10.0, "BBB": 20.0, "CCC": 5.0, "DDD": 50.0}
    books = ensure_books(con)
    assert books["long"]["cash_usd"] == BOOKS["long"]["alloc_usd"]

    # evening 1 (bar 9/21): long book enters AAA, BBB; insider enters CCC
    day, nxt = cal[0], cal[1]
    plans = plan_book("long", dict(BOOKS["long"], max_positions=2), [], ["AAA", "BBB", "DDD"], {"AAA", "BBB", "DDD"},
                      day, nxt, ref, {}, books["long"]["cash_usd"], set())
    plans += plan_book("insider", BOOKS["insider"], [], ["CCC"], set(), day, nxt, ref, {"CCC": 0.4},
                       books["insider"]["cash_usd"], {"AAA", "BBB"})
    assert [(o["book"], o["ticker"], o["side"]) for o in plans] == [("long", "AAA", "buy"), ("long", "BBB", "buy"), ("insider", "CCC", "buy")]
    broker = FakeBroker(ref, nxt)
    sent = []
    for o in plans:
        r = broker.submit(o["ticker"], o["side"], o["qty"], o["order_type"], o["tif"], o["client_order_id"], o["limit_price"])
        o.update({"alpaca_id": r["id"], "status": r["status"], "dry_run": False, "submitted_at": pd.Timestamp.now()})
        sent.append(o)
    ledger._insert(con, "agent_orders", pd.DataFrame(sent))

    # evening 2 (bar 9/22): fills sync into lots, cash moves, lots reconcile to positions
    st = sync_fills(con, broker, cal)
    assert st["filled"] == 3 and st["closed"] == 0
    lots = open_lots(con)
    assert {(l["book"], l["ticker"]) for l in lots} == {("long", "AAA"), ("long", "BBB"), ("insider", "CCC")}
    blocked, msgs = reconcile(lots, broker.alpaca_positions())
    assert not blocked and not msgs
    b2 = ensure_books(con)
    spent_long = sum(l["qty"] * l["entry_px"] for l in lots if l["book"] == "long")
    assert b2["long"]["cash_usd"] == pytest.approx(BOOKS["long"]["alloc_usd"] - spent_long)
    ins_lot = next(l for l in lots if l["book"] == "insider")
    assert pd.Timestamp(ins_lot["hold_until"]).date() == cal[1 + 5]          # 5 sessions after the 9/22 fill

    # evening 2 plan: BBB fell out of the keep zone -> sold; DDD enters the freed slot; nothing re-buys AAA
    day, nxt = cal[1], cal[2]
    long_lots = [l for l in lots if l["book"] == "long"]
    plans = plan_book("long", dict(BOOKS["long"], max_positions=2), long_lots, ["AAA", "DDD"], {"AAA", "DDD"},
                      day, nxt, ref, {}, b2["long"]["cash_usd"], {"CCC"})
    assert [(o["ticker"], o["side"], o["reason"]) for o in plans] == [("BBB", "sell", "rank_out"), ("DDD", "buy", "entry")]
    assert plans[0]["client_order_id"] == client_id("long", day, "BBB", "sell")
    for o in plans:
        r = broker.submit(o["ticker"], o["side"], o["qty"], o["order_type"], o["tif"], o["client_order_id"], o["limit_price"])
        o.update({"alpaca_id": r["id"], "status": r["status"], "dry_run": False, "submitted_at": pd.Timestamp.now()})
    ledger._insert(con, "agent_orders", pd.DataFrame(plans))
    st = sync_fills(con, broker, cal)
    assert st["closed"] == 1 and st["filled"] == 1
    closed = con.execute("SELECT ticker, ret_pct FROM agent_lots WHERE status='closed'").fetchall()
    assert closed[0][0] == "BBB" and closed[0][1] == pytest.approx((0.998 / 1.002 - 1) * 100, abs=1e-6)
    nav = mark_books(con, cal[2], ref, 100_000.0)
    long_nav = next(n for n in nav if n["book"] == "long")
    assert long_nav["n_positions"] == 2 and abs(long_nav["equity_usd"] - BOOKS["long"]["alloc_usd"]) < 200   # only spreads paid
    assert not reconcile(open_lots(con), broker.alpaca_positions())[0]
