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
    assert nav["auction_tr"].isna().all() and nav["sim_tr"].isna().all()          # no auction basis: unknown, not the sim NAV


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


# ---------------------------------------------------------------- unknown dividends (2026-10-21) ----
def test_an_unknown_total_return_is_none_in_the_baselines_not_the_dividends_at_zero():
    d = [D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 23)]
    known = _nav([(d[0], "long", 100, 0, 0, 100), (d[1], "long", 99, 95, 0.5, 99.5), (d[2], "long", 101, 96, 0.5, 101.5)])
    gap = _nav([(d[0], "long", 100, 0, 0, 100), (d[1], "long", 99, 95, 0.5, 99.5), (d[2], "long", 101, 96, np.nan, np.nan)])
    a = evaluate.baselines(known, {"long": d[1]}, {"SPY": {}, "QQQ": {}})
    b = evaluate.baselines(gap, {"long": d[1]}, {"SPY": {}, "QQQ": {}})
    assert "tr_unknown_days" not in a["books"]["long"] and b["books"]["long"]["tr_unknown_days"] == ["2026-09-23"]
    assert b["books"]["long"]["ret"]["sim"] == a["books"]["long"]["ret"]["sim"] == pytest.approx(1.0)   # the price basis is known
    assert b["books"]["long"]["ret"]["sim_tr"] is None and b["books"]["long"]["ret"]["auction_tr"] is None
    assert a["books"]["long"]["ret"]["sim_tr"] == pytest.approx(1.5)                  # not 101 / 100 with the 0.5 dropped
    assert b["books"]["long"]["div_cum"] is None
    for k in ("sim_tr", "auction_tr"):
        assert b["combined"]["index"][k][:2] == a["combined"]["index"][k][:2] and b["combined"]["index"][k][2] is None
        assert b["combined"]["ret"][k] is None and b["combined"]["series"]["long"][k][2] is None
    assert b["combined"]["index"]["sim"] == a["combined"]["index"]["sim"]


def _ledger_and_panel(tmp_path, n=30, unknown_last=False):
    from hedge_fund.features.panel import PanelStore
    days = pd.bdate_range("2026-09-28", periods=n)
    rng = np.random.default_rng(3)
    with PanelStore(tmp_path / "p.db") as st:
        for sym in ("SPY", "IWM"):
            px = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
            for x, p in zip(days, px):
                st.con.execute("INSERT INTO index_daily VALUES (?, ?, ?, ?, ?, ?, ?, 'y', now())", [sym, x.date(), p, p, p, p, p])
    con = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(con)
    from agent import auction_basis
    auction_basis.ensure_table(con)
    con.execute("INSERT INTO agent_books VALUES ('long', 60000, 60000, 30, '2026-09-21', now())")
    eq = 60000 * np.cumprod(1 + rng.normal(0, 0.01, n))
    for i, (x, e) in enumerate(zip(days, eq)):
        con.execute("INSERT INTO agent_book_nav VALUES (?, 'long', 0, ?, ?, 1, NULL)", [x.date(), e, e])
        dv = None if unknown_last and i == n - 1 else 2.0
        con.execute(f"INSERT INTO agent_auction_nav ({', '.join(auction_basis.NAV_COLS)}) VALUES (?, 'long', ?, 0, ?, 0, 0, ?, ?, 0, 0)",
                    [x.date(), e, e, dv, None if dv is None else e + dv])
    return con, PanelStore(tmp_path / "p.db", read_only=True), days


@pytest.mark.parametrize("unknown_last", [False, True])
def test_evaluate_leaves_an_unknown_day_out_of_alpha2_and_says_so(tmp_path, unknown_last):
    con, store, days = _ledger_and_panel(tmp_path, unknown_last=unknown_last)
    try:
        nav, _, _ = evaluate.load_books(con)
        assert nav["auction_tr"].isna().sum() == nav["sim_tr"].isna().sum() == int(unknown_last)   # NaN, not equity + 0
        out = evaluate.evaluate(con, store, D(2026, 9, 28), None, [], {})
    finally:
        con.close()
        store.close()
    rec = out["books"]["long"]
    assert rec["alpha2_paper"]["n"] == len(days) - 1 - int(unknown_last)              # no padded 0 return on the last day
    if unknown_last:
        assert rec["tr_unknown_days"] == [str(days[-1].date())] and out["tr_unknown"]["books"] == {"long": [str(days[-1].date())]}
        assert "不按分红 0 计" in out["tr_unknown"]["note"]
    else:
        assert "tr_unknown_days" not in rec and "tr_unknown" not in out                # a normal day writes neither key
