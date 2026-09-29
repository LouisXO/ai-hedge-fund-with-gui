"""S48 measurement: per-book baselines from the close before the first fill, chained combined index, evaluation maths."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from agent import evaluate, ledger

D = dt.date


def _nav(rows):
    """rows: (day, book, equity, market_value, div_cum, equity_auction_tr)"""
    df = pd.DataFrame(rows, columns=["as_of", "book", "equity_usd", "market_value_usd", "div_cum", "equity_auction_tr"])
    df["sim"] = df["equity_usd"]
    df["sim_tr"] = df["equity_usd"] + df["div_cum"]
    df["auction_tr"] = df["equity_auction_tr"]
    df["n_positions"] = 1
    return df


def test_each_book_starts_at_the_close_before_its_first_fill_and_spy_on_the_same_day():
    d = [D(2026, 9, 18), D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 23)]
    nav = _nav([(d[0], "long", 100, 0, 0, 100), (d[1], "long", 100, 0, 0, 100), (d[2], "long", 98, 95, 0, 99), (d[3], "long", 99, 96, 1, 100.5)])
    spy = {d[0]: 100.0, d[1]: 102.0, d[2]: 101.0, d[3]: 102.0}
    b = evaluate.baselines(nav, {"long": d[2]}, {"SPY": spy, "QQQ": spy})
    x = b["books"]["long"]
    assert x["base_day"] == "2026-09-21" and x["first_fill"] == "2026-09-22"          # not the 09-18 row
    assert x["bench"]["SPY"] == pytest.approx(0.0)                                   # 102 -> 102, not 100 -> 102
    assert x["ret"]["sim"] == pytest.approx(-1.0) and x["ret"]["sim_tr"] == pytest.approx(0.0)
    assert x["ret"]["auction_tr"] == pytest.approx(0.5)
    assert x["exposure_avg_pct"] == pytest.approx((95 / 98 + 96 / 99) / 2 * 100)
    assert b["combined"]["days"][0] == "2026-09-21"


def test_combined_index_chains_daily_returns_so_a_new_book_does_not_jump_it():
    d = [D(2026, 9, 22), D(2026, 9, 23), D(2026, 9, 24), D(2026, 9, 25)]
    nav = _nav([(d[0], "long", 60000, 0, 0, 60000), (d[1], "long", 57000, 57000, 0, 57000), (d[2], "long", 57000, 57000, 0, 57000),
                (d[3], "long", 57000, 57000, 0, 57000),
                (d[2], "core", 10000, 0, 0, 10000), (d[3], "core", 10000, 9000, 0, 10000)])
    b = evaluate.baselines(nav, {"long": d[1], "core": d[3]}, {"SPY": {}, "QQQ": {}})
    idx = b["combined"]["index"]["sim"]
    assert idx == [100.0, 95.0, 95.0, 95.0]                  # the level method would read 67000/70000 = 95.7 on 09-24
    assert b["books"]["core"]["base_day"] == "2026-09-24" and b["books"]["core"]["ret"]["sim"] == 0.0
    assert b["combined"]["series"]["core"]["sim"][:2] == [None, None]


def test_books_without_fills_are_left_out():
    nav = _nav([(D(2026, 9, 22), "insider", 30000, 0, 0, 30000)])
    assert evaluate.baselines(nav, {}, {"SPY": {}, "QQQ": {}}) == {"books": {}, "combined": None, "bases": evaluate.BASIS_LABEL}


def test_load_books_reads_the_first_fill_and_tolerates_a_ledger_without_auction_nav(tmp_path):
    con = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(con)
    con.execute("INSERT INTO agent_books VALUES ('long', 60000, 60000, 30, '2026-09-21', now())")
    con.execute("INSERT INTO agent_book_nav VALUES ('2026-09-21', 'long', 60000, 0, 60000, 0, NULL), ('2026-09-22', 'long', 100, 59000, 59100, 30, NULL)")
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, status, filled_qty, filled_at, dry_run)
                   VALUES ('a', 'long', '2026-09-21', 'X', 'buy', 10, 'filled', 10, '2026-09-22 09:30:13', FALSE),
                          ('b', 'long', '2026-09-21', 'Y', 'buy', 10, 'filled', 10, '2026-09-20 09:30:00', TRUE)""")
    nav, first, alloc = evaluate.load_books(con)
    con.close()
    assert first == {"long": D(2026, 9, 22)} and alloc == {"long": 60000.0}           # the dry run does not count
    assert list(nav["auction_tr"]) == [60000.0, 59100.0]


def test_clustered_mean_uses_trading_days_as_clusters():
    x = np.array([1.0, 1.0, 1.0, 0.0])
    r = evaluate.clustered_mean(x, ["d1", "d1", "d1", "d2"])
    assert r["n"] == 4 and r["n_clusters"] == 2 and r["mean"] == pytest.approx(0.75)
    # residual sums per day: +0.75 and -0.75 -> sqrt(2 * 0.5625 * 2) / 4
    assert r["se"] == pytest.approx(np.sqrt(2 * 0.5625 * 2) / 4)
    assert evaluate.clustered_mean(np.array([]), [])["mean"] is None


def test_tracking_counts_only_days_after_evaluate_from():
    idx = pd.to_datetime(["2026-09-26", "2026-09-29", "2026-09-30", "2026-10-01"])
    paper = pd.Series([90.0, 100.0, 101.0, 102.01], index=idx)
    replay = pd.Series([50.0, 100.0, 100.0, 100.0], index=idx)
    t = evaluate.tracking(paper, replay, pd.Timestamp("2026-09-29"))
    assert t["n_days"] == 2 and t["mean_bp"] == pytest.approx(100.0) and t["cum_pct"] == pytest.approx(2.01)
    assert t["from"] == "2026-09-29"


def test_execution_and_fill_rate_split_by_book_and_side():
    rows = [{"book": "long", "side": "buy", "day": "2026-09-30", "gap_pct": 0.5, "cross": 10.0, "gap_fill": False},
            {"book": "long", "side": "buy", "day": "2026-09-22", "gap_pct": 9.0, "cross": 10.0, "gap_fill": False},   # before: out
            {"book": "insider", "side": "sell", "day": "2026-10-01", "gap_pct": None, "cross": None, "gap_fill": False}]
    e = evaluate.execution(rows, D(2026, 9, 29))
    assert e["long/buy"]["n"] == 1 and e["long/buy"]["mean"] == 0.5
    assert e["insider/sell"]["n"] == 0 and e["insider/sell"]["n_no_cross"] == 1
    orders = pd.DataFrame([{"book": "insider", "as_of": D(2026, 9, 29), "side": "buy", "qty": 10, "status": "filled", "filled_qty": 10.0},
                           {"book": "insider", "as_of": D(2026, 9, 29), "side": "buy", "qty": 10, "status": "expired", "filled_qty": 0.0},
                           {"book": "insider", "as_of": D(2026, 9, 29), "side": "buy", "qty": 10, "status": "accepted", "filled_qty": None}])
    f = evaluate.fill_rate(orders, D(2026, 9, 29))
    assert f["insider/buy"] == {"n": 2, "n_filled": 1, "share": 0.5, "qty_share": 0.5}   # the working order is not final
