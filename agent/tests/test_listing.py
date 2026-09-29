"""Listing mask and its table: union of intervals, key migration, refresh. No network, temp DuckDB."""
import duckdb
import pandas as pd
import pytest

from agent import audit
from agent.s11_insider_wide import listed_mask
from agent.sources import av_listing
from hedge_fund.features.panel import PanelStore

OLD_DDL = """CREATE TABLE listing_status (
    symbol VARCHAR, name VARCHAR, exchange VARCHAR, asset_type VARCHAR,
    ipo_date DATE, delisting_date DATE, status VARCHAR, fetched_at TIMESTAMP,
    PRIMARY KEY (symbol, status))"""
T0, T1 = pd.Timestamp("2026-09-20 12:00"), pd.Timestamp("2026-09-27 03:00")
FILL = [(f"S{i:02d}", "Active", "2000-01-03", None) for i in range(30)]      # the refresh refuses a list that shrank by 10%
DATES = pd.DatetimeIndex(["2015-06-01", "2016-05-20", "2016-05-23", "2020-01-02", "2025-02-21", "2025-02-24",
                          "2026-09-14", "2026-09-15", "2026-09-25"])


def rows(*r, at=T1):
    """(symbol, status, ipo, delisting, [asset_type]) -> a frame shaped like av_listing.fetch's."""
    df = pd.DataFrame([x[:4] for x in r], columns=["symbol", "status", "ipo_date", "delisting_date"])
    df["name"], df["exchange"], df["fetched_at"] = df["symbol"] + " Inc", "NYSE", at
    df["asset_type"] = [x[4] if len(x) > 4 else "Stock" for x in r]
    for c in ("ipo_date", "delisting_date"):
        df[c] = pd.to_datetime(df[c]).dt.date
    return df


def pk(con):
    return con.execute("""SELECT constraint_column_names FROM duckdb_constraints()
                          WHERE table_name = 'listing_status' AND constraint_type = 'PRIMARY KEY'""").fetchone()[0]


def table(store, name="listing_status"):
    got = store.con.execute(f"SELECT symbol, status, ipo_date, delisting_date FROM {name} ORDER BY 1, 3, 2").fetchall()
    return [(s, st, str(i), str(d) if d else None) for s, st, i, d in got]


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


# -- the mask ---------------------------------------------------------------

def test_reused_ticker_is_listed_in_both_lives_and_not_between(store):
    # SNDK: the old company left in 2016, the spin-off listed in 2025 under the same ticker
    store.insert("listing_status", rows(("SNDK", "Delisted", "1995-11-08", "2016-05-20"),
                                        ("SNDK", "Active", "2025-02-24", None)))
    m = listed_mask(store, DATES, ["SNDK"])["SNDK"]
    assert m.tolist() == [True, True, False, False, False, True, True, True, True]


def test_a_delisted_row_for_a_living_company_does_not_remove_it(store):
    # OKE: the vendor lists the same company as Active and as Delisted on 2026-09-14
    store.insert("listing_status", rows(("OKE", "Active", "1985-07-01", None),
                                        ("OKE", "Delisted", "1985-07-01", "2026-09-14")))
    assert listed_mask(store, DATES, ["OKE"])["OKE"].all()


def test_a_real_delisting_ends_on_its_date(store):
    store.insert("listing_status", rows(("GONE", "Delisted", "2010-01-04", "2026-09-14")))
    m = listed_mask(store, DATES, ["GONE", "NEVER"])
    assert m.loc["2026-09-14", "GONE"] and not m.loc["2026-09-15":, "GONE"].any()
    assert not m["NEVER"].any()                                  # no row at all: never listed


def test_two_delisted_segments_of_one_symbol_are_both_kept(store):
    store.insert("listing_status", rows(("AB", "Delisted", "2014-01-02", "2016-05-20"),
                                        ("AB", "Delisted", "2019-06-03", "2025-02-21")))
    m = listed_mask(store, DATES, ["AB"])["AB"]
    assert m.tolist() == [True, True, False, True, True, False, False, False, False]


def test_the_mask_ignores_everything_that_is_not_a_stock(store):
    store.insert("listing_status", rows(("SPY", "Active", "1993-01-29", None, "ETF")))
    assert not listed_mask(store, DATES, ["SPY"])["SPY"].any()


def test_the_mask_does_not_see_the_future(store):
    # a listing that starts after day D says nothing about day D
    store.insert("listing_status", rows(("NEW", "Active", "2025-02-24", None)))
    m = listed_mask(store, DATES, ["NEW"])["NEW"]
    assert not m.loc[:"2025-02-21"].any() and m.loc["2025-02-24":].all()


# -- the key migration ------------------------------------------------------

@pytest.fixture
def old_store(tmp_path):
    """A panel.db created before the key change, as production has it."""
    path = tmp_path / "panel.db"
    con = duckdb.connect(str(path))
    con.execute(OLD_DDL)
    df = rows(("DELL", "Active", "2018-12-21", None), ("DELL", "Delisted", "1988-08-17", "2015-03-04"),
              ("NOIPO", "Delisted", None, "2019-01-02"), ("SPY", "Active", "1993-01-29", None, "ETF"))
    con.execute("INSERT INTO listing_status SELECT symbol, name, exchange, asset_type, ipo_date, delisting_date, "
                "status, fetched_at FROM df")
    con.close()
    s = PanelStore(path)
    yield s
    s.close()


def test_migration_changes_the_key_and_keeps_every_row(old_store):
    assert pk(old_store.con) == ["symbol", "status"]
    before = old_store.con.execute("SELECT * EXCLUDE (ipo_date) FROM listing_status ORDER BY symbol, status").fetchall()
    out = old_store.migrate_listing_key()
    assert out == {"migrated": True, "rows_before": 4, "rows_after": 4}
    assert pk(old_store.con) == ["symbol", "status", "ipo_date"]
    assert old_store.con.execute("SELECT * EXCLUDE (ipo_date) FROM listing_status ORDER BY symbol, status").fetchall() == before
    # a key column cannot be NULL: an unknown ipo date becomes 1900-01-01, which the mask reads the same way
    assert listed_mask(old_store, DATES, ["NOIPO"])["NOIPO"].tolist() == [True] * 3 + [False] * 6


def test_migration_is_safe_to_run_twice(old_store):
    old_store.migrate_listing_key()
    snap = old_store.con.execute("SELECT * FROM listing_status ORDER BY symbol, status").fetchall()
    assert old_store.migrate_listing_key() == {"migrated": False, "rows_before": 4, "rows_after": 4}
    assert old_store.con.execute("SELECT * FROM listing_status ORDER BY symbol, status").fetchall() == snap
    assert old_store.con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE 'listing_status_new%'").fetchone()[0] == 0


def test_a_failed_migration_leaves_the_old_table(old_store, monkeypatch):
    monkeypatch.setattr(PanelStore, "_listing_count", lambda self, name: 4 if name == "listing_status" else 3)
    with pytest.raises(RuntimeError, match="row count"):
        old_store.migrate_listing_key()
    monkeypatch.undo()
    assert pk(old_store.con) == ["symbol", "status"]
    assert old_store.con.execute("SELECT count(*) FROM listing_status").fetchone()[0] == 4


def test_a_new_database_starts_with_the_new_key(store):
    assert pk(store.con) == ["symbol", "status", "ipo_date"]
    assert store.migrate_listing_key()["migrated"] is False


# -- the refresh ------------------------------------------------------------

def test_refresh_adds_segments_instead_of_replacing_them(store):
    store.write_listing(rows(("AB", "Active", "2019-06-03", None), at=T0),
                        rows(("AB", "Delisted", "2014-01-02", "2016-05-20"), at=T0))
    # a week later the vendor's delisted list carries another old company under the same ticker
    out = store.write_listing(rows(("AB", "Active", "2019-06-03", None)),
                              rows(("AB", "Delisted", "2001-03-01", "2009-12-31")))
    assert table(store) == [("AB", "Delisted", "2001-03-01", "2009-12-31"), ("AB", "Delisted", "2014-01-02", "2016-05-20"),
                            ("AB", "Active", "2019-06-03", None)]
    assert (out["rows_before"], out["rows_after"]) == (2, 3)


def test_refresh_keeps_the_longest_of_two_rows_with_the_same_start(store):
    store.write_listing(rows(("AA", "Active", "2000-01-03", None)),
                        rows(("DUP", "Delisted", "2001-01-02", "2006-01-03"), ("DUP", "Delisted", "2001-01-02", "2005-01-03")))
    assert table(store)[1] == ("DUP", "Delisted", "2001-01-02", "2006-01-03")


def test_a_listing_that_left_the_active_list_ends_where_the_delisted_row_says(store):
    store.write_listing(rows(*FILL, ("GONE", "Active", "2010-01-04", None), ("LOST", "Active", "2012-01-03", None),
                             ("SNDK", "Active", "2025-02-24", None), at=T0),
                        rows(("SNDK", "Delisted", "1995-11-08", "2016-05-20"), at=T0))
    out = store.write_listing(rows(*FILL),
                              rows(("SNDK", "Delisted", "1995-11-08", "2016-05-20"), ("GONE", "Delisted", "2010-01-04", "2026-09-14")))
    m = listed_mask(store, DATES, ["GONE", "LOST", "SNDK"])
    assert m.loc["2026-09-14", "GONE"] and not m.loc["2026-09-15":, "GONE"].any()
    # no Delisted row for this listing: the vendor has not said when it ended, so it stays as it was
    assert m.loc["2026-09-25", "LOST"] and m.loc["2026-09-25", "SNDK"]
    assert out["active_closed"] == ["GONE"] and out["active_missing"] == ["LOST", "SNDK"]


def test_refresh_refuses_a_truncated_active_list(store):
    full = rows(*[(f"S{i:02d}", "Active", "2000-01-03", None) for i in range(20)], at=T0)
    store.write_listing(full, rows(("GONE", "Delisted", "2010-01-04", "2026-09-14"), at=T0))
    with pytest.raises(RuntimeError, match="active list"):
        store.write_listing(full.head(10).assign(fetched_at=T1), rows(("S15", "Delisted", "2000-01-03", "2026-09-21")))
    assert store.con.execute("SELECT count(*), max(fetched_at) FROM listing_status").fetchone() == (21, T0.to_pydatetime())


def test_a_closed_listing_keeps_its_start_when_the_delisted_row_starts_later(store):
    # the vendor's Delisted row for a company often starts years after its Active row did (DOC, LAB, NMIH)
    store.write_listing(rows(*FILL, ("ZZZA", "Active", "2010-01-04", None), at=T0), rows(at=T0))
    out = store.write_listing(rows(*FILL), rows(("ZZZA", "Delisted", "2020-01-02", "2026-09-14")))
    assert [r for r in table(store) if r[0] == "ZZZA"] == [("ZZZA", "Delisted", "2010-01-04", "2026-09-14"),
                                                           ("ZZZA", "Delisted", "2020-01-02", "2026-09-14")]
    m = listed_mask(store, DATES, ["ZZZA"])["ZZZA"]
    assert m.loc["2015-06-01"] and m.loc["2026-09-14"] and not m.loc["2026-09-15":].any()
    assert out["active_closed"] == ["ZZZA"] and out["active_missing"] == []


def test_an_old_delisting_does_not_end_a_listing_seen_last_week(store):
    # OKE: an old Delisted row, and one download that misses the Active row; the company is alive
    store.write_listing(rows(*FILL, ("OKE", "Active", "1985-07-01", None), at=T0),
                        rows(("OKE", "Delisted", "1985-07-01", "2019-03-01"), at=T0))
    out = store.write_listing(rows(*FILL), rows(("OKE", "Delisted", "1985-07-01", "2019-03-01")))
    assert ("OKE", "Active", "1985-07-01", None) in table(store)
    assert listed_mask(store, DATES, ["OKE"])["OKE"].all()
    assert out["active_closed"] == [] and out["active_missing"] == ["OKE"]


def test_a_new_company_on_the_ticker_does_not_end_the_old_listing(store):
    # REU was last seen on the active list on 2026-09-20; a listing that began after that is another company's
    store.write_listing(rows(*FILL, ("REU", "Active", "2010-01-04", None), at=T0), rows(at=T0))
    store.write_listing(rows(*FILL), rows(("REU", "Delisted", "2026-09-21", "2026-09-25")))
    assert ("REU", "Active", "2010-01-04", None) in table(store)          # nothing says when it ended: still open
    # its own delisting arrives a week later: that one ends it, not the later company's
    store.write_listing(rows(*FILL, at=T1 + pd.Timedelta(days=7)),
                        rows(("REU", "Delisted", "2026-09-21", "2026-09-25"), ("REU", "Delisted", "2010-01-04", "2026-09-14"),
                             at=T1 + pd.Timedelta(days=7)))
    assert [r for r in table(store) if r[0] == "REU"] == [("REU", "Delisted", "2010-01-04", "2026-09-14"),
                                                          ("REU", "Delisted", "2026-09-21", "2026-09-25")]
    assert listed_mask(store, DATES, ["REU"])["REU"].tolist() == [True] * 7 + [False, True]


def test_a_moved_ipo_date_is_reported_apart_and_keeps_the_older_start(store):
    store.write_listing(rows(*FILL, ("CRTO", "Active", "2016-01-04", None), at=T0), rows(at=T0))
    out = store.write_listing(rows(*FILL, ("CRTO", "Active", "2026-07-27", None)), rows())
    assert [r for r in table(store) if r[0] == "CRTO"] == [("CRTO", "Active", "2016-01-04", None),
                                                           ("CRTO", "Active", "2026-07-27", None)]
    assert listed_mask(store, DATES, ["CRTO"])["CRTO"].loc["2020-01-02":].all()
    assert out["ipo_changed"] == ["CRTO"] and out["active_missing"] == [] and out["active_closed"] == []


def test_lists_that_shrink_a_little_each_week_cannot_pass_one_after_another(store):
    full = [(f"S{i:03d}", "Active", "2000-01-03", None) for i in range(100)]
    store.write_listing(rows(*full, at=T0), rows(at=T0))
    store.write_listing(rows(*full[:91]), rows())                                  # 91% of the table: accepted
    with pytest.raises(RuntimeError, match="active list"):                         # 85%: >= 90% of 91, < 90% of 100
        store.write_listing(rows(*full[:85], at=T1 + pd.Timedelta(days=7)), rows(at=T1 + pd.Timedelta(days=7)))
    assert store.con.execute("SELECT count(*) FROM listing_status WHERE status = 'Active'").fetchone()[0] == 100


def test_refresh_keeps_the_table_as_it_was_before(store):
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None), at=T0), rows(("X", "Delisted", "2001-01-02", "2005-01-03"), at=T0))
    assert table(store, "listing_status_prev") == []
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None)), rows(("OKE", "Delisted", "1985-07-01", "2026-09-14")))
    assert table(store, "listing_status_prev") == [("OKE", "Active", "1985-07-01", None), ("X", "Delisted", "2001-01-02", "2005-01-03")]


def test_a_second_refresh_the_same_day_keeps_the_snapshot(store):
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None), at=T0), rows(at=T0))
    first = store.write_listing(rows(("OKE", "Active", "1985-07-01", None)), rows(("X", "Delisted", "2001-01-02", "2005-01-03")))
    again = store.write_listing(rows(("OKE", "Active", "1985-07-01", None), at=T1 + pd.Timedelta(hours=5)),
                                rows(("X", "Delisted", "2001-01-02", "2005-01-03"), at=T1 + pd.Timedelta(hours=5)))
    assert first["snapshot"] and not again["snapshot"]
    assert table(store, "listing_status_prev") == [("OKE", "Active", "1985-07-01", None)]


def test_refresh_migrates_an_old_table_first(old_store):
    old_store.write_listing(rows(("DELL", "Active", "2018-12-21", None), ("SPY", "Active", "1993-01-29", None, "ETF")),
                            rows(("DELL", "Delisted", "1984-05-03", "1987-10-19")))
    assert pk(old_store.con) == ["symbol", "status", "ipo_date"]
    assert [r for r in table(old_store) if r[0] == "DELL"] == [
        ("DELL", "Delisted", "1984-05-03", "1987-10-19"), ("DELL", "Delisted", "1988-08-17", "2015-03-04"),
        ("DELL", "Active", "2018-12-21", None)]


CSV = {"active": "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
                 "oke,Oneok Inc,NYSE,Stock,1985-07-01,null,Active\n"
                 "SNDK,Sandisk Corp,NASDAQ,Stock,2025-02-24,null,Active\n",
       "delisted": "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
                   "OKE,Oneok Inc,NYSE,Stock,1985-07-01,2026-09-14,Delisted\n"
                   "SNDK,SanDisk Corp,NASDAQ,Stock,1995-11-08,2016-05-20,Delisted\n"}


def test_loader_end_to_end_with_a_fake_vendor(store):
    asked = []

    def get(params):
        asked.append(params["state"])
        return CSV[params["state"]]

    out = av_listing.refresh(store, get=get)
    assert asked == ["active", "delisted"] and (out["active"], out["delisted"]) == (2, 2)
    m = listed_mask(store, DATES, ["OKE", "SNDK"])
    assert m["OKE"].all() and m["SNDK"].tolist() == [True, True, False, False, False, True, True, True, True]


def test_loader_writes_nothing_when_one_list_fails(store):
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None), at=T0), rows(("X", "Delisted", "2001-01-02", "2005-01-03"), at=T0))
    with pytest.raises(RuntimeError):
        av_listing.refresh(store, get=lambda p: CSV["active"] if p["state"] == "active" else "{}", wait=0.0)
    assert store.con.execute("SELECT max(fetched_at) FROM listing_status").fetchone()[0] == T0.to_pydatetime()


def test_fetch_does_not_wait_after_its_last_try(monkeypatch):
    slept = []
    monkeypatch.setattr(av_listing.time, "sleep", slept.append)
    with pytest.raises(RuntimeError):
        av_listing.fetch("active", tries=3, wait=7.0, get=lambda p: "{}")
    assert slept == [7.0, 7.0]


def test_refresh_downloads_before_it_opens_the_database(monkeypatch):
    # an open PanelStore locks panel.db for every other process; a throttled vendor can take minutes
    seen = []

    class Store:
        def __enter__(self):
            seen.append("open")
            return self

        def __exit__(self, *exc):
            seen.append("close")

        def write_listing(self, active, delisted):
            seen.append("write")
            return {}

    monkeypatch.setattr(av_listing, "PanelStore", Store)
    monkeypatch.setattr(av_listing, "fetch", lambda state, **kw: seen.append(state) or rows())
    monkeypatch.setattr("sys.argv", ["av_listing", "refresh"])
    assert av_listing.main() == 0
    assert seen == ["active", "delisted", "open", "write", "close"]


def test_migrate_closes_stale_rows_with_their_start_and_is_safe_twice(tmp_path):
    path = tmp_path / "panel.db"
    con = duckdb.connect(str(path))
    con.execute(OLD_DDL)
    df = pd.concat([rows(("ZZ", "Active", "2010-01-04", None), ("OLD", "Active", "2012-01-03", None), at=T0),
                    rows(("AA", "Active", "2000-01-03", None), ("ZZ", "Delisted", "2020-01-02", "2026-09-14"),
                         ("OLD", "Delisted", "2012-01-03", "2019-03-01"))])
    con.execute("INSERT INTO listing_status SELECT symbol, name, exchange, asset_type, ipo_date, delisting_date, "
                "status, fetched_at FROM df")
    con.close()
    with PanelStore(path) as s:
        out = av_listing.migrate(s)
        assert out["migrated"] and out["active_closed"] == ["ZZ"] and out["active_missing"] == ["OLD"]
        assert (out["rows_before"], out["rows_now"]) == (5, 5)             # ZZ: one Active row became a Delisted row
        assert ("ZZ", "Delisted", "2010-01-04", "2026-09-14") in table(s)
        snap = table(s)
        again = av_listing.migrate(s)
        assert not again["migrated"] and again["active_closed"] == [] and table(s) == snap


# -- the audit --------------------------------------------------------------

def bars(store, tickers, days=25, last="2026-09-25", px=50.0, volume=1e6):
    idx = pd.bdate_range(end=last, periods=days)
    df = pd.DataFrame([(t, d.date(), px, px, px, px, px, volume, "test", T1) for t in tickers for d in idx],
                      columns=["ticker", "trade_date", "open", "high", "low", "close", "adj_close", "volume",
                               "source", "fetched_at"])
    store.upsert_bars(df)


def ledger_with(tmp_path, tickers):
    path = str(tmp_path / "ledger.db")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE agent_lots (lot_id VARCHAR, book VARCHAR, ticker VARCHAR, status VARCHAR)")
    for i, (t, status) in enumerate(tickers):
        con.execute("INSERT INTO agent_lots VALUES (?, 'long_term', ?, ?)", [str(i), t, status])
    con.close()
    return path


def test_audit_counts_active_liquid_names_the_mask_excludes(store, tmp_path, capsys):
    bars(store, ["OKE", "SNDK", "LATE", "THIN"])
    bars(store, ["THIN"], volume=10.0)                            # overwrites: $500 a day, below the floor
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None), ("SNDK", "Active", "2025-02-24", None),
                             ("LATE", "Active", "2026-10-01", None), ("THIN", "Active", "2026-10-01", None)),
                        rows(("OKE", "Delisted", "1985-07-01", "2026-09-14"), ("SNDK", "Delisted", "1995-11-08", "2016-05-20")))
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=ledger_with(tmp_path, []))
    got = {r[1]: r for r in rep.rows}
    row = got["liquid names with an Active row that the listing mask excludes"]
    assert row[2] == "FAIL" and row[3].startswith("1 of 3") and "LATE" in row[3] and "OKE" not in row[3]


def test_audit_lists_held_names_whose_listing_state_changed(store, tmp_path, capsys):
    bars(store, ["OKE", "HELD", "SOLD", "BACK"])
    store.write_listing(rows(*FILL, ("OKE", "Active", "1985-07-01", None), ("HELD", "Active", "2010-01-04", None),
                             ("SOLD", "Active", "2010-01-04", None), at=T0),
                        rows(("BACK", "Delisted", "2001-01-02", "2005-01-03"), at=T0))
    path = ledger_with(tmp_path, [("OKE", "open"), ("HELD", "open"), ("BACK", "open"), ("SOLD", "closed")])
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=path)
    assert rep.rows[-1][2] == "PASS"                              # first refresh: nothing to compare with
    store.write_listing(rows(*FILL, ("OKE", "Active", "1985-07-01", None), ("BACK", "Active", "2026-09-01", None)),
                        rows(("OKE", "Delisted", "1985-07-01", "2026-09-14"), ("HELD", "Delisted", "2010-01-04", "2026-09-22"),
                             ("SOLD", "Delisted", "2010-01-04", "2026-09-22")))
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=path)
    check, status, detail = rep.rows[-1][1:]
    assert check == "held names whose listing state changed at the last refresh" and status == "WARN"
    assert "HELD listed -> not listed" in detail and "BACK not listed -> listed" in detail
    assert "OKE" not in detail and "SOLD" not in detail           # still listed; no longer held


def test_audit_warns_when_the_listing_list_is_stale(store, tmp_path):
    store.write_listing(rows(("OKE", "Active", "1985-07-01", None), ("KLG", "Active", "2023-10-02", None), at=T0),
                        rows(("KLG", "Delisted", "2023-10-02", "2025-09-25"), at=T0))
    bars(store, ["KLG"], last="2025-09-25")
    bars(store, ["OKE"], last="2026-09-25")
    ledger = ledger_with(tmp_path, [])
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=ledger)
    got = {r[1]: r for r in rep.rows}
    fresh = got["listing list freshness (newest Active fetch vs newest bar)"]
    assert fresh[2] == "PASS" and fresh[3].startswith("5 days")
    assert got["names still Active after their bars and a Delisted row ended (kept listed by the mask)"][3] == "1 ['KLG']"
    bars(store, ["OKE"], last="2026-10-02")                      # a week later the refresh failed
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=ledger)
    fresh = {r[1]: r for r in rep.rows}["listing list freshness (newest Active fetch vs newest bar)"]
    assert fresh[2] == "WARN" and fresh[3].startswith("12 days")


def test_audit_still_names_a_changed_holding_after_a_second_refresh_the_same_day(store, tmp_path):
    bars(store, ["HELD"])
    store.write_listing(rows(*FILL, ("HELD", "Active", "2010-01-04", None), at=T0), rows(at=T0))
    later = rows(("HELD", "Delisted", "2010-01-04", "2026-09-22"))
    store.write_listing(rows(*FILL), later)
    store.write_listing(rows(*FILL, at=T1 + pd.Timedelta(hours=5)), later.assign(fetched_at=T1 + pd.Timedelta(hours=5)))
    rep = audit.Report()
    audit.audit_listing(store, rep, ledger_db=ledger_with(tmp_path, [("HELD", "open")]))
    check, status, detail = rep.rows[-1][1:]
    assert status == "WARN" and "HELD listed -> not listed" in detail and "fetched 2026-09-20 -> 2026-09-27" in detail


def test_a_closed_listing_is_not_shortened_when_the_vendor_resends_an_old_row(store):
    # TEL: an old Delisted row with the same start as the Active row; the company really delists now under a new key
    store.write_listing(rows(*FILL, ("TELX", "Active", "1990-01-02", None), at=T0),
                        rows(("TELX", "Delisted", "1990-01-02", "2007-06-01"), at=T0))
    store.write_listing(rows(*FILL), rows(("TELX", "Delisted", "1990-01-02", "2007-06-01"),
                                          ("TELX", "Delisted", "2007-06-29", "2026-09-24")))
    assert ("TELX", "Delisted", "1990-01-02", "2026-09-24") in table(store)
    # a week later the vendor sends the old row again: the interval must keep its later end
    T2 = pd.Timestamp("2026-10-04 03:00")
    store.write_listing(rows(*FILL, at=T2), rows(("TELX", "Delisted", "1990-01-02", "2007-06-01"),
                                                 ("TELX", "Delisted", "2007-06-29", "2026-09-24"), at=T2))
    assert ("TELX", "Delisted", "1990-01-02", "2026-09-24") in table(store)
    assert listed_mask(store, DATES, ["TELX"])["TELX"].loc["2016-05-20"]
