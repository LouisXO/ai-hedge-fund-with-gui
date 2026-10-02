"""Balder's public record page -> balder_record rows; open -> closed keeps one row; scored against QQQ (no network)."""
import datetime as dt
import math

import duckdb
import pandas as pd
import pytest

from agent import balder_record as br

ALGO = """<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Opened</th><th>Closed</th><th>Entry</th><th>Exit</th><th>Return</th></tr></thead>
<tbody><tr><td><b>HD</b></td><td>Oversold bounce</td><td>09-28</td><td>10-02</td><td>290.57</td><td>285.46</td><td class="neg">-1.8%</td></tr>
<tr><td><b>ARM</b></td><td>Momentum</td><td>12-30</td><td>01-02</td><td>1,100.00</td><td>1,210.00</td><td class="pos">+10.0%</td></tr></tbody></table>"""
LONG_OPEN = """<table><thead><tr><th>Symbol</th><th>In</th><th>Out</th><th>Entry</th><th>Last / exit</th> <th>Return</th><th>Held</th></tr></thead>
<tbody><tr><td><b>SMTC</b></td><td colspan="2"><span class="pill">OPEN</span></td><td>131.56</td><td>194.88</td><td class="pos">+48.1%</td><td>—</td></tr></tbody></table>"""
LONG_CLOSED = LONG_OPEN.replace("""<td colspan="2"><span class="pill">OPEN</span></td><td>131.56</td><td>194.88</td><td class="pos">+48.1%</td><td>—</td>""",
                                """<td>09-21</td><td>10-06</td><td>131.56</td><td>190.00</td><td class="pos">+44.4%</td><td>11d</td>""")
TODAY = dt.date(2026, 10, 2)


def test_both_books_parse_and_the_year_is_the_one_on_or_before_the_fetch_day():
    rows = br.parse("<html>" + ALGO + LONG_OPEN + "</html>", TODAY).set_index("symbol")
    assert rows.loc["HD", "opened"] == dt.date(2026, 9, 28) and rows.loc["HD", "ret_pct"] == -1.8
    assert rows.loc["ARM", "opened"] == dt.date(2025, 12, 30) and rows.loc["ARM", "entry"] == 1100.0   # Dec -> last year
    assert rows.loc["SMTC", "status"] == "open" and rows.loc["SMTC", "opened_seen"] and rows.loc["SMTC", "ret_pct"] == 48.1


def test_a_missing_table_fails_loudly():
    with pytest.raises(RuntimeError, match="long"):
        br.parse(ALGO, TODAY)


def test_an_open_position_and_its_close_are_one_row_with_the_real_entry_date(tmp_path):
    con = duckdb.connect(str(tmp_path / "l.db"))
    st = br.upsert(con, br.parse(ALGO + LONG_OPEN, TODAY))
    assert st["new_open"] == ["long SMTC"] and len(st["new_closed"]) == 2
    br.upsert(con, br.parse(ALGO + LONG_OPEN, TODAY + dt.timedelta(days=1)))     # seen again: first-seen date stays
    assert con.execute("SELECT opened, first_seen FROM balder_record WHERE symbol = 'SMTC'").fetchone() == (TODAY, TODAY)
    st = br.upsert(con, br.parse(ALGO + LONG_CLOSED, dt.date(2026, 10, 7)))
    assert st["now_closed"] == ["long SMTC +44.4%"] and not st["new_closed"]
    assert con.execute("SELECT count(*), min(opened), min(closed), bool_and(NOT opened_seen) FROM balder_record WHERE symbol = 'SMTC'").fetchone() \
        == (1, dt.date(2026, 9, 21), dt.date(2026, 10, 6), True)


def test_each_trade_is_scored_against_qqq_over_its_own_window():
    days = pd.bdate_range("2026-09-21", "2026-10-05")
    bench = pd.DataFrame({"QQQ": [100 + i for i in range(len(days))], "SPY": [100.0] * len(days)}, index=days)
    df = br.parse(ALGO + LONG_OPEN, TODAY)
    sc = br.score(df, bench).set_index("symbol")
    qqq = (bench["QQQ"].asof(pd.Timestamp("2026-10-02")) / bench["QQQ"].asof(pd.Timestamp("2026-09-28")) - 1) * 100
    assert sc.loc["HD", "qqq"] == pytest.approx(qqq) and sc.loc["HD", "ex_qqq"] == pytest.approx(-1.8 - qqq)
    assert math.isnan(sc.loc["ARM", "qqq"])                                    # before the benchmark history: not scored
