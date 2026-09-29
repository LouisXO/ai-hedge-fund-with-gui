"""Daily review (S48): which real-account deals a review covers, FIFO option summary, lesson classes and
dating, the closed-lot table and the notification. Temp files and in-memory DuckDB; no network."""
import datetime as dt
import json
import types

import duckdb
import pandas as pd
import pytest

from agent import ledger
from agent import review as R

D = dt.date


def _deals(rows):
    df = pd.DataFrame(rows, columns=["deal_id", "ts", "code", "name", "side", "qty", "price", "is_option", "underlying"])
    df["ts"] = pd.to_datetime(df["ts"])
    df["day"] = df["ts"].dt.date
    return df


DEALS = _deals([                                          # made-up contracts and prices (this repository is public)
    ("1", "2026-10-07 10:05:00", "US.AAA261007C100000", "", "BUY", 1, 2.00, True, "US.AAA"),
    ("2", "2026-10-07 10:20:00", "US.AAA261007C100000", "", "BUY", 1, 1.00, True, "US.AAA"),
    ("3", "2026-10-07 14:30:00", "US.BBB261016C50000", "", "BUY", 1, 3.00, True, "US.BBB"),       # reached the table a day late
    ("4", "2026-10-08 01:00:00", "US.AAA261007C100000", "", "SELL", 2, 0.0, True, "US.AAA"),      # expiry settlement
    ("5", "2026-10-08 15:50:00", "US.CCC261009P40000", "", "BUY", 1, 2.50, True, "US.CCC"),
    ("6", "2026-10-10 00:40:00", "US.CCC261009P40000", "", "SELL", 1, 0.0, True, "US.CCC"),       # Saturday settlement
    ("7", "2026-09-15 15:00:00", "US.DDD261016C20000", "", "BUY", 4, 1.00, True, "US.DDD"),
    ("8", "2026-09-25 14:00:00", "US.DDD261016C20000", "", "SELL", 4, 2.00, True, "US.DDD"),
])


def _write_review(out, day, deals):
    with open(out / f"review_{day}.json", "w") as f:
        json.dump({"real": {"deals": deals}}, f)


def test_review_takes_deals_after_the_last_review_plus_late_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    # the 10-07 review saw only the morning fills (old files have no deal_id: matched by time|code)
    _write_review(tmp_path, "2026-10-07", [{"ts": "2026-10-07 10:05:00.123000", "code": "US.AAA261007C100000"},
                                           {"ts": "2026-10-07 10:20:00.456000", "code": "US.AAA261007C100000"}])
    _write_review(tmp_path, "2026-10-08", [{"ts": "2026-10-08 01:00:00", "code": "US.AAA261007C100000", "deal_id": "4"}])
    _write_review(tmp_path, "2026-10-09", [])
    _write_review(tmp_path, "2026-10-13", [])                          # a later file never hides deals from an earlier day
    keys, last, first = R.earlier_reviews(D(2026, 10, 12))
    assert (last, first) == (D(2026, 10, 9), D(2026, 10, 7))
    got = R.select_deals(DEALS, D(2026, 10, 12), D(2026, 10, 9), keys, last, first)
    assert list(got["deal_id"]) == ["3", "5", "6"]                     # BBB and CCC late, Saturday's settlement on Monday
    keys, last, first = R.earlier_reviews(D(2026, 10, 8))
    got = R.select_deals(DEALS, D(2026, 10, 8), D(2026, 10, 7), keys, last, first)
    assert list(got["deal_id"]) == ["3", "4", "5"]                     # rerun of 10-08 shows its own settlement again
    # no review before: only the window after the previous session
    keys, last, first = R.earlier_reviews(D(2026, 10, 7))
    assert R.select_deals(DEALS, D(2026, 10, 7), D(2026, 10, 6), keys, last, first)["deal_id"].tolist() == ["1", "2", "3"]


def test_fifo_and_option_summary_by_days_to_expiry():
    trips = R.fifo_round_trips(DEALS)
    aaa = [t for t in trips if t["code"].startswith("US.AAA")][0]
    assert aaa["qty"] == 2 and aaa["avg_cost"] == pytest.approx(1.50) and aaa["exit_px"] == 0
    pos = [("US.BBB261016C50000", "", 1, 3.0, 0.2, 20.0, -280.0, True, "US.BBB")]
    s = {r["bucket"]: r for r in R.options_summary(DEALS, D(2026, 10, 12), pos)}
    assert s["0–1 天"]["n"] == 2 and s["0–1 天"]["premium"] == pytest.approx(550.0)
    assert s["0–1 天"]["expired_worthless"] == pytest.approx(550.0) and s["0–1 天"]["realized"] == pytest.approx(-550.0)
    assert s["2–10 天"]["n_open"] == 1 and s["2–10 天"]["open_mark_pnl"] == pytest.approx(20.0 - 300.0)
    assert s["> 10 天"]["realized"] == pytest.approx((2.00 - 1.00) * 4 * 100)
    assert R.options_summary(DEALS, D(2026, 11, 30), pos) == []        # nothing opened in the last 30 days


def test_lessons_classes_dates_and_rerun(tmp_path, monkeypatch):
    lp = tmp_path / "lessons.jsonl"
    monkeypatch.setattr(R, "LESSONS", str(lp))
    lp.write_text(json.dumps({"date": "2026-09-28", "rule": "paper_exec_gap", "text": "old"}) + "\n"
                  + json.dumps({"date": "2026-09-23", "rule": "real_0dte", "text": "AAA"}) + "\n")
    flags = [{"rule": "paper_exec_gap", "text": "gap", "cls": "sim", "date": "2026-09-28", "key": "paper_exec_gap"},
             {"rule": "paper_rejected", "text": "1 rejected", "cls": "action", "date": "2026-09-28", "key": "paper_rejected"},
             {"rule": "real_short_dte", "text": "BBB", "cls": "real", "date": "2026-09-23", "key": "BBB|BUY"},
             {"rule": "real_concentration", "text": "ZZZ", "cls": "info", "info": True, "date": "2026-09-28", "key": "conc"},
             {"rule": "paper_unfilled", "text": "legacy flag without a class"}]
    c = R.record_lessons(D(2026, 9, 28), flags)
    rows = [json.loads(l) for l in lp.read_text().splitlines()]
    assert {(r["date"], r["rule"]) for r in rows} == {("2026-09-23", "real_0dte"), ("2026-09-23", "real_short_dte"), ("2026-09-28", "paper_rejected")}
    assert c == {"real_0dte": 1, "real_short_dte": 1, "paper_rejected": 1}
    R.record_lessons(D(2026, 9, 28), flags)                            # rerun: same rows
    assert len(lp.read_text().splitlines()) == 3
    R.record_lessons(D(2026, 9, 29), flags[2:3])                        # the same deal seen again by a later review: not twice
    assert len(lp.read_text().splitlines()) == 3


def test_lessons_survive_reruns_out_of_order(tmp_path, monkeypatch):
    """Rerun the later day, then the earlier one, then the later one again: the late deal's lesson moves to the
    earlier review (which shows the deal from then on) and is not dropped by the second rerun of the later day."""
    lp = tmp_path / "lessons.jsonl"
    monkeypatch.setattr(R, "LESSONS", str(lp))
    ba = {"rule": "real_short_dte", "text": "AAA BUY(2026-09-23):买入时只剩 2 天", "cls": "real", "date": "2026-09-23", "key": "US.AAA|BUY"}
    cost = {"rule": "real_short_dte", "text": "BBB BUY(2026-09-25):买入时只剩 3 天", "cls": "real", "date": "2026-09-25", "key": "US.BBB|BUY"}
    rows = lambda: sorted((r["date"], r["rule"], r["key"], r["text"]) for r in map(json.loads, lp.read_text().splitlines()))
    R.record_lessons(D(2026, 9, 28), [ba, cost])        # the later review saw the late deal first
    first = rows()
    R.record_lessons(D(2026, 9, 23), [ba])              # the earlier day rerun: its window now holds the deal
    assert rows() == first
    assert {r["key"]: r["review"] for r in map(json.loads, lp.read_text().splitlines())} == {"US.AAA|BUY": "2026-09-23", "US.BBB|BUY": "2026-09-28"}
    R.record_lessons(D(2026, 9, 28), [cost])            # the later day rerun: the earlier review file shows the deal now
    assert rows() == first
    R.record_lessons(D(2026, 9, 23), [ba])
    assert rows() == first


class FakeStore:
    def __init__(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE TABLE index_daily (symbol VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, adj_close DOUBLE)")
        self.con.execute("CREATE TABLE bars (ticker VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, adj_close DOUBLE)")

    def index_series(self, symbol, col):
        df = self.con.execute(f"SELECT trade_date, {col} FROM index_daily WHERE symbol = ? ORDER BY 1", [symbol]).df()
        return pd.Series(df[col].to_numpy(), index=pd.to_datetime(df["trade_date"]))


def _paper_fixture(tmp_path, monkeypatch):
    days = [d.date() for d in pd.bdate_range("2026-09-21", "2026-09-30")]
    st = FakeStore()
    for i, d in enumerate(days):
        st.con.execute("INSERT INTO index_daily VALUES ('SPY', ?, ?, ?, ?), ('IWM', ?, ?, ?, ?)",
                       [d, 600 + i, 600.5 + i, 600.5 + i, d, 200 - i, 199.5 - i, 199.5 - i])
        st.con.execute("INSERT INTO bars VALUES ('AAA', ?, ?, ?, ?, ?, ?)", [d, 10 + i, 11 + i, 9 + i, 10.5 + i, 10.5 + i])
        drop = 0.85 if d == days[-1] else 1.0                          # BBB loses 15% on the last day
        st.con.execute("INSERT INTO bars VALUES ('BBB', ?, 20, 21, 16, ?, ?)", [d, 20 * drop, 20 * drop])
    con = ledger.connect(str(tmp_path / "l.db"))
    ledger.ensure_schema(con)
    day, prev = days[-1], days[-2]
    con.execute("INSERT INTO agent_books VALUES ('insider', 30000, 20000, 20, ?, now())", [days[0]])
    con.execute("INSERT INTO agent_book_nav (as_of, book, equity_usd, cash_usd, n_positions) VALUES (?, 'insider', 29000, 20000, 1), (?, 'insider', 29500, 20000, 1)", [day, prev])
    con.execute("""INSERT INTO agent_lots (lot_id, book, ticker, qty, entry_day, entry_px, exit_day, exit_px, ret_pct, status)
                   VALUES ('l1', 'insider', 'AAA', 100, ?, 10.10, ?, 17.00, ?, 'closed'),
                          ('l2', 'insider', 'BBB', 50, ?, 20.0, NULL, NULL, NULL, 'open')""", [days[1], day, (17.0 / 10.10 - 1) * 100, days[1]])
    con.execute("""INSERT INTO agent_orders (client_order_id, book, as_of, ticker, side, qty, order_type, status, filled_qty, dry_run)
                   VALUES ('o1', 'insider', ?, 'CCC', 'buy', 10, 'limit', 'rejected', 0, FALSE),
                          ('o2', 'insider', ?, 'DDD', 'buy', 10, 'limit', 'expired', 0, FALSE)""", [prev, prev])
    adb = tmp_path / "auctions.db"
    a = duckdb.connect(str(adb))
    a.execute("CREATE TABLE auctions (ticker VARCHAR, day DATE, open_px DOUBLE)")
    a.execute("INSERT INTO auctions VALUES ('AAA', ?, 10.0), ('AAA', ?, 17.34)", [days[1], day])
    a.close()
    monkeypatch.setattr(R, "AUCTIONS_DB", adb)
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(R, "headlines", lambda *a, **k: {})
    (tmp_path / f"sync_{day}.json").write_text(json.dumps({"reconcile": ["BBB: lots 50 vs broker 40"]}))
    # the previous session's 16:10 run: one order refused at the POST (not in agent_orders), one accepted
    (tmp_path / f"exec_{prev}.json").write_text(json.dumps({"orders": [
        {"book": "insider", "ticker": "EEE", "side": "buy", "qty": 5, "order_type": "limit", "status": "error: 403 insufficient buying power"},
        {"book": "insider", "ticker": "DDD", "side": "buy", "qty": 10, "order_type": "limit", "status": "accepted"}]}))
    return con, st, day, prev, days


def test_paper_section_closed_lots_and_rule_classes(tmp_path, monkeypatch):
    con, st, day, prev, days = _paper_fixture(tmp_path, monkeypatch)
    out = R.paper_section(con, st, day, prev, 0.1)
    (c,) = out["closed"]
    assert c["ticker"] == "AAA" and c["hold_sessions"] == len(days) - 2
    assert c["entry_vs_cross_pct"] == pytest.approx(1.0) and c["exit_vs_cross_pct"] == pytest.approx((17.0 / 17.34 - 1) * 100)
    assert c["spy_pct"] == pytest.approx(((600 + len(days) - 1) / 601 - 1) * 100)
    assert c["iwm_pct"] == pytest.approx(((200 - len(days) + 1) / 199 - 1) * 100)
    assert c["replay_pct"] == pytest.approx(((10 + len(days) - 1) / 11 - 1) * 100)
    cls = {f["rule"]: f["cls"] for f in out["flags"]}
    assert cls == {"paper_rejected": "action", "paper_reconcile": "action", "paper_lot_tail": "action", "paper_unfilled": "sim"}
    assert "DDD" in [f for f in out["flags"] if f["rule"] == "paper_unfilled"][0]["text"]
    assert "CCC" not in [f for f in out["flags"] if f["rule"] == "paper_unfilled"][0]["text"]
    rej = [f for f in out["flags"] if f["rule"] == "paper_rejected"][0]["text"]
    assert rej.startswith("2 张单被券商拒绝") and "CCC" in rej and "EEE(提交时被拒)" in rej
    assert [o["ticker"] for o in out["orders"] if "提交时被拒" in o["tags"]] == ["EEE"]
    assert "EEE" not in [f for f in out["flags"] if f["rule"] == "paper_unfilled"][0]["text"]


def test_summary_closed_lot_without_return():
    rep = {"market": {"spy": {}, "iwm": {}, "vix": {}}, "paper": {"books": [], "orders": [], "flags": [],
           "closed": [{"book": "long", "ticker": "AAA", "ret_pct": None}, {"book": "long", "ticker": "BBB", "ret_pct": 2.0}]},
           "real": {"nav": {}, "deals": [], "round_trips": [], "flags": []}}
    assert "模拟盘平仓 2 笔:long AAA —; long BBB +2.0%" in R.summary_lines(rep)


def test_notification_leads_with_action_count():
    rep = {"paper": {"books": [{"book": "long", "day_ret": -0.5}], "flags": [
               {"rule": "paper_unfilled", "text": "x", "cls": "sim"},
               {"rule": "paper_rejected", "text": "2 张单被券商拒绝:A, B", "cls": "action"}]},
           "real": {"nav": {"pnl": -100.0}, "flags": [{"rule": "real_0dte", "text": "y", "cls": "real"},
                                                      {"rule": "real_concentration", "text": "z", "info": True}]}}
    t = R.notify_text(rep)
    assert t.startswith("需要处理 1 条:2 张单被券商拒绝")
    assert "实盘当日 -100" in t and "实盘行为 1 条" in t
    fc = R.flag_classes(rep)
    assert [len(fc[k]) for k in ("action", "real", "sim", "info")] == [1, 1, 1, 1]


def test_real_section_dates_each_deal_by_its_own_day(tmp_path, monkeypatch):
    """A late afternoon option buy, a Saturday expiry settlement and two 0DTE buys of one contract on the review day:
    days to expiry and the price context come from each deal's date, lessons carry that date, the two buys are one
    lesson. Made-up tickers and prices."""
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    for f in ("headlines", "retail", "insiders_30d"):
        monkeypatch.setattr(R, f, lambda *a, **k: {})
    days = [d.date() for d in pd.bdate_range("2026-08-03", "2026-10-12")]
    st = FakeStore()
    for i, d in enumerate(days):
        st.con.execute("INSERT INTO index_daily VALUES ('SPY', ?, ?, ?, ?)", [d, 600 + i, 600 + i, 600 + i])
        st.con.execute("INSERT INTO bars VALUES ('AAA', ?, ?, ?, ?, ?, ?), ('BBB', ?, 60, 61, 59, 60, 60)",
                       [d, 99 + i, 101 + i, 98 + i, 100 + i, 100 + i, d])
    close = {d: 100 + i for i, d in enumerate(days)}
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE acct_nav (date DATE, total_assets DOUBLE, cash DOUBLE)")
    con.execute("CREATE TABLE acct_flows (date DATE, amount_usd DOUBLE)")
    con.execute("CREATE TABLE acct_deals (deal_id VARCHAR, ts TIMESTAMP, code VARCHAR, name VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE, is_option BOOLEAN, underlying VARCHAR)")
    con.execute("CREATE TABLE acct_positions (date DATE, code VARCHAR, name VARCHAR, qty DOUBLE, cost_price DOUBLE, price DOUBLE, market_val DOUBLE, pl_val DOUBLE, is_option BOOLEAN, underlying VARCHAR)")
    con.execute("INSERT INTO acct_nav VALUES ('2026-10-09', 10000, 5000), ('2026-10-12', 9900, 5000)")
    con.execute("""INSERT INTO acct_deals VALUES
        ('L1', '2026-10-08 15:30:00', 'US.AAA261009C100000', '', 'BUY', 1, 1.20, TRUE, 'US.AAA'),
        ('R1', '2026-10-09 11:00:00', 'US.BBB261016C60000', '', 'BUY', 1, 2.00, TRUE, 'US.BBB'),
        ('S1', '2026-10-10 00:40:00', 'US.AAA261009C100000', '', 'SELL', 1, 0.0, TRUE, 'US.AAA'),
        ('B1', '2026-10-12 10:00:00', 'US.BBB261012P50000', '', 'BUY', 1, 0.50, TRUE, 'US.BBB'),
        ('B2', '2026-10-12 10:30:00', 'US.BBB261012P50000', '', 'BUY', 1, 0.40, TRUE, 'US.BBB')""")
    _write_review(tmp_path, "2026-10-08", [])                           # L1 reached acct_deals after both reviews ran
    _write_review(tmp_path, "2026-10-09", [{"ts": "2026-10-09 11:00:00", "code": "US.BBB261016C60000", "deal_id": "R1"}])
    day = D(2026, 10, 12)
    out = R.real_section(con, st, day, D(2026, 10, 9), 0.1, {}, {})
    by = {x["deal_id"]: x for x in out["deals"]}
    assert list(by) == ["L1", "S1", "B1", "B2"] and out["window"]["late"] == 1
    assert by["L1"]["day"] == "2026-10-08" and by["L1"]["dte"] == 1               # not expiry minus the review day
    assert by["L1"]["u_close"] == close[D(2026, 10, 8)]                          # that day's close, not the review day's
    assert any(t.startswith("买入时只剩 1 天") for t in by["L1"]["tags"])
    assert by["S1"]["dte"] == -1 and "到期作废(券商结算记录)" in by["S1"]["tags"] and by["S1"]["u_close"] is None
    assert by["B1"]["u_close"] == 60.0 and by["B1"]["dte"] == 0
    fl = {}
    for f in out["flags"]:
        fl.setdefault(f["rule"], []).append(f)
    assert [(f["date"], f["key"]) for f in fl["real_short_dte"]] == [("2026-10-08", "US.AAA261009C100000|BUY")]
    (z,) = fl["real_0dte"]
    assert z["date"] == "2026-10-12" and "×2" in z["text"]
    assert [f["date"] for f in fl["real_add_same_day"]] == ["2026-10-12"]
    (rt,) = out["round_trips"]
    assert rt["code"] == "US.AAA261009C100000" and rt["exit_px"] == 0 and rt["exit_day"] == "2026-10-10"
