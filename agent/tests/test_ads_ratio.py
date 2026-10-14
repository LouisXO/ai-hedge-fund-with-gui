"""S53a: ADS ratios from 20-F cover pages, and share counts in listed units. Temp DuckDB, no network; made-up values."""
import pandas as pd
import pytest

from agent.books import fundamentals as F
from agent.sources import ads_ratio as A
from agent.tests.test_fundamentals_s47 import BS, DEI, WD, flow, instant, load, row, store  # noqa: F401  (fixture)


# -- phrases -----------------------------------------------------------------

@pytest.mark.parametrize("text, ratio", [
    ("American Depositary Shares, each representing eight Ordinary Shares", 8),
    ("American depositary shares (one American depositary share representing five Class A ordinary shares, par", 5),
    ("American depositary shares (each representing one ordinary share, par value US$0.00125 per share)", 1),
    ("American Depositary Shares, each representing 2 ordinary shares, par value US$0.01 per share", 2),
    ("American depositary shares, each representing one-half of one ordinary share", 0.5),
    ("American Depositary Shares, three of which represent two Class A ordinary shares", 2 / 3),
    ("American depositary shares, every three ADSs representing two Class A ordinary shares", 2 / 3),
    ("American Depositary Shares, each representing one-tenth of one ordinary share", 0.1),
    ('American Depositary Shares ("ADSs"), each ADS representing ten (10) ordinary shares', 10),
    ("ADSs, each representing fifteen Class A ordinary shares", 15),
    ("American Depositary Shares, each representing 0.125 ordinary share", 0.125),
    ("American Depositary Shares, each representing 0.1 of a common share", 0.1),
    ("Ten ADSs represent one ordinary share", 0.1),
    ("Each American Depositary Share representing ten shares", 10),
    ("Each ADS represents the right to receive five common shares", 5),
])
def test_ratio_phrases(text, ratio):
    got = A.ratios_in(text)
    assert got and got[0][0] == pytest.approx(ratio, rel=1e-4)


@pytest.mark.parametrize("words, value", [("twenty-five", 25), ("one hundred", 100), ("eight (8)", 8), ("1,000", 1000),
                                          ("two-thirds of one", 2 / 3), ("1/4", 0.25), ("nonsense", None)])
def test_numbers(words, value):
    assert A.number(words) == (pytest.approx(value) if value is not None else None)


def cover(table: str, footnote: str = "", ixbrl: bool = True) -> str:
    """A 20-F cover page as EDGAR serves it: hidden inline-XBRL header first (which mentions ADSs too)."""
    hdr = ('<ix:header><ix:hidden><ix:nonNumeric name="dei:Security12bTitle">American Depositary Shares, each '
           'representing 99 ordinary shares</ix:nonNumeric></ix:hidden></ix:header>') if ixbrl else ""
    return (f"<html><body><div style='display:none'>{hdr}</div><p>FORM 20-F</p>"
            "<p>Securities registered or to be registered pursuant to Section 12(b) of the Act.</p>"
            f"<table><tr><td>Title of each class</td><td>Name of each exchange on which registered</td></tr>{table}</table>"
            "<p>Securities registered or to be registered pursuant to Section 12(g) of the Act. None</p>"
            "<p>Indicate by check mark if the registrant is a well-known seasoned issuer. Yes No</p>"
            f"<p>{footnote}</p><p>TABLE OF CONTENTS</p></body></html>")


def test_cover_with_a_ratio_in_the_12b_table():
    got = A.parse(cover("<tr><td>American depositary shares, each representing four Class A ordinary shares</td>"
                        "<td>NYSE</td></tr><tr><td>Class A ordinary shares*</td><td>NYSE</td></tr>"))
    assert got["status"] == "parsed_12b" and got["ratio"] == 4          # not the 99 of the hidden header


def test_cover_without_a_depositary_share_is_ratio_1():
    got = A.parse(cover("<tr><td>Common Shares, par value $0.01</td><td>Nasdaq</td></tr>"))
    assert got["status"] == "no_ads" and got["ratio"] == 1


def test_footnote_names_an_ads_without_a_ratio():
    # TSM's cover: the 12(b) table lists common shares, a footnote below says they are listed for the ADSs
    got = A.parse(cover("<tr><td>Common Shares, par value NT$10.00 each*</td><td>NYSE</td></tr>",
                        "* Not for trading, but only in connection with the listing of American Depositary Shares"))
    assert got["status"] == "ads_no_ratio" and got["ratio"] is None
    doc = cover("<tr><td>Common Shares*</td><td>NYSE</td></tr>") + "<p>Item 9. Each ADS represents five common shares.</p>"
    assert A.parse_doc(doc)["ratio"] == 5


def test_an_ads_named_only_after_the_cover_does_not_count():
    # direct listing; the table of contents has "Item 12.D American Depositary Shares", the definitions an old program
    page = cover("<tr><td>Ordinary Shares, par value NIS 0.01</td><td>Nasdaq</td></tr>").replace(
        "<p>TABLE OF CONTENTS</p>", "<p>TABLE OF CONTENTS</p><p>Item 12.D American Depositary Shares</p>"
        "<p>ADS refers to our American depositary shares, each of which represented 360 ordinary shares</p>")
    got = A.parse(page)
    assert got["status"] == "no_ads" and got["ratio"] == 1


def test_footnote_with_a_period_before_the_ads():
    got = A.parse(cover("<tr><td>Common Shares, par value NT$10.00 each*</td><td>The New York Stock Exchange, Inc.</td></tr>",
                        "* Not for trading, but only in connection with the listing on the New York Stock Exchange, Inc. "
                        "of American Depositary Shares (\"ADS\") representing such Common Shares."))
    assert got["status"] == "ads_no_ratio"                       # an ADS company, not a direct listing


def test_ratios_that_disagree_are_left_for_review():
    got = A.parse(cover("<tr><td>American depositary shares, each representing two ordinary shares</td></tr>"
                        "<tr><td>American depositary shares, each representing ten ordinary shares</td></tr>"))
    assert got["status"] == "ambiguous" and got["ratio"] is None


def test_page_without_a_cover():
    assert A.parse("<html><body>Exhibit 99.1 press release</body></html>")["status"] == "no_cover"


# -- listed units in the fundamentals build ------------------------------------

def ratios(store, rows):
    """rows: (cik, filed, ratio, status); accn as load() spells it."""
    store.con.execute(A.DDL)
    df = pd.DataFrame(rows, columns=["cik", "filed", "ratio", "status"])
    df["accn"] = df["cik"].astype(str) + "-" + pd.to_datetime(df["filed"]).dt.date.astype(str)
    df["filed"] = pd.to_datetime(df["filed"]).dt.date
    for c, v in (("form", "20-F"), ("cover_text", ""), ("doc", ""), ("source", "test")):
        df[c] = v
    df["fetched_at"] = pd.Timestamp("2026-10-14")
    store.insert("ads_ratio", df[["cik", "accn", "form", "filed", "ratio", "status", "cover_text", "doc", "source", "fetched_at"]])


def test_ads_counts_are_divided_by_the_filings_ratio(store):
    rows = [instant(1, DEI, "2026-03-31", 8.0e9, "20-F", "2026-05-20"),
            flow(1, WD, "2025-04-01", "2026-03-31", 8.2e9, "20-F", "2026-05-20")]
    load(store, rows, [("ADR", 1, "2026q2", 5)])
    ratios(store, [(1, "2026-05-20", 8.0, "parsed_12b")])
    r = row(F.build(store), "ADR", "2026-05-20")
    assert r["shares"] == pytest.approx(1.0e9) and r["shares_flag"] == ""


def test_a_fraction_of_a_share_per_ads_multiplies(store):
    # 8 ADSs to a share: 75M ordinary shares are 600M ADSs (was taken as 75M: market cap 8x too small)
    rows = [instant(1, DEI, "2021-02-28", 7.5e7, "20-F", "2021-03-15")]
    load(store, rows, [("ADR", 1, "2021q1", 5)])
    ratios(store, [(1, "2021-03-15", 0.125, "parsed_12b")])
    assert row(F.build(store), "ADR", "2021-03-15")["shares"] == pytest.approx(6.0e8)


def test_a_ratio_change_keeps_consecutive_filings_in_line(store):
    # 0.125 -> 1 when the ordinary shares were split 8:1: in listed units the two counts agree
    rows = [instant(1, DEI, "2021-02-28", 7.5e7, "20-F", "2021-03-15"),
            instant(1, DEI, "2022-03-31", 6.1e8, "20-F", "2022-04-27")]
    load(store, rows, [("ADR", 1, "2021q1", 5), ("ADR", 1, "2022q2", 5)])
    ratios(store, [(1, "2021-03-15", 0.125, "parsed_12b"), (1, "2022-04-27", 1.0, "parsed_12b")])
    df = F.build(store)
    assert row(df, "ADR", "2021-03-15")["shares"] == pytest.approx(6.0e8)
    assert row(df, "ADR", "2022-04-27")["shares"] == pytest.approx(6.1e8)


def test_weighted_only_count_is_usable_once_converted(store):
    rows = [flow(1, WD, "2024-01-01", "2024-12-31", 3.4e8, "20-F", "2025-04-28"),
            instant(1, "Assets", "2024-12-31", 5e9, "20-F", "2025-04-28")]
    load(store, rows, [("ADR", 1, "2025q2", 5)])
    ratios(store, [(1, "2025-04-28", 2.0, "parsed_12b")])
    r = row(F.build(store), "ADR", "2025-04-28")
    assert r["shares"] == pytest.approx(1.7e8) and r["shares_flag"] == ""    # was missing, flag "ads"


def test_unknown_ratio_of_a_depositary_share_company_gives_no_count(store):
    rows = [instant(1, DEI, "2025-03-31", 4.0e9, "20-F", "2025-04-20"),
            instant(1, DEI, "2026-03-31", 4.0e9, "20-F", "2026-04-20"),
            instant(1, "Assets", "2025-12-31", 5e9, "20-F", "2026-04-20")]
    load(store, rows, [("ADR", 1, "2025q2", 5), ("ADR", 1, "2026q2", 5)])
    ratios(store, [(1, "2025-04-20", 5.0, "parsed_doc"), (1, "2026-04-20", None, "ads_no_ratio")])
    df = F.build(store)
    assert row(df, "ADR", "2025-04-20")["shares"] == pytest.approx(8.0e8)
    r = row(df, "ADR", "2026-04-20")
    assert pd.isna(r["shares"]) and r["shares_flag"] == "ads_unknown"     # not 2025's count carried over


def test_direct_listing_and_domestic_filings_are_unchanged(store):
    rows = [instant(1, DEI, "2026-03-31", 1.2e8, "20-F", "2026-04-10"),       # common shares listed directly
            instant(2, DEI, "2026-06-30", 3.0e8, "10-Q", "2026-08-01")]       # a 10-Q filer that once filed 20-Fs
    load(store, rows, [("SHIP", 1, "2026q2", 5), ("DOM", 2, "2026q3", 5)])
    ratios(store, [(1, "2026-04-10", 1.0, "no_ads"), (2, "2019-04-01", 4.0, "parsed_12b")])
    df = F.build(store)
    assert row(df, "SHIP", "2026-04-10")["shares"] == 1.2e8
    assert row(df, "DOM", "2026-08-01")["shares"] == 3.0e8


def test_no_ratio_table_leaves_the_build_as_it_was(store):
    rows = [instant(1, DEI, "2026-03-31", 1.86e9, "20-F", "2026-04-28")]
    load(store, rows, [("ADR", 1, "2026q2", 5)])
    assert row(F.build(store), "ADR", "2026-04-28")["shares"] == 1.86e9


def test_a_period_row_converts_an_ads_issuers_10k_and_10q(store, tmp_path, monkeypatch):
    # an ADS issuer that files 10-K/10-Q (1 ADS = 10 shares): the cover parse never sees it, the hand table does
    rows = [instant(1, DEI, "2026-07-31", 1.12e9, "10-Q", "2026-08-06"),
            instant(1, DEI, "2021-02-28", 9.0e7, "10-K", "2021-03-01")]
    load(store, rows, [("ADS10K", 1, "2021q1", 5), ("ADS10K", 1, "2026q3", 5)])
    yml = tmp_path / "ads_ratio.yaml"
    yml.write_text("- {cik: 1, from: 2022-05-10, to: 2026-08-06, ratio: 10, forms: '10-K,10-Q', source: x, checked: 2026-10-14}\n")
    monkeypatch.setattr(A, "MANUAL", yml)
    monkeypatch.setattr(A, "CACHE", tmp_path / "cache.jsonl")
    (tmp_path / "cache.jsonl").write_text('{"cik": 9, "accn": "9-x", "form": "20-F", "filed": "2026-01-01", "status": "no_ads", '
                                          '"ratio": 1.0, "cover_text": "", "doc": "", "fetched_at": "2026-10-14"}\n')
    store.con.execute(A.DDL)
    t = A.table(store)
    t = t[t["cik"] == 1].assign(filed=lambda d: pd.to_datetime(d["filed"]).dt.date, fetched_at=pd.Timestamp("2026-10-14"))
    store.insert("ads_ratio", t[["cik", "accn", "form", "filed", "ratio", "status", "cover_text", "doc", "source", "fetched_at"]])
    df = F.build(store)
    assert row(df, "ADS10K", "2026-08-06")["shares"] == pytest.approx(1.12e8)
    assert row(df, "ADS10K", "2021-03-01")["shares"] == 9.0e7              # before the period: as reported


def test_a_hand_checked_direct_listing_is_not_an_ads_company(store):
    rows = [instant(1, DEI, "2025-03-31", 5.0e8, "40-F", "2025-04-20"),
            instant(1, DEI, "2026-03-31", 5.0e8, "40-F", "2026-04-20")]
    load(store, rows, [("CAN", 1, "2025q2", 5), ("CAN", 1, "2026q2", 5)])
    ratios(store, [(1, "2025-04-20", 1.0, "manual_direct")])          # the other filing has no row at all
    df = F.build(store)
    assert row(df, "CAN", "2026-04-20")["shares"] == 5.0e8            # not dropped as ads_unknown
