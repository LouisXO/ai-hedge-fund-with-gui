"""S48 pages: benchmarks SPY and QQQ from each book's start, labelled bases, gap to the cross by order kind, counts-only progress."""
import datetime as dt
import sys
import types

import pandas as pd
import pytest

sys.modules.setdefault("site_theme", types.SimpleNamespace(head=lambda *a, **k: "", nav=lambda *a, **k: "", FOOT=""))
from agent import dashboard, evaluate  # noqa: E402

D = dt.date


def _base():
    d = [D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 23)]
    nav = pd.DataFrame([(d[0], "long", 100.0, 0.0, 0.0, 100.0), (d[1], "long", 99.0, 90.0, 0.0, 99.5), (d[2], "long", 101.0, 95.0, 0.5, 102.0)],
                       columns=["as_of", "book", "equity_usd", "market_value_usd", "div_cum", "equity_auction_tr"])
    nav["sim"], nav["sim_tr"], nav["auction_tr"], nav["n_positions"] = nav["equity_usd"], nav["equity_usd"] + nav["div_cum"], nav["equity_auction_tr"], 1
    bench = {"SPY": {d[0]: 10.0, d[1]: 10.1, d[2]: 10.2}, "QQQ": {d[0]: 20.0, d[1]: 20.0, d[2]: 21.0}}
    return evaluate.baselines(nav, {"long": d[1]}, bench), [(x, "long", 0, 1) for x in d]


def test_cards_name_the_start_the_basis_spy_qqq_and_invested_share():
    base, _ = _base()
    html = dashboard.book_cards(base, {"long": 0})
    for s in ("自 2026-09-21 收盘", "模拟器口径含分红", "竞价口径含分红", "SPY", "QQQ", "平均仓位", "首笔成交 2026-09-22"):
        assert s in html
    assert "alpha" not in html.lower()


def test_paper_block_carries_spy_and_qqq_from_the_first_start():
    base, nav = _base()
    b = dashboard.paper_block(base, nav)
    assert b["paper_since"] == "2026-09-21" and b["paper_nav"]["spy"][0] == 100.0 and b["paper_nav"]["qqq"][-1] == pytest.approx(105.0)
    assert b["paper_nav"]["long"][-1] == pytest.approx(101.5)                       # sim + dividends, from the 09-21 close
    assert dashboard.paper_block({"books": {}, "combined": None}, [])["paper_nav"]["days"] == []


def test_auction_fills_are_only_opg_orders_and_the_gap_is_to_the_cross():
    fills = [("long", "A", "buy", D(2026, 9, 30), "2026-09-30 09:30:01", 10.1, 10.0, "limit", "day"),
             ("insider", "B", "buy", D(2026, 9, 30), "2026-09-30 09:30:02", 5.2, 5.0, "market", "day"),
             ("insider", "C", "buy", D(2026, 9, 30), "2026-09-30 09:30:00", 5.0, 5.0, "market", "opg")]
    rows = [{"book": "long", "ticker": "A", "side": "buy", "day": "2026-09-30", "gap_pct": 0.5, "gap_fill": False},
            {"book": "insider", "ticker": "B", "side": "buy", "day": "2026-09-30", "gap_pct": 2.0, "gap_fill": False},
            {"book": "insider", "ticker": "C", "side": "buy", "day": "2026-09-30", "gap_pct": 0.1, "gap_fill": False}]
    f = evaluate.fill_gaps(fills, rows)
    assert [x["kind"] for x in f] == ["day_limit", "day_market", "opg"]
    assert [x["gap"] for x in f] == [0.5, 2.0, 0.1] and f[0]["gap_model"] == pytest.approx(1.0)
    txt = dashboard.gap_summary(f)
    assert "开盘竞价单(OPG) 1 笔 +0.10%" in txt and "DAY 市价单 1 笔 +2.00%" in txt and "对开盘竞价价" in txt


def test_progress_shows_counts_only():
    p = {"evaluate_from": "2026-09-29", "progress": {"long": {"closed": 3, "target": 100, "or_date": "2027-06-30", "on_time": None},
                                                    "insider": {"closed": 5, "target": 200, "or_date": "2027-06-30", "on_time": 4}},
         "orders": {"insider/buy": {"n": 10, "n_filled": 7}}}
    html = dashboard.progress_html(p)
    assert "平仓 3/100" in html and "按时入场 4" in html and "内部人 买 7/10" in html
    assert "alpha" not in html.lower() and "t " not in html.replace("target", "")


def test_week_summary_reads_allocations_and_measures_from_each_books_start(tmp_path, monkeypatch):
    from agent import ledger, week_summary
    from hedge_fund.features.panel import PanelStore
    panel = tmp_path / "panel.db"
    with PanelStore(panel) as st:
        for sym, px in (("SPY", [100, 101, 102, 103, 104]), ("QQQ", [50, 50, 51, 51, 52]), ("IWM", [20, 20, 20, 20, 20])):
            for d, p in zip(["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25"], px):
                st.con.execute("INSERT INTO index_daily VALUES (?, ?, ?, ?, ?, ?, ?, 'y', now())", [sym, d, p, p, p, p + 1, p])   # raw close != adj
        st.con.execute("INSERT INTO bars VALUES ('A', '2026-09-25', 9, 9, 9, 9, 9, 1, 'alpaca', now()), ('B', '2026-09-25', 21, 21, 21, 21, 21, 1, 'alpaca', now())")
    db = str(tmp_path / "o.db")
    con = ledger.connect(db)
    ledger.ensure_schema(con)
    con.execute("CREATE TABLE acct_nav (date DATE, total_assets DOUBLE)")
    for b, a in (("long", 50000.0), ("insider", 20000.0), ("core", 5000.0)):
        con.execute("INSERT INTO agent_books VALUES (?, ?, ?, 1, '2026-09-21', now())", [b, a, a])
        for d in ("2026-09-18", "2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25"):
            eq = a if (d < "2026-09-24" or b != "long") else a * 1.01
            con.execute("INSERT INTO agent_book_nav VALUES (?, ?, 0, 0, ?, 0, NULL)", [d, b, eq])
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, status, filled_qty, filled_at, dry_run)
                   VALUES ('l', 'long', '2026-09-21', 'A', 'buy', 1, 'filled', 1, '2026-09-22 09:30', FALSE),
                          ('i', 'insider', '2026-09-22', 'B', 'buy', 1, 'filled', 1, '2026-09-24 09:30', FALSE),
                          ('c', 'core', '2026-09-24', 'SPY', 'buy', 1, 'filled', 1, '2026-09-25 09:30', FALSE)""")
    con.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, entry_day, entry_px, status) VALUES
                   ('1', 'long', 'A', 100, '2026-09-22', 10, 'open'), ('2', 'long', 'B', 100, '2026-09-22', 20, 'open')""")
    con.close()
    monkeypatch.setattr(week_summary, "AGENT_DIR", tmp_path)
    monkeypatch.setattr(week_summary, "PanelStore", lambda read_only=True: PanelStore(panel, read_only=True))
    assert week_summary.main(["--db", db, "--out", str(tmp_path), "--auction", str(tmp_path / "none.json")]) == 0
    html = (tmp_path / "weekly" / "2026-09-26-第一周总结.html").read_text()
    assert "$75,000" in html                                       # the allocations' sum, not 1e5
    assert "SPY +2.97%" in html                                    # long: 09-21 close (101) -> 104 on adj_close; not from 09-18, not raw close
    assert "占亏损股票合计亏损的 100%" in html                        # A is the only loser: its loss over the losers' sum


def test_week_orders_are_counted_by_their_state_at_the_end_of_the_week(tmp_path):
    import duckdb
    from agent import ledger, week_summary
    db = str(tmp_path / "o.db")
    con = ledger.connect(db)
    ledger.ensure_schema(con)
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, status, filled_qty, filled_at, dry_run) VALUES
                   ('a', 'insider', '2026-09-22', 'A', 'buy', 1, 'expired', 1, '2026-09-23 09:30', FALSE),   -- filled, reads expired later
                   ('b', 'insider', '2026-09-23', 'B', 'buy', 1, 'expired', 0, NULL, FALSE),
                   ('c', 'insider', '2026-09-25', 'C', 'buy', 1, 'filled', 1, '2026-09-28 09:30', FALSE),    -- filled on Monday
                   ('d', 'insider', '2026-09-25', 'D', 'buy', 1, 'expired', 0, NULL, FALSE),                 -- expired on Monday
                   ('e', 'insider', '2026-09-24', 'E', 'buy', 1, 'filled', 1, '2026-09-25 09:30', TRUE),     -- dry run
                   ('f', 'insider', '2026-09-28', 'F', 'buy', 1, 'filled', 1, '2026-09-29 09:30', FALSE)""")  # after the week
    con.close()
    m = duckdb.connect()
    m.execute(f"ATTACH '{db}' AS o (READ_ONLY)")
    got = {s: n for b, s, n in m.execute(week_summary.ORDERS_AS_OF_END, ["2026-09-25"] * 3).fetchall()}
    m.close()
    assert got == {"filled": 1, "expired": 1, "pending": 2}


def test_the_insider_cost_note_uses_the_books_own_gap(tmp_path):
    import json
    from agent import week_summary
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "site"))
    import build_paper
    fills = [{"book": "long", "gap": 0.5}, {"book": "insider", "gap": 1.0}, {"book": "insider", "gap": 1.4}, {"book": "insider", "gap": None}]
    assert build_paper.book_gap(fills, "insider") == (pytest.approx(1.2), 2)
    assert build_paper.book_gap(fills, "core") == (None, 0)
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"fill_rows": [{"book": "insider", "day": "2026-09-25", "gap_pct": 1.0},
                                           {"book": "insider", "day": "2026-09-28", "gap_pct": 3.0},    # after the week
                                           {"book": "long", "day": "2026-09-22", "gap_pct": 0.5}]}))
    assert week_summary.week_gap(str(p), "insider") == (1.0, 1)
    assert week_summary.week_gap(str(tmp_path / "missing.json"), "insider") == (None, 0)
