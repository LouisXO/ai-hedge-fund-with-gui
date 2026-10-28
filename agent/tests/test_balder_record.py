"""Balder's public record page -> balder_record rows; open -> closed keeps one row; scored against QQQ; his prices checked
against our bars (made-up tickers and prices, a temporary DuckDB, no network)."""
import datetime as dt
import json
import math

import duckdb
import pandas as pd
import pytest

from agent import balder_record as br
from hedge_fund.features.panel import PanelStore

ALGO = """<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Opened</th><th>Closed</th><th>Entry</th><th>Exit</th><th>Return</th></tr></thead>
<tbody><tr><td><b>ABC</b></td><td>Strategy A</td><td>03-02</td><td>03-06</td><td>20.00</td><td>19.50</td><td class="neg">-2.5%</td></tr>
<tr><td><b>DEF</b></td><td>Strategy B</td><td>12-30</td><td>01-02</td><td>1,100.00</td><td>1,210.00</td><td class="pos">+10.0%</td></tr></tbody></table>"""
LONG_OPEN = """<table><thead><tr><th>Symbol</th><th>In</th><th>Out</th><th>Entry</th><th>Last / exit</th> <th>Return</th><th>Held</th></tr></thead>
<tbody><tr><td><b>GHI</b></td><td colspan="2"><span class="pill">OPEN</span></td><td>10.00</td><td>13.50</td><td class="pos">+35.0%</td><td>—</td></tr></tbody></table>"""
LONG_CLOSED = LONG_OPEN.replace("""<td colspan="2"><span class="pill">OPEN</span></td><td>10.00</td><td>13.50</td><td class="pos">+35.0%</td><td>—</td>""",
                                """<td>03-09</td><td>03-20</td><td>10.00</td><td>12.50</td><td class="pos">+25.0%</td><td>9d</td>""")
TODAY = dt.date(2026, 3, 12)           # GHI is open on the page
LATER = dt.date(2026, 3, 23)           # GHI closed on 03-20


def test_both_books_parse_and_the_year_is_the_one_on_or_before_the_fetch_day():
    rows = br.parse("<html>" + ALGO + LONG_OPEN + "</html>", TODAY).set_index("symbol")
    assert rows.loc["ABC", "opened"] == dt.date(2026, 3, 2) and rows.loc["ABC", "ret_pct"] == -2.5
    assert rows.loc["DEF", "opened"] == dt.date(2025, 12, 30) and rows.loc["DEF", "entry"] == 1100.0   # Dec -> last year
    assert rows.loc["GHI", "status"] == "open" and rows.loc["GHI", "opened_seen"] and rows.loc["GHI", "ret_pct"] == 35.0


def test_a_missing_table_fails_loudly():
    with pytest.raises(RuntimeError, match="long"):
        br.parse(ALGO, TODAY)


def test_an_open_position_and_its_close_are_one_row_with_the_real_entry_date(tmp_path):
    con = duckdb.connect(str(tmp_path / "l.db"))
    st = br.upsert(con, br.parse(ALGO + LONG_OPEN, TODAY))
    assert st["new_open"] == ["long GHI"] and len(st["new_closed"]) == 2
    br.upsert(con, br.parse(ALGO + LONG_OPEN, TODAY + dt.timedelta(days=1)))     # seen again: first-seen date stays
    assert con.execute("SELECT opened, first_seen FROM balder_record WHERE symbol = 'GHI'").fetchone() == (TODAY, TODAY)
    st = br.upsert(con, br.parse(ALGO + LONG_CLOSED, LATER))
    assert st["now_closed"] == ["long GHI +25.0%"] and not st["new_closed"]
    assert con.execute("SELECT count(*), min(opened), min(closed), bool_and(NOT opened_seen) FROM balder_record WHERE symbol = 'GHI'").fetchone() \
        == (1, dt.date(2026, 3, 9), dt.date(2026, 3, 20), True)


def test_each_trade_is_scored_against_qqq_over_its_own_window():
    days = pd.bdate_range("2026-02-23", "2026-03-16")
    bench = pd.DataFrame({"QQQ": [100 + i for i in range(len(days))], "SPY": [100.0] * len(days)}, index=days)
    df = br.parse(ALGO + LONG_OPEN, TODAY)
    sc = br.score(df, bench).set_index("symbol")
    qqq = (bench["QQQ"].asof(pd.Timestamp("2026-03-06")) / bench["QQQ"].asof(pd.Timestamp("2026-03-02")) - 1) * 100
    assert sc.loc["ABC", "qqq"] == pytest.approx(qqq) and sc.loc["ABC", "ex_qqq"] == pytest.approx(-2.5 - qqq)
    assert math.isnan(sc.loc["DEF", "qqq"])                                    # before the benchmark history: not scored
    st = br.stats(sc[sc["book"] == "algo"])                                    # ABC scored, DEF not: one excess return, no t
    assert (st["n"], st["mean_ret"], st["mean_ex_qqq"], st["t"], st["beat_qqq"]) == (2, 3.75, round(-2.5 - qqq, 2), None, 0.0)


def test_trades_before_the_benchmark_history_score_none_not_nan(tmp_path, monkeypatch, capsys):
    start = pd.Timestamp(dt.date.today() + dt.timedelta(days=1))              # every trade predates the QQQ history
    bench = pd.DataFrame({"QQQ": [100.0, 101.0], "SPY": [100.0, 100.0]}, index=pd.bdate_range(start, periods=2))
    sc = br.score(br.parse(ALGO + LONG_CLOSED, LATER), bench)
    st = br.stats(sc[sc["book"] == "algo"])
    assert st == {"n": 2, "mean_ret": 3.75, "mean_qqq": None, "mean_ex_qqq": None, "t": None, "beat_qqq": None, "sum_ret": 7.5}
    json.dumps(st, allow_nan=False)
    monkeypatch.setattr(br, "benchmarks", lambda: bench)
    monkeypatch.setattr(br, "record_bars", lambda df: pd.DataFrame(columns=["ticker", "trade_date", "low", "high"]))
    monkeypatch.setattr(br.ledger, "connect", lambda *a, **k: duckdb.connect(str(tmp_path / "l.db")))
    page, out = tmp_path / "page.html", tmp_path / "record.json"
    page.write_text("<html>" + ALGO + LONG_CLOSED + "</html>")
    assert br.main(["--html", str(page), "--out", str(out)]) == 0
    j = json.loads(out.read_text(), parse_constant=lambda c: pytest.fail(f"{c} in the json"))   # NaN / Infinity: not JSON
    assert j["algo"]["mean_ex_qqq"] is None and j["long"]["t"] is None and j["by_strategy"]["Strategy A"]["beat_qqq"] is None
    json.dumps(j, allow_nan=False)
    assert "vs QQQ n/a" in capsys.readouterr().out


def _bars(rows):
    """Fake daily bars: (ticker, day, low, high)."""
    return pd.DataFrame([{"ticker": t, "trade_date": pd.Timestamp(d), "low": lo, "high": hi} for t, d, lo, hi in rows])


def test_each_entry_and_exit_price_is_checked_against_our_bar_that_day():
    df = br.parse(ALGO + LONG_CLOSED, LATER)                              # ABC 03-02 -> 03-06, DEF (no bars), GHI 03-09 -> 03-20
    bars = _bars([("ABC", "2026-03-02", 19.5, 20.5), ("ABC", "2026-03-06", 9.0, 10.0),   # exit 19.50: twice our bar (a split?)
                  ("GHI", "2026-03-09", 9.8, 10.2), ("GHI", "2026-03-19", 12.0, 13.0), ("XYZ", "2026-03-06", 1.0, 2.0)])
    pc = br.price_check(df, bars)
    assert pc == {"n_checked": 3, "n_outside": 1, "n_no_bar_day": 1, "no_bars": ["DEF"],      # GHI has no bar on 03-20
                  "outside": [{"book": "algo", "symbol": "ABC", "leg": "exit", "day": "2026-03-06", "price": 19.5,
                               "low": 9.0, "high": 10.0}]}
    json.dumps(pc, allow_nan=False)
    assert br.price_check(df, bars.assign(low=bars["low"] * 0.5, high=bars["high"] * 2))["n_outside"] == 0   # inside the band


def test_the_json_keeps_the_newest_prices_outside(monkeypatch):
    monkeypatch.setattr(br, "N_OUTSIDE", 1)
    df = br.parse(ALGO + LONG_CLOSED, LATER)
    pc = br.price_check(df, _bars([("ABC", "2026-03-02", 1.0, 2.0), ("ABC", "2026-03-06", 1.0, 2.0), ("GHI", "2026-03-09", 1.0, 2.0)]))
    assert pc["n_outside"] == 3 and [(x["symbol"], x["day"]) for x in pc["outside"]] == [("GHI", "2026-03-09")]


def test_a_first_seen_entry_day_is_not_checked():
    df = br.parse(ALGO + LONG_OPEN, TODAY)                                 # GHI open: `opened` is the day we first saw it
    pc = br.price_check(df, _bars([("GHI", TODAY, 1.0, 2.0), ("ABC", "2026-03-02", 19.5, 20.5)]))
    assert (pc["n_checked"], pc["n_outside"], pc["n_no_bar_day"], pc["no_bars"]) == (1, 0, 1, ["DEF"])   # ABC 03-06: no bar
    assert br.price_check(df.iloc[0:0], _bars([("ABC", "2026-03-02", 1.0, 2.0)])) == {
        "n_checked": 0, "n_outside": 0, "outside": [], "n_no_bar_day": 0, "no_bars": []}


def _panel(path, days, bars):
    with PanelStore(path) as s:
        s.upsert_bars(pd.DataFrame([{"ticker": t, "trade_date": d, "open": lo, "high": hi, "low": lo, "close": hi, "adj_close": hi,
                                     "volume": 1.0, "source": "t", "fetched_at": pd.Timestamp("2026-03-23")} for t, d, lo, hi in bars]))
        s.upsert_index(pd.DataFrame([{"symbol": k, "trade_date": d.date(), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                      "adj_close": 100.0 + i, "source": "t", "fetched_at": pd.Timestamp("2026-03-23")}
                                     for k in ("QQQ", "SPY") for i, d in enumerate(days)]))


def test_the_bars_are_read_in_one_read_only_query_and_land_in_the_json(tmp_path, monkeypatch):
    today = dt.date.today()
    d = {md: br.to_date(md, today) for md in ("03-02", "03-06", "03-09", "03-20", "12-30")}   # DEF opened 12-30: the earliest
    days = pd.bdate_range(min(d.values()) - dt.timedelta(days=10), today)
    _panel(tmp_path / "p.db", days, [("ABC", d["03-02"], 19.5, 20.5), ("ABC", d["03-06"], 9.0, 10.0), ("GHI", d["03-09"], 9.8, 10.2),
                                     ("GHI", d["03-20"], 12.0, 13.0), ("ABC", min(d.values()) - dt.timedelta(days=5), 1.0, 2.0),
                                     ("XYZ", d["03-06"], 1.0, 2.0)])
    opened = []

    def panel(read_only=False):
        opened.append(read_only)
        return PanelStore(tmp_path / "p.db", read_only=read_only)

    monkeypatch.setattr(br, "PanelStore", panel)
    monkeypatch.setattr(br.ledger, "connect", lambda *a, **k: duckdb.connect(str(tmp_path / "l.db")))
    page, out = tmp_path / "page.html", tmp_path / "record.json"
    page.write_text("<html>" + ALGO + LONG_CLOSED + "</html>")
    got = br.record_bars(br.parse(ALGO + LONG_CLOSED, today))
    assert sorted(got["ticker"]) == ["ABC", "ABC", "GHI", "GHI"]                # only the record's symbols, only its dates
    assert br.main(["--html", str(page), "--out", str(out)]) == 0
    pc = json.load(open(out))["price_check"]
    assert opened and all(opened)                                              # benchmarks() and the bars: read-only
    assert (pc["n_checked"], pc["n_outside"], pc["no_bars"]) == (4, 1, ["DEF"]) and pc["outside"][0]["symbol"] == "ABC"
    monkeypatch.setattr(br, "record_bars", lambda df: (_ for _ in ()).throw(RuntimeError("panel locked")))
    assert br.main(["--html", str(page), "--out", str(out)]) == 0               # the record json is written anyway
    j = json.load(open(out))
    assert j["price_check"] == {"error": "RuntimeError: panel locked"} and j["algo"]["n"] == 2
