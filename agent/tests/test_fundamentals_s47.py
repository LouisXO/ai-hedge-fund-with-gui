"""S47 data corrections: share counts, entity mapping, parent equity, year-to-date flows. Temp DuckDB, no network."""
import duckdb
import numpy as np
import pandas as pd
import pytest

from agent.books import fundamentals as F
from agent.books.data import fundamentals as read_fundamentals
from agent.books.factors import latest_before
from agent.sources.sec_xbrl import DDL as XBRL_DDL
from hedge_fund.features.panel import PanelStore

CFO, NI, EQ = "NetCashProvidedByUsedInOperatingActivities", "NetIncomeLoss", "StockholdersEquity"
EQ_NCI = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
MI, RNCI, PL = "MinorityInterest", "RedeemableNoncontrollingInterestEquityCarryingAmount", "ProfitLoss"
REV, RFC = "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"
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


def test_facts_filed_later_do_not_change_what_was_known():
    known = (year_to_date(1, CFO, 2023, [34, 63, 89, 110]) + year_to_date(1, CFO, 2024, [40, 70, 95, 118])
             + [flow(2, NI, "2023-01-01", "2023-12-31", 50, "10-K", "2024-03-01")])       # first report of a new filer
    later = [flow(1, CFO, "2024-02-01", "2024-04-30", 7, "10-Q/A", "2025-06-01"),          # a period that cuts across two quarters
             flow(1, CFO, "2024-04-01", "2024-06-30", 99, "10-Q", "2025-08-01"),           # the quarter on its own, a year on
             flow(2, NI, "2023-07-01", "2023-09-30", 12, "10-Q", "2024-11-01")]            # last year's quarter as a comparative
    for name, tag in (("cfo", CFO), ("ni", NI)):
        was = F.ttm_flows(facts(known), name, [tag])
        now = F.ttm_flows(facts(known + later), name, [tag])
        now = now[now["filed"] <= pd.Timestamp("2025-02-20")]
        pd.testing.assert_frame_equal(was.reset_index(drop=True), now.reset_index(drop=True))


def test_period_that_ends_after_its_filing_is_ignored(store):
    rows = [instant(1, DEI, "2033-09-12", 5.2e6, "10-Q", "2023-09-13"),                  # a typing error on the cover page
            instant(1, DEI, "2026-07-31", 5.4e6, "10-Q", "2026-08-05")]
    load(store, rows, [("TYPO", 1, "2026q3", 5)])
    df = F.build(store)
    assert set(df["filed"]) == {pd.Timestamp("2026-08-05")} and df["shares"].tolist() == [5.4e6]


def test_first_10k_is_the_ttm():
    # listed mid-year: one 10-Q, then the first 10-K; last year's quarters were never filed (S47 补充 6)
    rows = [flow(1, NI, "2024-01-01", "2024-03-31", 10, "10-Q", "2024-05-01"),
            flow(1, NI, "2024-01-01", "2024-12-31", 100, "10-K", "2025-02-20")]
    t = F.ttm_flows(facts(rows), "ni", [NI])
    assert t[["period_end", "filed", "ni_ttm"]].values.tolist() == [[pd.Timestamp("2024-12-31"), pd.Timestamp("2025-02-20"), 100]]


def test_broken_chain_takes_the_year_plus_this_year_to_date_less_last_years():
    # VSNT: a new 10-Q filer. Fiscal 2025 from the 10-K; 2025's quarters exist only as comparatives of 2026.
    rows = [flow(1, NI, "2025-01-01", "2025-12-31", 930, "10-K", "2026-02-20"),
            flow(1, NI, "2026-01-01", "2026-03-31", 200, "10-Q", "2026-05-01"),
            flow(1, NI, "2025-01-01", "2025-03-31", 250, "10-Q", "2026-05-01"),
            flow(1, NI, "2026-01-01", "2026-06-30", 497, "10-Q", "2026-08-01"),
            flow(1, NI, "2025-01-01", "2025-06-30", 669, "10-Q", "2026-08-01")]
    t = F.ttm_flows(facts(rows), "ni", [NI]).set_index("period_end")
    assert t.loc[pd.Timestamp("2026-03-31"), "ni_ttm"] == 930 + 200 - 250
    assert t.loc[pd.Timestamp("2026-06-30"), "ni_ttm"] == 930 + 497 - 669            # was: 930, two quarters behind
    assert t.loc[pd.Timestamp("2026-06-30"), "filed"] == pd.Timestamp("2026-08-01")
    # not a year to date: a quarter that starts mid-year is not bridged
    assert F.ttm_flows(facts(rows[:1] + [flow(1, NI, "2026-04-01", "2026-06-30", 297, "10-Q", "2026-08-01"),
                                         flow(1, NI, "2025-04-01", "2025-06-30", 419, "10-Q", "2026-08-01")]),
                       "ni", [NI])["period_end"].tolist() == [pd.Timestamp("2025-12-31")]


def test_quarters_that_span_more_than_a_year_are_not_a_ttm():
    # four chained 16-week quarters are 448 days: not a year
    ends = ["2025-04-23", "2025-08-13", "2025-12-03", "2026-03-25"]
    starts = ["2025-01-01", "2025-04-24", "2025-08-14", "2025-12-04"]
    rows = [flow(1, NI, s, e, 10, "10-Q", "2026-05-01") for s, e in zip(starts, ends)]
    assert F.quarterly_flows(facts(rows), "ni", [NI])["ni"].tolist() == [10, 10, 10, 10]
    assert F.ttm_flows(facts(rows), "ni", [NI]).empty


def test_a_10k_quarter_that_equals_the_year_is_the_year():
    # TSCO: the 10-K tags the fourth quarter's dates on the year's $1,096M
    rows = [flow(1, NI, "2024-12-29", "2025-03-29", 180, "10-Q", "2025-04-24"),
            flow(1, NI, "2025-03-30", "2025-06-28", 303, "10-Q", "2025-07-24"),
            flow(1, NI, "2025-06-29", "2025-09-27", 264, "10-Q", "2025-10-23"),
            flow(1, NI, "2025-09-28", "2025-12-27", 1096, "10-K", "2026-02-19"),
            flow(1, NI, "2024-12-29", "2025-12-27", 1096, "10-K", "2026-02-19")]
    q = F.quarterly_flows(facts(rows), "ni", [NI])
    assert q["ni"].tolist() == [180, 303, 264, 1096 - 180 - 303 - 264]
    t = F.ttm_flows(facts(rows), "ni", [NI]).set_index("period_end")["ni_ttm"]
    assert t[pd.Timestamp("2025-12-27")] == 1096                                      # was 1096 + 180 + 303 + 264
    # in a 10-Q the same coincidence is a real quarter
    rows[3] = flow(1, NI, "2025-09-28", "2025-12-27", 1096, "10-Q", "2026-02-19")
    assert F.quarterly_flows(facts(rows), "ni", [NI])["ni"].tolist()[-1] == 1096
    # AMSC: the year itself is first filed a year later, as a comparative; it cannot reach back
    known = rows[:3] + [flow(1, NI, "2025-09-28", "2025-12-27", 1096, "10-K", "2026-02-19")]
    later = [flow(1, NI, "2024-12-29", "2025-12-27", 1096, "10-K", "2027-02-18")]
    was = F.ttm_flows(facts(known), "ni", [NI])
    now = F.ttm_flows(facts(known + later), "ni", [NI])
    pd.testing.assert_frame_equal(was, now[now["filed"] <= pd.Timestamp("2026-12-31")].reset_index(drop=True))


def test_zero_in_the_preferred_revenue_tag_does_not_hide_the_other():
    # FLS: Revenues = 0 next to RevenueFromContract... = 4,729 in the same 10-K
    rows = [flow(1, REV, "2025-01-01", "2025-12-31", 0, "10-K", "2026-02-20"),
            flow(1, RFC, "2025-01-01", "2025-12-31", 4729, "10-K", "2026-02-20")]
    assert F.ttm_flows(facts(rows), "rev", [REV, RFC])["rev_ttm"].tolist() == [4729]
    assert F.ttm_flows(facts(rows[:1]), "rev", [REV, RFC])["rev_ttm"].tolist() == [0]    # a zero alone stands


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


def test_counts_of_an_earlier_period_are_not_compared(store):
    # NVDA's 10:1 split: the 10-K balance sheet is pre-split, the next 10-Q is post-split on both counts
    rows = [instant(1, BS, "2024-01-28", 2.464e9, "10-K", "2024-02-21"),
            instant(1, DEI, "2024-08-23", 24.53e9, "10-Q", "2024-08-28"),
            flow(1, WD, "2024-04-29", "2024-07-28", 24.85e9, "10-Q", "2024-08-28")]
    load(store, rows, [("SPLIT", 1, "2024q3", 5)])
    r = row(F.build(store), "SPLIT", "2024-08-28")
    assert r["shares"] == 24.53e9 and r["shares_flag"] == ""                  # was: missing, "mismatch"


def test_a_single_count_of_the_period_is_used_and_listed_when_an_older_one_disagrees(store):
    # MCHB: the cover-page count is from before the merger, 330 days old; the 10-Q's weighted count is current
    rows = [instant(1, DEI, "2025-09-15", 18.9e6, "10-K", "2025-09-20"),
            flow(1, WD, "2026-04-01", "2026-06-30", 222.45e6, "10-Q", "2026-08-11"),
            instant(1, "Assets", "2026-06-30", 4e9, "10-Q", "2026-08-11")]
    load(store, rows, [("MCHB", 1, "2026q3", 5)])
    df = F.factor_inputs(store)
    r = row(df, "MCHB", "2026-08-11")
    assert r["shares"] == 222.45e6 and r["shares_flag"] == "unconfirmed"
    assert pd.read_csv(F.review_path(store))["ticker"].tolist() == ["MCHB"]


def test_two_counts_that_agree_outvote_the_third(store):
    rows = [instant(1, DEI, "2026-07-31", 65.9e6, "10-Q", "2026-08-07"),               # HG: one class on the cover page
            instant(1, BS, "2026-06-30", 98.6e6, "10-Q", "2026-08-07"),
            flow(1, WD, "2026-04-01", "2026-06-30", 101.1e6, "10-Q", "2026-08-07"),
            instant(2, DEI, "2026-07-31", 100e6, "10-Q", "2026-08-07"),                # cover page and weighted agree
            instant(2, BS, "2026-06-30", 300e6, "10-Q", "2026-08-07"),
            flow(2, WD, "2026-04-01", "2026-06-30", 102e6, "10-Q", "2026-08-07"),
            instant(3, DEI, "2026-07-31", 10e6, "10-Q", "2026-08-07"),                 # none agree
            instant(3, BS, "2026-06-30", 30e6, "10-Q", "2026-08-07"),
            flow(3, WD, "2026-04-01", "2026-06-30", 90e6, "10-Q", "2026-08-07")]
    load(store, rows, [("HG", 1, "2026q3", 5), ("TWO", 2, "2026q3", 5), ("NONE", 3, "2026q3", 5)])
    df = F.build(store).set_index("ticker")
    assert df.loc["HG", "shares"] == 98.6e6 and df.loc["HG", "shares_flag"] == ""
    assert df.loc["TWO", "shares"] == 100e6 and df.loc["TWO", "shares_flag"] == ""
    assert pd.isna(df.loc["NONE", "shares"]) and df.loc["NONE", "shares_flag"] == "mismatch"


def test_a_count_of_one_is_not_a_source(store):
    rows = [instant(1, DEI, "2026-07-31", 1, "10-Q", "2026-08-07"),                    # XBRL noise on the cover page
            instant(1, BS, "2026-06-30", 50e6, "10-Q", "2026-08-07"),
            flow(1, WD, "2026-04-01", "2026-06-30", 0, "10-Q", "2026-08-07")]
    load(store, rows, [("ONE", 1, "2026q3", 5)])
    r = row(F.build(store), "ONE", "2026-08-07")
    assert r["shares"] == 50e6 and r["shares_flag"] == "" and pd.isna(r["shares_dei"])


def test_foreign_filer_with_an_instant_count_is_scored(store):
    # S47 补充 7: the ADS ratio is taken as 1 (a known limitation); only weighted-only filers are left out
    rows = [instant(1, DEI, "2026-03-31", 1.86e9, "20-F", "2026-04-28"),
            flow(1, WD, "2025-01-01", "2025-12-31", 1.9e9, "20-F", "2026-04-28")]
    load(store, rows, [("ADR", 1, "2026q2", 5)])
    r = row(F.build(store), "ADR", "2026-04-28")
    assert r["shares"] == 1.86e9 and r["shares_flag"] == ""


# -- build: equity, entity mapping, visibility -------------------------------

def test_equity_is_the_parents(store):
    d, f = "2026-03-31", "2026-05-07"
    rows = [instant(1, EQ_NCI, d, 10.7e9, "10-Q", f), instant(1, MI, d, 7.0e9, "10-Q", f), instant(1, RNCI, d, 0.5e9, "10-Q", f),
            instant(2, EQ_NCI, d, 5e9, "10-Q", f), instant(2, EQ, d, 4e9, "10-Q", f),                    # the parent's is reported
            instant(3, EQ_NCI, d, 6e9, "10-Q", f),                                                         # no NCI tag: 0
            instant(4, EQ, "2015-06-30", 58e9, "10-Q", "2015-08-01"), instant(4, EQ_NCI, d, 100e9, "10-Q", f),
            instant(4, MI, d, 2e9, "10-Q", f),                                                            # UNH: parent tag stale
            instant(5, EQ_NCI, d, 9e9, "10-Q", f), instant(5, MI, "2025-06-30", 1e9, "10-K", "2025-08-01")]  # NCI of another period
    load(store, rows, [(t, c, "2026q2", 5) for t, c in (("XIFR", 1), ("BOTH", 2), ("PG", 3), ("UNH", 4), ("OLDNCI", 5))])
    df = F.build(store)
    latest = df.sort_values("filed").drop_duplicates("ticker", keep="last").set_index("ticker")["equity"]
    # XIFR: 10.7B - 7.0B; its 0.5B redeemable NCI is mezzanine equity, outside the total, and is not subtracted
    assert latest.to_dict() == {"XIFR": 3.7e9, "BOTH": 4e9, "PG": 6e9, "UNH": 98e9, "OLDNCI": 9e9}
    assert row(df, "UNH", "2015-08-01")["equity"] == 58e9
    assert not set(F.HELPERS) & set(df.columns)


def test_net_income_falls_back_to_a_fresh_tag_only(store):
    rows = [flow(1, NI, "2022-01-01", "2022-12-31", 14.7e6, "20-F", "2023-04-20"),     # EDRY: NetIncomeLoss dropped after 2022
            flow(1, PL, "2022-01-01", "2022-12-31", 15.0e6, "20-F", "2023-04-20"),
            flow(1, PL, "2025-01-01", "2025-12-31", -3.1e6, "20-F", "2026-04-20"),
            flow(2, NI, "2025-01-01", "2025-12-31", 50e6, "10-K", "2026-02-20"),         # both fresh: NetIncomeLoss
            flow(2, PL, "2025-01-01", "2025-12-31", 55e6, "10-K", "2026-02-20")]
    load(store, rows, [("EDRY", 1, "2026q2", 5), ("BOTH", 2, "2026q1", 5)])
    df = F.build(store)
    assert row(df, "EDRY", "2023-04-20")["ni_ttm"] == 14.7e6
    assert row(df, "EDRY", "2026-04-20")["ni_ttm"] == -3.1e6                           # not blocked by the 2022 value
    assert row(df, "BOTH", "2026-02-20")["ni_ttm"] == 50e6
    assert not [c for c in df.columns if c.startswith("ni_ttm") and c != "ni_ttm"]


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


# -- audit -------------------------------------------------------------------

def test_known_fiscal_year_check_catches_the_old_cash_flow():
    from agent.audit import known_flow_failures
    f = pd.DataFrame({"ticker": ["AAPL", "AAPL"], "filed": pd.to_datetime(["2023-08-04", "2023-11-03"]), "cfo_ttm": [113.072e9, 110.543e9]})
    assert known_flow_failures(f) == []
    assert len(known_flow_failures(f.assign(cfo_ttm=150.25e9))) == 1          # four years' first quarters
    assert len(known_flow_failures(f.assign(cfo_ttm=np.nan))) == 1
    assert len(known_flow_failures(f.iloc[:1])) == 1                           # the 10-K row is missing


def test_top_list_check_names_capped_book_to_market_and_old_share_facts():
    from agent.audit import top_list_suspects
    day = pd.Timestamp("2026-09-25")
    names = [f"T{i:03d}" for i in range(200)]
    fs = pd.DataFrame({"composite": np.linspace(2, -2, 200), "n_families": 4, "mcap": 1e9}, index=names)
    fs.loc["T001", "mcap"] = 1e8                                               # a share count ten times too small
    fs.loc["T150", "mcap"] = 0.5e8                                             # the same far down the list: not reported
    fs.loc["T002", "n_families"] = 2                                           # not scored, so not in the list
    f = pd.DataFrame({"equity": 5e8, "assets": 2e9, "shares_asof": pd.Timestamp("2026-06-30")}, index=names)
    f.loc["T003", "shares_asof"] = pd.Timestamp("2025-06-30")                  # fresh at the filing, 452 days old today
    f.loc["T004", "shares_asof"] = pd.NaT
    at_cap, old, cap = top_list_suspects(fs, f, day)
    assert at_cap == ["T001"] and old == ["T003"]
    assert 0.5 < cap <= 5.0


def test_latest_symbol_is_the_one_with_most_filings(store):
    # SLNH (15 Form 4s) and its preferred SLNHP (5) in 2026q2; the 2026q3 10-Q has no issuer_seen row yet
    rows = [instant(1, DEI, "2026-08-10", 244.6e6, "10-Q", "2026-08-13"), instant(1, "Assets", "2026-06-30", 1e9, "10-Q", "2026-08-13")]
    load(store, rows, [("SLNH", 1, "2026q2", 15), ("SLNHP", 1, "2026q2", 5)])
    df = F.build(store)
    assert df["ticker"].tolist() == ["SLNH"]                                        # was SLNHP, the last alphabetically


def test_on_a_tie_the_previous_owner_keeps_the_symbol(store):
    # BH 2022q2: the owner (CIK 9) and a stray CIK (1) with one filing each
    rows = [instant(9, "Assets", "2022-06-30", 2e9, "10-Q", "2022-08-05"), instant(1, "Assets", "2022-03-31", 1e8, "10-Q", "2022-05-10")]
    load(store, rows, [("BH", 9, "2022q1", 3), ("BH", 9, "2022q2", 1), ("BH", 1, "2022q2", 1)])
    df = F.build(store)
    assert df[["ticker", "cik"]].values.tolist() == [["BH", 9]]


def test_symbol_owner_is_carried_for_at_most_four_quarters(store):
    # NIO: CIK 1 held the symbol in 2015q1; CIK 2 files under it from 2019 with no Form 4 until 2026q1
    rows = [instant(2, "Assets", "2019-03-31", 5e9, "20-F", "2019-04-02"), instant(2, "Assets", "2025-12-31", 9e9, "20-F", "2026-04-08"),
            instant(1, "Assets", "2015-03-31", 1e8, "10-Q", "2015-05-10"), instant(1, "Assets", "2015-12-31", 1e8, "10-K", "2016-03-10")]
    load(store, rows, [("NIO", 1, "2015q1", 2), ("NIO", 2, "2026q1", 4)])
    df = F.build(store)
    assert set(df[df["cik"] == 2]["filed"]) == {pd.Timestamp("2019-04-02"), pd.Timestamp("2026-04-08")}   # was: 2026 only
    assert set(df[df["cik"] == 1]["filed"]) == {pd.Timestamp("2015-05-10"), pd.Timestamp("2016-03-10")}   # within 4 quarters: kept


def test_dates_outside_1990_to_the_filing_do_not_stop_the_build(store):
    load(store, [instant(1, "Assets", "2024-06-30", 2e9, "10-Q", "2024-08-01")], [("A", 1, "2024q3", 5)])
    for end in ("3024-06-30", "0202-06-30"):                  # beyond datetime64[ns]: the whole build used to fail
        store.con.execute(f"INSERT INTO xbrl_facts (cik, ns, tag, unit, period_start, period_end, is_instant, val, accn, form, filed) "
                          f"VALUES (1, 'us-gaap', 'Assets', 'USD', DATE '{end}', DATE '{end}', TRUE, 1e9, 'x{end}', '10-Q', DATE '2024-08-01')")
    assert F.build(store)["assets"].tolist() == [2e9]


# -- factor_inputs: replace, guard, read path -----------------------------------

def two_names():
    return [instant(1, DEI, "2026-07-31", 50e6, "10-Q", "2026-08-07"), instant(1, EQ, "2026-06-30", 1e9, "10-Q", "2026-08-07"),
            instant(1, "Assets", "2026-06-30", 3e9, "10-Q", "2026-08-07"),
            instant(2, DEI, "2026-07-31", 80e6, "10-Q", "2026-08-07"), instant(2, EQ, "2026-06-30", 2e9, "10-Q", "2026-08-07"),
            instant(2, "Assets", "2026-06-30", 5e9, "10-Q", "2026-08-07")]


def test_factor_inputs_refuses_a_table_with_much_less_coverage(store):
    load(store, two_names(), [("A", 1, "2026q3", 5), ("B", 2, "2026q3", 5)])
    F.factor_inputs(store)
    store.con.execute(f"DELETE FROM xbrl_facts WHERE cik = 2 AND tag IN ('{DEI}', '{EQ}')")     # a partial refresh
    with pytest.raises(RuntimeError, match="not replaced"):
        F.factor_inputs(store)
    n = store.con.execute("SELECT count(*) FROM fundamentals_pit WHERE shares IS NOT NULL AND equity IS NOT NULL").fetchone()[0]
    assert n == 2                                                                         # the old table stands
    F.factor_inputs(store, force=True)
    assert store.con.execute("SELECT count(shares), count(equity) FROM fundamentals_pit").fetchone() == (1, 1)


class _FailingCreate:
    """A connection whose table replacement fails (disk full, killed process)."""
    def __init__(self, con):
        self._con = con

    def __getattr__(self, k):
        return getattr(self._con, k)

    def execute(self, sql, *a):
        if sql.startswith("CREATE OR REPLACE TABLE fundamentals_pit"):
            raise duckdb.IOException("disk full")
        return self._con.execute(sql, *a)


def test_a_failed_replacement_keeps_the_old_table_and_review_list(store, monkeypatch):
    load(store, two_names(), [("A", 1, "2026q3", 5), ("B", 2, "2026q3", 5)])
    F.factor_inputs(store)
    before = store.con.execute("SELECT * FROM fundamentals_pit").df()
    F.review_path(store).write_text("was\n")
    store.con.execute(f"INSERT INTO xbrl_facts (cik, ns, tag, unit, period_start, period_end, is_instant, val, accn, form, filed) "
                      f"VALUES (3, 'us-gaap', '{BS}', 'shares', DATE '2026-06-30', DATE '2026-06-30', TRUE, 9e6, 'x', '10-Q', DATE '2026-08-07')")
    monkeypatch.setattr(store, "con", _FailingCreate(store.con))
    with pytest.raises(duckdb.IOException):
        F.factor_inputs(store)
    pd.testing.assert_frame_equal(store.con.execute("SELECT * FROM fundamentals_pit").df(), before)
    assert F.review_path(store).read_text() == "was\n"                                   # written only after the table


def test_rebuild_is_deterministic_and_the_books_can_read_the_table(store):
    rows = two_names() + year_to_date(1, CFO, 2025, [10, 30, 60, 100]) + [
        flow(3, WD, "2025-01-01", "2025-12-31", 3.4e8, "20-F", "2026-04-28"), instant(3, "Assets", "2025-12-31", 5e9, "20-F", "2026-04-28")]
    load(store, rows, [("A", 1, "2026q3", 5), ("B", 2, "2026q3", 5), ("ADR", 3, "2026q2", 5)])
    first = F.factor_inputs(store)
    pd.testing.assert_frame_equal(first, F.factor_inputs(store))
    fund = read_fundamentals(store)
    assert {"shares_flag", "shares_asof"} <= set(fund.columns)
    f = latest_before(fund, pd.Timestamp("2026-09-25"))
    f = f.where(f["equity"] > 0.05 * f["assets"])                                        # what factor_scores does
    assert f.loc["A", "shares"] == 50e6 and pd.isna(f.loc["ADR", "shares"])


def test_override_fills_only_missing_recent_counts(store):
    fund = pd.DataFrame({"ticker": ["V", "V", "V", "X"], "shares": [np.nan, np.nan, 2.0e9, np.nan],
                         "filed": pd.to_datetime(["2018-01-30", "2026-07-28", "2026-08-01", "2026-08-01"])})
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM fund")
    store.con.execute("CREATE TABLE shares_override AS SELECT 'V' AS ticker, 1.88e9 AS shares, DATE '2026-09-22' AS as_of")
    got = read_fundamentals(store)["shares"].tolist()
    assert np.isnan(got[0]) and got[1:3] == [1.88e9, 2.0e9] and np.isnan(got[3])      # 2018 stays, XBRL's count stays
