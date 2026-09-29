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
    assert b["order_type"] == "market" and b["tif"] == "opg" and b["limit_price"] is None   # market-on-open in the auction
    # slot = equity / N = (1000 + 3*100*10) / 3, sized off the capped price; cash plan includes Z's proceeds
    assert b["qty"] == int(((1000 + 3000) / 3) // 10.30)
    day = plan_book("long", cfg, lots, ranked, keep, AS_OF, NEXT, close, {}, cash_usd=1000.0, blocked=set(), tif="day")
    db = [o for o in day if o["side"] == "buy"][0]
    assert db["order_type"] == "limit" and db["limit_price"] == pytest.approx(10.30)             # DAY fallback keeps the cap


def test_no_entries_when_not_scored_and_no_exits_either():
    cfg = dict(BOOKS["long"], max_positions=3)
    lots = [_lot("A", 100)]
    orders = plan_book("long", cfg, lots, ["B"], set(), AS_OF, NEXT, {"A": 10, "B": 10}, {}, 5000.0, set(), scored=False)
    assert orders == []


def test_insider_book_exits_on_hold_until_and_caps_entry():
    cfg = BOOKS["insider"]
    lots = [dict(_lot("H", 50, hold_until=NEXT), book="insider"), dict(_lot("K", 50, hold_until=dt.date(2026, 9, 25)), book="insider")]
    orders = plan_book("insider", cfg, lots, ["N"], set(), AS_OF, NEXT, {"H": 20.0, "K": 20.0, "N": 8.0},
                       {"N": 0.4}, cash_usd=28_000.0, blocked=set())
    assert [(o["ticker"], o["side"]) for o in orders] == [("H", "sell"), ("N", "buy")]
    assert orders[1]["order_type"] == "market" and orders[1]["limit_price"] is None
    day = plan_book("insider", cfg, lots, ["N"], set(), AS_OF, NEXT, {"H": 20.0, "K": 20.0, "N": 8.0},
                    {"N": 0.4}, cash_usd=28_000.0, blocked=set(), tif="day")
    assert day[1]["order_type"] == "limit" and day[1]["limit_price"] == 8.24     # DAY: close + 3%, never market (2026-09-28)
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


def test_insider_day_entry_is_a_capped_limit():
    """No book buys at market on DAY orders: a market order pays the ask (INBX 2026-09-28, 105.33 vs a 98.69 open)."""
    cfg = {"max_positions": 20, "entry_cap_pct": 3.0}
    o = plan_book("insider", cfg, [], ["N"], set(), AS_OF, NEXT, {"N": 10.0}, {"N": 0.4}, cash_usd=28_000.0, blocked=set(), tif="day")
    assert o and o[0]["order_type"] == "limit" and o[0]["tif"] == "day" and o[0]["limit_price"] == 10.30


def test_unfilled_insider_orders_are_retried_once_filled_ones_block():
    from agent.execute import blocking_orders
    rows = [("A", "filled", 10), ("B", "expired", 0), ("C", "accepted", 0), ("D", "expired", 0), ("D", "canceled", 0)]
    skip, retry = blocking_orders(rows)
    assert skip == {"A", "C", "D"}          # filled, still working, and two misses
    assert retry == {"B"}                    # one miss: retry once


def test_frozen_ticker_gets_no_order_of_either_side():
    """A lot that does not match the broker's position (split, merger, manual trade) is frozen: its qty is not
    what the account holds, so neither the expired-hold sell nor a re-entry is planned (2026-09-28 audit)."""
    cfg = BOOKS["insider"]
    lots = [dict(_lot("H", 50, hold_until=NEXT), book="insider"), dict(_lot("K", 50, hold_until=NEXT), book="insider")]
    orders = plan_book("insider", cfg, lots, ["N"], set(), AS_OF, NEXT, {"H": 20.0, "K": 20.0, "N": 8.0},
                       {"N": 0.4}, cash_usd=28_000.0, blocked={"H"}, tif="day", frozen={"H"})
    assert [(o["ticker"], o["side"]) for o in orders] == [("K", "sell"), ("N", "buy")]


def _panel(rows: dict[str, list]):
    """date x ticker frame over four sessions, None = no bar."""
    return pd.DataFrame(rows, index=pd.to_datetime(["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]), dtype=float)


def test_bars_coverage_counts_liquid_names_that_lost_their_bar():
    """The date check passes when one symbol has today's bar; coverage is what sees a batch that failed
    (2026-09-28 audit: three batches of 100 without data for a week)."""
    from agent.execute import COVERAGE_MIN, bars_coverage
    names = [f"S{i:02d}" for i in range(50)]
    close = _panel({t: [10.0] * 4 for t in names} | {"THIN": [10.0] * 4, "NEWCO": [None, None, None, 10.0]})
    adv = _panel({t: [5e6] * 4 for t in names} | {"THIN": [1e6] * 4, "NEWCO": [None] * 4})
    day = close.index[-1]
    assert bars_coverage(close, adv, day) == 1.0
    close.loc[day, ["S00", "S01", "THIN"]] = None                 # THIN is under $3M: not in the base
    cov = bars_coverage(close, adv, day)
    assert cov == pytest.approx(48 / 50) and cov < COVERAGE_MIN   # nothing is planned
    assert f"bars coverage {cov:.1%}" == "bars coverage 96.0%"    # the skipped_reason main() writes
    close.loc[day, "S01"] = 10.0
    assert bars_coverage(close, adv, day) == pytest.approx(0.98)  # one name gone in fifty: a delisting, not a failure
    assert not bars_coverage(close, adv, day) < COVERAGE_MIN
    assert bars_coverage(close, adv, close.index[0]) is None      # no earlier session to compare with


def test_held_ticker_without_a_bar_is_kept_not_sold_as_rank_out():
    """A data gap must not cause a sale: GAP has no bar on the scoring day, so it has no rank and is not in keep."""
    from agent.execute import held_without_bar
    cfg = dict(BOOKS["long"], max_positions=4)
    lots = [_lot("A", 100), _lot("GAP", 100), _lot("NUL", 100), _lot("Z", 100)]
    adj = _panel({"A": [10.0] * 4, "NUL": [10.0, 10.0, 10.0, None], "Z": [10.0] * 4, "C": [10.0] * 4})   # GAP: no column at all
    day = adj.index[-1]
    no_bar = held_without_bar(lots, adj, day)
    assert no_bar == ["GAP", "NUL"]
    assert held_without_bar(lots, adj, adj.index[-2]) == ["GAP"]
    keep = {"A", "C"}                                             # what the ranking returned: Z really fell out
    close = {"A": 10.0, "Z": 10.0, "C": 10.0}
    before = plan_book("long", cfg, lots, ["A", "C"], keep, AS_OF, NEXT, close, {}, cash_usd=1000.0, blocked=set(), tif="day")
    assert [(o["ticker"], o["reason"]) for o in before if o["side"] == "sell"] == [("GAP", "rank_out"), ("NUL", "rank_out"), ("Z", "rank_out")]
    orders = plan_book("long", cfg, lots, ["A", "C"], keep | set(no_bar), AS_OF, NEXT, close, {}, cash_usd=1000.0,
                       blocked=set(), tif="day")
    assert [(o["ticker"], o["side"], o["reason"]) for o in orders] == [("Z", "sell", "rank_out"), ("C", "buy", "entry")]
    # an insider lot leaves on its date whether or not it has a bar: the exit is not a ranking
    ins = [dict(_lot("GAP", 50, hold_until=NEXT), book="insider")]
    out = plan_book("insider", BOOKS["insider"], ins, [], set(), AS_OF, NEXT, {}, {}, cash_usd=0.0, blocked=set(), tif="day")
    assert [(o["ticker"], o["reason"]) for o in out] == [("GAP", "hold_expired")]


def test_bars_guard_reason_is_what_main_plans_on():
    """main() takes skipped_reason from this function: under 98% coverage nothing is planned."""
    from agent.execute import bars_guard_reason
    assert bars_guard_reason(0.9741) == "bars coverage 97.41%"
    assert bars_guard_reason(0.97996) == "bars coverage 97.99%"          # floored: never reads as 98.00%
    assert bars_guard_reason(0.98) is None and bars_guard_reason(1.0) is None
    assert bars_guard_reason(None) is None                               # no earlier session: no guard


def test_long_keep_holds_a_name_without_a_bar_and_plan_book_does_not_sell_it():
    from agent.execute import long_keep
    cfg = dict(BOOKS["long"], max_positions=3)
    lots = [_lot("A", 100), _lot("GAP", 100), _lot("Z", 100)]
    keep = long_keep({"A", "C"}, ["GAP"])
    assert keep == {"A", "C", "GAP"}
    orders = plan_book("long", cfg, lots, ["A", "C"], keep, AS_OF, NEXT, {"A": 10.0, "Z": 10.0, "C": 10.0}, {},
                       cash_usd=1000.0, blocked=set(), tif="day")
    assert [(o["ticker"], o["side"], o["reason"]) for o in orders] == [("Z", "sell", "rank_out"), ("C", "buy", "entry")]


def test_a_failed_bars_update_is_named_with_failed():
    from agent.execute import bars_update_note
    assert bars_update_note(None) is None
    assert bars_update_note({"failed_symbols": [], "n_batches_failed": 0, "rows": 5}) is None
    assert bars_update_note({"error": "IO Error: database is locked", "failed_symbols": [], "n_batches_failed": 0}) \
        == "bars update failed: IO Error: database is locked"
    note = bars_update_note({"failed_symbols": ["AAA", "BBB"], "n_batches_failed": 1})
    assert "failed" in note and "2 symbols" in note and "AAA" in note


def test_class_shares_are_not_entered_but_a_held_one_is_kept():
    from agent.execute import entry_candidates
    assert entry_candidates(["AAA", "BRK-A", "LGF.B", "BWL-A", "BBB", "SPY"]) == ["AAA", "BBB", "SPY"]
    cfg = dict(BOOKS["long"], max_positions=3)
    lots = [_lot("BWL-A", 100)]
    ranked = entry_candidates(["BRK-A", "AAA"])
    orders = plan_book("long", cfg, lots, ranked, {"BWL-A", "AAA", "BRK-A"}, AS_OF, NEXT,
                       {"BWL-A": 30.0, "AAA": 10.0, "BRK-A": 700_000.0}, {}, cash_usd=3000.0, blocked=set(), tif="day")
    assert [(o["ticker"], o["side"]) for o in orders] == [("AAA", "buy")]
