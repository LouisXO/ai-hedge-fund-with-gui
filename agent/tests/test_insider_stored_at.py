"""S54: insider_tx.stored_at, the instant a Form 4 row first reached the panel. The shadow's visibility rule rests on it:
it is added to an existing table by the writers' own schema step, set at insert, and never moved by a later re-insert."""
import duckdb
import pandas as pd

from hedge_fund.features.panel import PanelStore

OLD_DDL = """CREATE TABLE insider_tx (
    accession VARCHAR, ticker VARCHAR, issuer_cik VARCHAR, filing_date DATE, trans_date DATE,
    owner_name VARCHAR, relationship VARCHAR, officer_title VARCHAR, trans_code VARCHAR,
    acq_disp VARCHAR, shares DOUBLE, price DOUBLE, value_usd DOUBLE, shares_after DOUBLE,
    source VARCHAR, fetched_at TIMESTAMP,
    PRIMARY KEY (accession, ticker, trans_date, trans_code, shares, price))"""


def _row(acc: str, source: str = "edgar_realtime", **kw) -> dict:
    return {"accession": acc, "ticker": "AAA", "issuer_cik": "1", "filing_date": pd.Timestamp("2026-10-21").date(),
            "trans_date": pd.Timestamp("2026-10-20").date(), "owner_name": "o", "relationship": "isOfficer",
            "officer_title": "CEO", "trans_code": "P", "acq_disp": "A", "shares": 100.0, "price": 10.0,
            "value_usd": 1000.0, "shares_after": None, "source": source, "fetched_at": pd.Timestamp.now(), **kw}


def test_the_column_is_added_to_an_old_table_and_old_rows_stay_null(tmp_path):
    path = tmp_path / "panel.db"
    con = duckdb.connect(str(path))
    con.execute(OLD_DDL)
    con.execute("INSERT INTO insider_tx (accession, ticker, trans_date, trans_code, shares, price, filing_date) "
                "VALUES ('old', 'AAA', '2026-10-01', 'P', 1, 1, '2026-10-02')")
    con.close()
    with PanelStore(path) as s:                                   # a writer's open runs the schema step (DDL)
        assert s.con.execute("SELECT stored_at FROM insider_tx WHERE accession = 'old'").fetchone()[0] is None
    with PanelStore(path, read_only=True) as s:                   # a reader neither needs nor adds anything
        assert "stored_at" in [r[0] for r in s.con.execute("DESCRIBE insider_tx").fetchall()]


def test_insert_sets_now_and_a_reinsert_keeps_the_first_instant(tmp_path):
    with PanelStore(tmp_path / "panel.db") as s:
        t0 = pd.Timestamp.now(tz="UTC")
        s.insert("insider_tx", pd.DataFrame([_row("a1")]))
        first = pd.Timestamp(s.con.execute("SELECT stored_at FROM insider_tx").fetchone()[0])
        assert first >= t0 - pd.Timedelta(seconds=1) and first <= pd.Timestamp.now(tz="UTC")
        # the quarterly bulk load replaces the row with its own source and parse time: the first arrival stays
        s.insert("insider_tx", pd.DataFrame([_row("a1", source="sec_form345", fetched_at=pd.Timestamp("2027-02-01"))]))
        row = s.con.execute("SELECT source, stored_at FROM insider_tx").fetchone()
        assert row[0] == "sec_form345" and pd.Timestamp(row[1]) == first
        # a row given its own instant keeps it (the tests of the shadow build visibility this way); a new row gets now
        given = pd.Timestamp("2026-10-21 16:12", tz="America/Los_Angeles")
        s.insert("insider_tx", pd.DataFrame([_row("a2", stored_at=given), _row("a3")]))
        got = dict(s.con.execute("SELECT accession, stored_at FROM insider_tx").fetchall())
        assert pd.Timestamp(got["a2"]) == given and pd.Timestamp(got["a3"]) >= first
        assert s.con.execute("SELECT count(*) FROM insider_tx").fetchone()[0] == 3


def test_tables_without_the_column_insert_as_before(tmp_path):
    with PanelStore(tmp_path / "panel.db") as s:
        df = pd.DataFrame([{"symbol": "SPY", "trade_date": pd.Timestamp("2026-10-21").date(), "open": 1.0, "high": 1.0,
                            "low": 1.0, "close": 1.0, "adj_close": 1.0, "source": "t", "fetched_at": pd.Timestamp.now()}])
        assert s.insert("index_daily", df) == 1 and s.insert("index_daily", df) == 1
        assert s.con.execute("SELECT count(*) FROM index_daily").fetchone()[0] == 1


def test_a_row_stored_before_the_column_keeps_its_old_fetched_at_when_replaced(tmp_path):
    path = tmp_path / "panel.db"
    con = duckdb.connect(str(path))
    con.execute(OLD_DDL)
    con.execute("INSERT INTO insider_tx (accession, ticker, trans_date, trans_code, shares, price, filing_date, source, fetched_at) "
                "VALUES ('old', 'AAA', '2026-10-20', 'P', 100, 10, '2026-10-21', 'edgar_realtime', '2026-10-21 16:12:00')")
    con.close()
    with PanelStore(path) as s:                                   # the bulk load replaces it after the ALTER
        s.insert("insider_tx", pd.DataFrame([_row("old", source="sec_form345", fetched_at=pd.Timestamp("2027-02-01"))]))
        src, stored, fetched = s.con.execute("SELECT source, stored_at, fetched_at FROM insider_tx").fetchone()
    assert src == "sec_form345" and pd.Timestamp(fetched) == pd.Timestamp("2027-02-01")
    assert pd.Timestamp(stored) == pd.Timestamp("2026-10-21 16:12", tz="America/Los_Angeles")   # not the load's now
