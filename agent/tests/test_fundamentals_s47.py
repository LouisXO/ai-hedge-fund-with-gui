"""S47 data corrections: share counts, entity mapping, parent equity, year-to-date flows. Temp DuckDB, no network."""
import numpy as np
import pandas as pd
import pytest

from agent.books import fundamentals as F
from agent.sources.sec_xbrl import DDL as XBRL_DDL
from hedge_fund.features.panel import PanelStore

CFO, NI, EQ = "NetCashProvidedByUsedInOperatingActivities", "NetIncomeLoss", "StockholdersEquity"
EQ_NCI = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
DEI, BS, WD = "EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"


def facts(rows):
    df = pd.DataFrame(rows, columns=["cik", "tag", "period_start", "period_end", "is_instant", "val", "form", "filed"])
    for c in ("period_start", "period_end", "filed"):
        df[c] = pd.to_datetime(df[c]).dt.date
    return df


def flow(cik, tag, start, end, val, form, filed):
    return (cik, tag, start, end, False, val, form, filed)


def instant(cik, tag, end, val, form, filed):
    return (cik, tag, end, end, True, val, form, filed)


def year_to_date(cik, tag, year, vals, first_filed=None):
    """A calendar fiscal year as the cash flow statement reports it: 3, 6, 9 and 12 months from January 1."""
    ends = [f"{year}-03-31", f"{year}-06-30", f"{year}-09-30", f"{year}-12-31"]
    filed = first_filed or [f"{year}-05-01", f"{year}-08-01", f"{year}-11-01", f"{year + 1}-02-20"]
    forms = ["10-Q", "10-Q", "10-Q", "10-K"]
    return [flow(cik, tag, f"{year}-01-01", e, v, fm, fd) for e, v, fm, fd in zip(ends, vals, forms, filed)]


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    for stmt in XBRL_DDL:
        s.con.execute(stmt)
    yield s
    s.close()


def load(store, rows, seen):
    """rows: fact tuples; seen: (ticker, cik, quarter, n_filings) as issuer_seen has them."""
    df = facts(rows)
    df["ns"] = np.where(df["tag"] == DEI, "dei", "us-gaap")
    df["unit"] = np.where(df["tag"].isin([DEI, BS, WD]), "shares", "USD")
    df["accn"] = df["cik"].astype(str) + "-" + df["filed"].astype(str)
    store.insert("xbrl_facts", df)
    s = pd.DataFrame(seen, columns=["ticker", "cik", "quarter", "n_filings"])
    s["cik"] = s["cik"].map("{:010d}".format)
    store.insert("issuer_seen", s)
    bars = pd.DataFrame({"ticker": s["ticker"].unique(), "trade_date": pd.Timestamp("2026-09-25").date(), "open": 10.0,
                         "high": 10.0, "low": 10.0, "close": 10.0, "adj_close": 10.0, "volume": 1e6, "source": "test",
                         "fetched_at": pd.Timestamp.now()})
    store.upsert_bars(bars)


def row(df, ticker, filed):
    return df[(df["ticker"] == ticker) & (df["filed"] == pd.Timestamp(filed))].iloc[0]


# -- flows -------------------------------------------------------------------

def test_year_to_date_facts_are_differenced_into_quarters():
    f = facts(year_to_date(1, CFO, 2024, [10, 30, 60, 100]))
    q = F.quarterly_flows(f, "cfo", [CFO]).set_index("period_end")
    assert q["cfo"].tolist() == [10, 20, 30, 40]                    # Q1, 6M - Q1, 9M - 6M, FY - 9M
    assert str(q.loc[pd.Timestamp("2024-06-30").date(), "filed"]) == "2024-08-01"
    assert str(q.loc[pd.Timestamp("2024-12-31").date(), "filed"]) == "2025-02-20"   # Q4 is known at the 10-K


def test_a_reported_quarter_is_preferred_to_a_derived_one():
    # the income statement gives both the three months and the six months: the reported quarter stands
    f = facts([flow(1, NI, "2024-01-01", "2024-03-31", 10, "10-Q", "2024-05-01"),
               flow(1, NI, "2024-04-01", "2024-06-30", 21, "10-Q", "2024-08-01"),
               flow(1, NI, "2024-01-01", "2024-06-30", 30, "10-Q", "2024-08-01")])
    assert F.quarterly_flows(f, "ni", [NI])["ni"].tolist() == [10, 21]


def test_four_quarters_add_up_to_the_year_as_reported():
    # the quarter's own fact (+37) disagrees with nine months less six months (-37): Q4 absorbs it
    f = facts(year_to_date(1, NI, 2024, [142, 248, 211, 83])
              + [flow(1, NI, "2024-07-01", "2024-09-30", 37, "10-Q", "2024-11-01")])
    q = F.quarterly_flows(f, "ni", [NI])
    assert q["ni"].tolist() == [142, 106, 37, 83 - 142 - 106 - 37]
    t = F.ttm_flows(f, "ni", [NI]).set_index("period_end")["ni_ttm"]
    assert t[pd.Timestamp("2024-12-31")] == 83


def test_q4_falls_back_to_annual_less_nine_months():
    # no first quarter on file (the company listed in the spring): only FY - 9M gives the fourth
    f = facts(year_to_date(1, CFO, 2024, [10, 30, 60, 100])[1:])
    q = F.quarterly_flows(f, "cfo", [CFO]).set_index("period_end")["cfo"]
    assert q.to_dict() == {pd.Timestamp("2024-09-30").date(): 30, pd.Timestamp("2024-12-31").date(): 40}


def test_a_derived_quarter_waits_for_both_of_its_filings():
    # the 6-month fact is public in August, the Q1 fact it is differenced against only in September
    f = facts(year_to_date(1, CFO, 2024, [10, 30, 60, 100], ["2024-09-15", "2024-08-01", "2024-11-01", "2025-02-20"]))
    q = F.quarterly_flows(f, "cfo", [CFO]).set_index("period_end")
    assert str(q.loc[pd.Timestamp("2024-06-30").date(), "filed"]) == "2024-09-15"


def test_ttm_is_the_fiscal_year_for_a_year_to_date_item():
    f = facts(year_to_date(1, CFO, 2023, [34, 63, 89, 110]) + year_to_date(1, CFO, 2024, [40, 70, 95, 118]))
    t = F.ttm_flows(f, "cfo", [CFO]).set_index("period_end")["cfo_ttm"]
    assert t[pd.Timestamp("2023-12-31")] == 110                     # was: the sum of four different years' Q1
    assert t[pd.Timestamp("2024-03-31")] == 110 - 34 + 40
    assert t[pd.Timestamp("2024-12-31")] == 118


def test_ttm_needs_four_quarters_that_cover_one_year():
    # one 3-month fact per year, which is all the old code kept of a cash flow statement
    rows = [flow(1, CFO, f"{y}-01-01", f"{y}-03-31", 10, "10-Q", f"{y}-05-01") for y in (2021, 2022, 2023, 2024)]
    assert F.ttm_flows(facts(rows), "cfo", [CFO]).empty
    # a missing quarter: Q2 2024 was never filed, so no window of four is a year until Q2 2025 is in
    rows = year_to_date(1, NI, 2023, [10, 20, 30, 40]) + [
        flow(1, NI, "2024-01-01", "2024-03-31", 11, "10-Q", "2024-05-01"),
        flow(1, NI, "2024-07-01", "2024-09-30", 13, "10-Q", "2024-11-01")]
    t = F.ttm_flows(facts(rows), "ni", [NI])
    assert pd.Timestamp("2024-09-30") not in set(t["period_end"])
    assert pd.Timestamp("2024-03-31") in set(t["period_end"])


def test_sixteen_week_quarter_is_a_quarter():
    # KR: 16 + 12 + 12 + 12 weeks, reported year-to-date
    ends = [("2025-05-24", "10-Q", "2025-06-27"), ("2025-08-16", "10-Q", "2025-09-19"), ("2025-11-08", "10-Q", "2025-12-12"),
            ("2026-01-31", "10-K", "2026-03-31")]
    f = facts([flow(1, CFO, "2025-02-02", e, v, fm, fd) for (e, fm, fd), v in zip(ends, [16, 28, 40, 52])])
    assert F.quarterly_flows(f, "cfo", [CFO])["cfo"].tolist() == [16, 12, 12, 12]
    assert F.ttm_flows(f, "cfo", [CFO])["cfo_ttm"].tolist() == [52]


def test_annual_filer_takes_the_latest_year_as_ttm():
    rows = [flow(1, NI, f"{y}-01-01", f"{y}-12-31", v, "20-F", f"{y + 1}-04-10")
            for y, v in ((2022, 117.4), (2023, 12.6), (2024, -7.9))]
    t = F.ttm_flows(facts(rows), "ni", [NI]).set_index("period_end")
    assert t.loc[pd.Timestamp("2022-12-31"), "ni_ttm"] == 117.4      # known with the first report, not the fourth
    assert t.loc[pd.Timestamp("2024-12-31"), "ni_ttm"] == -7.9        # not (117.4 + 12.6 - 7.9 + ...) / 4
    assert t.loc[pd.Timestamp("2024-12-31"), "filed"] == pd.Timestamp("2025-04-10")


def test_annual_value_is_not_the_ttm_when_the_year_has_quarters():
    rows = [flow(1, NI, "2024-01-01", "2024-03-31", 10, "10-Q", "2024-05-01"),
            flow(1, NI, "2024-01-01", "2024-12-31", 100, "10-K", "2025-02-20")]
    assert F.ttm_flows(facts(rows), "ni", [NI]).empty


# -- build: shares -----------------------------------------------------------

def quarters_of_weighted_shares(cik, year, q, fy):
    ends = [(f"{year}-01-01", f"{year}-03-31", f"{year}-05-01"), (f"{year}-04-01", f"{year}-06-30", f"{year}-08-01"),
            (f"{year}-07-01", f"{year}-09-30", f"{year}-11-01")]
    return [flow(cik, WD, s, e, q, "10-Q", fd) for s, e, fd in ends] + [
        flow(cik, WD, f"{year}-01-01", f"{year}-12-31", fy, "10-K", f"{year + 1}-02-20")]


def test_weighted_shares_are_taken_as_reported(store):
    # 10-K filer without instant share tags (dual class): the 10-K row was annual - 3 quarters = negative
    rows = quarters_of_weighted_shares(1, 2024, 2.6e9, 2.6e9) + [instant(1, "Assets", "2024-12-31", 5e9, "10-K", "2025-02-20")]
    load(store, rows, [("DUAL", 1, "2025q1", 5)])
    assert row(F.build(store), "DUAL", "2025-02-20")["shares"] == 2.6e9


def test_foreign_filer_with_only_a_weighted_count_has_no_shares(store):
    rows = [flow(1, WD, "2024-01-01", "2024-12-31", 3.4e8, "20-F", "2025-04-28"),
            instant(1, "Assets", "2024-12-31", 5e9, "20-F", "2025-04-28")]
    load(store, rows, [("ADR", 1, "2025q2", 5)])
    r = row(F.build(store), "ADR", "2025-04-28")
    assert pd.isna(r["shares"]) and r["shares_flag"] == "ads"       # was 0.25 x 3.4e8
    assert r["shares_w"] == 3.4e8                                     # as reported, for the review list


def test_share_counts_that_disagree_are_dropped_and_listed(store, tmp_path):
    rows = [instant(1, DEI, "2026-07-31", 18.9e6, "10-Q", "2026-08-07"),                 # from before a merger
            flow(1, WD, "2026-04-01", "2026-06-30", 222.4e6, "10-Q", "2026-08-07"),
            instant(2, DEI, "2026-07-31", 100e6, "10-Q", "2026-08-07"),                  # buyback: 1.2x is a real difference
            flow(2, WD, "2026-04-01", "2026-06-30", 120e6, "10-Q", "2026-08-07"),
            instant(3, DEI, "2026-07-31", 65.9e6, "10-Q", "2026-08-07"),                 # one class on the cover page
            instant(3, BS, "2026-06-30", 101e6, "10-Q", "2026-08-07")]
    load(store, rows, [("MERG", 1, "2026q3", 5), ("FINE", 2, "2026q3", 5), ("CLASS", 3, "2026q3", 5)])
    store.con.execute("CREATE TABLE shares_override AS SELECT 'CLASS' AS ticker, 99e6 AS shares")
    df = F.factor_inputs(store).set_index("ticker")
    assert pd.isna(df.loc["MERG", "shares"]) and df.loc["MERG", "shares_flag"] == "mismatch"
    assert df.loc["FINE", "shares"] == 100e6 and df.loc["FINE", "shares_flag"] == ""
    assert pd.isna(df.loc["CLASS", "shares"])                        # the override is applied by data.fundamentals
    assert F.review_path(store) == tmp_path / "shares_review.csv"
    rev = pd.read_csv(F.review_path(store)).set_index("ticker")
    assert sorted(rev.index) == ["CLASS", "MERG"]
    assert rev.loc["MERG", "shares_dei"] == 18.9e6 and rev.loc["MERG", "shares_w"] == 222.4e6
    assert rev.loc["CLASS", "override"] == 99e6 and pd.isna(rev.loc["MERG", "override"])
    assert store.con.execute("SELECT count(*) FROM fundamentals_pit WHERE shares_flag = 'mismatch'").fetchone()[0] == 2


def test_old_facts_are_not_carried_into_a_new_filing(store):
    rows = [instant(1, DEI, "2019-12-31", 100.08e6, "20-F", "2020-04-20"),              # the only share fact ever
            flow(1, NI, "2022-01-01", "2022-12-31", 14.7e6, "20-F", "2023-04-20"),      # the tag was dropped after 2022
            instant(1, "Assets", "2022-12-31", 2e9, "20-F", "2023-04-20"),
            instant(1, "Assets", "2025-12-31", 3e9, "20-F", "2026-04-29")]
    load(store, rows, [("OLD", 1, "2026q2", 5)])
    df = F.build(store)
    r = row(df, "OLD", "2026-04-29")
    assert pd.isna(r["shares"]) and r["shares_flag"] == "stale" and pd.isna(r["ni_ttm"])
    assert r["assets"] == 3e9
    assert row(df, "OLD", "2020-04-20")["shares"] == 100.08e6        # fresh when it was filed
    assert row(df, "OLD", "2023-04-20")["ni_ttm"] == 14.7e6


def test_newer_of_the_two_instant_counts_is_used(store):
    rows = [instant(1, DEI, "2025-02-10", 100e6, "10-K", "2025-02-20"),                  # cover page only in the 10-K
            instant(1, BS, "2025-06-30", 110e6, "10-Q", "2025-08-01")]
    load(store, rows, [("A", 1, "2025q1", 5)])
    r = row(F.build(store), "A", "2025-08-01")
    assert r["shares"] == 110e6 and r["shares_asof"] == pd.Timestamp("2025-06-30")


# -- build: equity, entity mapping, visibility -------------------------------

def test_equity_is_the_parents_only(store):
    rows = [instant(1, EQ_NCI, "2026-03-31", 10.7e9, "10-Q", "2026-05-07"), instant(1, "Assets", "2026-03-31", 19e9, "10-Q", "2026-05-07"),
            instant(2, EQ_NCI, "2026-03-31", 5e9, "10-Q", "2026-05-07"), instant(2, EQ, "2026-03-31", 4e9, "10-Q", "2026-05-07")]
    load(store, rows, [("NCI", 1, "2026q2", 5), ("BOTH", 2, "2026q2", 5)])
    df = F.build(store).set_index("ticker")
    assert pd.isna(df.loc["NCI", "equity"]) and df.loc["BOTH", "equity"] == 4e9


def test_symbol_stays_with_its_last_owner_when_the_quarter_has_no_record(store):
    # issuer_seen ends at 2026q2; both companies file in 2026q3. CIK 2 was seen under the symbol once, in 2016.
    rows = [instant(1, DEI, "2026-08-05", 112.6e6, "10-Q", "2026-08-06"), instant(1, "Assets", "2026-06-30", 3.3e9, "10-Q", "2026-08-06"),
            instant(2, DEI, "2026-08-11", 14.9e6, "10-Q", "2026-08-11"), instant(2, "Assets", "2026-06-30", 0.3e9, "10-Q", "2026-08-11"),
            instant(2, "Assets", "2016-06-30", 0.2e9, "10-Q", "2016-08-11")]
    load(store, rows, [("GS", 1, "2016q2", 3), ("GS", 1, "2026q2", 4), ("GS", 2, "2016q3", 1)])
    df = F.build(store)
    latest = df[df["ticker"] == "GS"].sort_values("filed").iloc[-1]
    assert latest["cik"] == 1 and latest["shares"] == 112.6e6
    assert set(df[df["cik"] == 2]["filed"]) == {pd.Timestamp("2016-08-11")}    # the quarter it did own the symbol


def test_row_holds_only_what_was_filed_by_its_date(store):
    rows = (year_to_date(1, CFO, 2023, [34, 63, 89, 110]) + year_to_date(1, CFO, 2024, [40, 70, 95, 118])
            + [flow(1, CFO, "2023-01-01", "2023-12-31", 999, "10-K", "2025-02-20"),      # restated a year later
               instant(1, EQ, "2024-09-30", 50.0, "10-Q", "2024-11-01"), instant(1, EQ, "2024-12-31", 60.0, "10-K", "2025-02-20")])
    load(store, rows, [("A", 1, "2024q1", 5)])
    df = F.build(store)
    assert row(df, "A", "2024-02-20")["cfo_ttm"] == 110                # as first filed
    assert row(df, "A", "2024-11-01")["cfo_ttm"] == 110 - 89 + 95
    assert row(df, "A", "2024-11-01")["equity"] == 50.0                # the December balance sheet is not public yet
    for r in df.itertuples():
        assert r.period_end <= r.filed


def test_late_filing_for_an_older_period_replaces_nothing(store):
    rows = [instant(1, "Assets", "2025-06-30", 200.0, "10-Q", "2025-08-01"),
            instant(1, "Assets", "2024-06-30", 100.0, "10-Q/A", "2025-09-01")]           # first seen in an amendment
    load(store, rows, [("A", 1, "2025q3", 5)])
    assert row(F.build(store), "A", "2025-09-01")["assets"] == 200.0
