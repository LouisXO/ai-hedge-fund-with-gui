"""S47b items 2 and 3: a reused ticker's bars split into two securities, class-share spellings, the listing
supplement. No network, temp DuckDB."""
import numpy as np
import pandas as pd
import pytest

from agent import audit
from agent.books import data, segments
from agent.books.segments import seams, split_points
from agent.s11_insider_wide import listed_mask
from agent.sources import alpaca_bars as ab
from hedge_fund.features.panel import PanelStore

NONE = pd.DataFrame(columns=segments.LISTING + ["exchange", "source", "checked"])     # no supplement rows


def listing(*r):
    return pd.DataFrame([x[:4] for x in r], columns=segments.LISTING).assign(
        ipo_date=lambda d: pd.to_datetime(d["ipo_date"]), delisting_date=lambda d: pd.to_datetime(d["delisting_date"]))


def facts(**by_ticker):
    """ticker=(first_bar, close_before, close_after[, listing symbol])"""
    return pd.DataFrame([{"ticker": t, "symbol": v[3] if len(v) > 3 else t, "first_bar": pd.Timestamp(v[0]),
                          "close_before": v[1], "close_after": v[2]} for t, v in by_ticker.items()]).set_index("ticker")


SESSIONS = pd.bdate_range("2010-01-01", "2026-12-31")


# -- the split rule (pure) ----------------------------------------------------

def test_another_company_under_the_ticker_is_split_at_the_new_listing():
    # SE: Spectra Energy to 2017-07-10, Sea Ltd from 2017-10-20
    sm = seams(listing(("SE", "Delisted", "2007-01-03", "2017-07-10"), ("SE", "Active", "2017-10-20", None)))
    sp = split_points(sm, facts(SE=("2012-11-27", 30.0, 15.0)), SESSIONS)
    r = sp.loc["SE"]
    assert r["split"] and r["pseudo"] == "SE@2007" and r["new_start"] == pd.Timestamp("2017-10-20")
    assert r["cutoff"] == pd.Timestamp("2017-10-05")                     # 15 days of when-issued bars stay
    assert r["gap_sessions"] > 5


def test_the_old_security_is_never_the_new_ones_when_issued_bars():
    # WOLF: old stock to Friday 2025-09-26, new stock from Monday 2025-09-29, 17x apart
    sm = seams(listing(("WOLF", "Delisted", "1993-02-09", "2025-09-26"), ("WOLF", "Active", "2025-09-29", None)))
    r = split_points(sm, facts(WOLF=("2015-01-02", 1.5, 26.0)), SESSIONS).loc["WOLF"]
    assert r["split"] and r["gap_sessions"] == 0 and r["pseudo"] == "WOLF@1993"
    assert r["cutoff"] == pd.Timestamp("2025-09-27")                     # the day after the old end, not 09-14


def test_one_security_under_two_rows_is_kept_whole():
    # a redomicile: two sessions between the rows, the close moves 3%
    sm = seams(listing(("RDM", "Delisted", "2001-05-01", "2021-03-10"), ("RDM", "Active", "2021-03-15", None)))
    r = split_points(sm, facts(RDM=("2015-01-02", 40.0, 41.2)), SESSIONS).loc["RDM"]
    assert r["gap_sessions"] == 2 and not r["split"]


@pytest.mark.parametrize("new_start, after, why", [
    ("2021-03-19", 41.2, "six sessions between the rows"),
    ("2021-03-12", 60.0, "the close moved 50%"),
    ("2021-03-12", np.nan, "no close after the new start: cannot be shown to be one security"),
])
def test_the_exception_needs_both_a_short_gap_and_a_close_that_carries_on(new_start, after, why):
    sm = seams(listing(("X", "Delisted", "2001-05-01", "2021-03-10"), ("X", "Active", new_start, None)))
    assert split_points(sm, facts(X=("2015-01-02", 40.0, after)), SESSIONS).loc["X", "split"], why


def test_no_split_without_bars_before_the_when_issued_days_or_without_an_earlier_end():
    sm = seams(listing(("NEWCO", "Delisted", "1990-01-02", "2005-01-03"), ("NEWCO", "Active", "2020-06-01", None),
                       ("OKE", "Active", "1985-07-01", None), ("OKE", "Delisted", "1985-07-01", "2026-09-14"),
                       ("GONE", "Delisted", "2010-01-04", "2016-01-04")))
    assert list(sm.index) == ["NEWCO"]                                   # OKE's Delisted row ends after its start
    assert split_points(sm, facts(NEWCO=("2020-05-20", np.nan, 12.0)), SESSIONS).empty   # 12 days early: when-issued


def test_several_old_intervals_go_to_one_pseudo_named_after_the_latest():
    sm = seams(listing(("AB", "Delisted", "1998-01-02", "2004-01-02"), ("AB", "Delisted", "2008-03-03", "2016-05-20"),
                       ("AB", "Active", "2019-06-03", None)))
    assert sm.loc["AB", "n_old"] == 2 and sm.loc["AB", "old_end"] == pd.Timestamp("2016-05-20")
    assert split_points(sm, facts(AB=("2012-11-27", 9.0, 20.0)), SESSIONS).loc["AB", "pseudo"] == "AB@2008"


# -- applying a split -------------------------------------------------------

def test_bars_before_the_cutoff_move_to_the_pseudo_column_in_every_frame():
    idx = pd.bdate_range("2017-09-25", "2017-10-31")
    close = pd.DataFrame({"SE": 30.0, "AAA": 5.0}, index=idx)
    sp = split_points(seams(listing(("SE", "Delisted", "2007-01-03", "2017-07-10"), ("SE", "Active", "2017-10-20", None))),
                      facts(SE=("2012-11-27", 30.0, 15.0)), SESSIONS)
    out = segments.split_bars({"close": close, "vol": close * 100}, sp)
    for f in out.values():
        assert list(f.columns) == ["SE", "AAA", "SE@2007"]
        assert f.loc[:"2017-10-04", "SE"].isna().all() and f.loc["2017-10-05":, "SE"].notna().all()
        assert f.loc[:"2017-10-04", "SE@2007"].notna().all() and f.loc["2017-10-05":, "SE@2007"].isna().all()
    late = segments.split_bars({"close": close.loc["2017-10-10":]}, sp)["close"]     # a load window after the seam
    assert "SE@2007" not in late.columns and late["SE"].notna().all()


def test_fundamentals_filed_before_the_new_listing_are_the_old_securitys():
    sp = pd.DataFrame({"pseudo": ["SE@2007"], "new_start": [pd.Timestamp("2017-10-20")]}, index=["SE"])
    df = pd.DataFrame({"ticker": ["SE", "SE", "AAA"], "filed": ["2017-05-01", "2018-03-01", "2017-05-01"]})
    assert segments.split_fundamentals(df, sp)["ticker"].tolist() == ["SE@2007", "SE", "AAA"]


# -- on a panel ---------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


def put_listing(store, *r):
    df = listing(*r)
    df["name"], df["exchange"], df["fetched_at"] = df["symbol"] + " Inc", "NYSE", pd.Timestamp("2026-09-27")
    df["asset_type"] = "Stock"
    for c in ("ipo_date", "delisting_date"):
        df[c] = df[c].dt.date
    store.insert("listing_status", df)


def put_bars(store, ticker, days, px, vol=1e6):
    px = np.broadcast_to(np.asarray(px, dtype=float), (len(days),))
    store.upsert_bars(pd.DataFrame({"ticker": ticker, "trade_date": [d.date() for d in days], "open": px, "high": px,
                                    "low": px, "close": px, "adj_close": px, "volume": vol, "source": "alpaca",
                                    "fetched_at": pd.Timestamp("2026-09-28")}))


DAYS = pd.bdate_range("2025-06-02", "2026-09-28")


def test_listing_mask_gives_the_pseudo_the_old_interval_and_the_ticker_the_new(store):
    put_listing(store, ("WOLF", "Delisted", "1993-02-09", "2025-09-26"), ("WOLF", "Active", "2025-09-29", None))
    put_bars(store, "WOLF", DAYS, np.where(DAYS < "2025-09-29", 1.5, 26.0))
    sp = segments.load_splits(store, extra=NONE)
    assert sp.index.tolist() == ["WOLF"] and sp.loc["WOLF", "close_before"] == 1.5 and sp.loc["WOLF", "close_after"] == 26.0
    m = listed_mask(store, DAYS, ["WOLF", "WOLF@1993"], extra=NONE)
    assert m.loc[:"2025-09-26", "WOLF@1993"].all() and not m.loc["2025-09-29":, "WOLF@1993"].any()
    assert not m.loc[:"2025-09-26", "WOLF"].any() and m.loc["2025-09-29":, "WOLF"].all()
    # a caller whose bars are not split: the ticker is the current security only
    assert listed_mask(store, DAYS, ["WOLF"], extra=NONE)["WOLF"].tolist() == (DAYS >= "2025-09-29").tolist()


def test_a_redomicile_keeps_one_column_and_both_intervals(store):
    put_listing(store, ("RDM", "Delisted", "2001-05-01", "2025-09-26"), ("RDM", "Active", "2025-09-30", None))
    put_bars(store, "RDM", DAYS, 40.0)
    assert segments.load_splits(store, extra=NONE).empty
    m = listed_mask(store, DAYS, ["RDM"], extra=NONE)["RDM"]
    assert m.loc[:"2025-09-26"].all() and not m.loc["2025-09-29"] and m.loc["2025-09-30":].all()


def test_class_share_spellings_match_and_one_security_is_never_two_columns(store):
    put_listing(store, ("BRK-B", "Active", "1996-05-09", None), ("BF-B", "Active", "1984-09-07", None))
    m = listed_mask(store, DAYS, ["BRK.B", "BF.B", "BF-B"], extra=NONE)
    assert m["BRK.B"].all()                                              # yfinance's dot, the vendor's hyphen
    assert m["BF-B"].all() and not m["BF.B"].any()                       # both spellings: the row's own only


def test_the_supplement_is_an_active_listing_and_joins_the_bars_universe(store, tmp_path):
    sup = segments.supplement()
    nrg = sup.set_index("symbol").loc["NRG"]
    assert nrg["exchange"] == "NYSE" and nrg["ipo_date"] == pd.Timestamp("2003-12-05") and nrg["source"]
    m = listed_mask(store, DAYS, ["NRG"])                                # reads agent/listing_supplement.yaml
    assert m["NRG"].all() and not listed_mask(store, DAYS, ["NRG"], extra=NONE)["NRG"].any()
    put_listing(store, ("AAA", "Active", "2010-01-04", None))
    store.insert("issuer_seen", pd.DataFrame({"ticker": ["AAA", "NRG"], "cik": "1", "quarter": "2026q2"}))
    assert ab.universe(store) == ["AAA", "NRG"] and ab.universe(store, extra=NONE) == ["AAA"]
    bad = tmp_path / "s.yaml"
    bad.write_text("rows:\n  - {symbol: ZZZ, ipo_date: 2001-01-02, exchange: NYSE}\n")
    with pytest.raises(ValueError, match="ZZZ lacks source, checked"):
        segments.supplement(str(bad))


def test_audit_warns_on_a_liquid_name_without_any_listing_row(store):
    put_listing(store, ("AAA", "Active", "2010-01-04", None), ("BRK-B", "Active", "1996-05-09", None))
    for t in ("AAA", "BRK.B", "ORPH", "NRG"):
        put_bars(store, t, DAYS[-60:], 50.0)
    put_bars(store, "TINY", DAYS[-60:], 1.0, vol=100)
    rep = audit.Report()
    audit.audit_listing(store, rep)
    row = [r for r in rep.rows if "no listing row" in r[1]][0]
    assert row[2] == "WARN" and "['ORPH']" in row[3]                    # BRK.B has BRK-B, NRG the supplement


# -- downstream: the market, the factors, the live list ----------------------------

def _panel(store, n=24):
    """n ordinary names, and SE: another company before 2026-03-02 (Delisted 2026-01-30), then the new one."""
    days = pd.bdate_range("2025-01-02", "2026-09-28")
    rng = np.random.default_rng(7)
    rows = [("SE", "Delisted", "2007-01-03", "2026-01-30"), ("SE", "Active", "2026-03-02", None)]
    fund = []
    for i in range(n):
        t = f"N{i:02d}"
        rows.append((t, "Active", "2000-01-03", None))
        put_bars(store, t, days, 20 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days)))), vol=5e5)
    put_bars(store, "SE", days[days <= "2026-01-30"], 30 * np.exp(np.cumsum(rng.normal(0, 0.01, (days <= "2026-01-30").sum()))), vol=5e5)
    put_bars(store, "SE", days[days >= "2026-02-20"], 90.0 + np.arange((days >= "2026-02-20").sum()) * 0.1, vol=5e5)
    put_listing(store, *rows)
    for t in [f"N{i:02d}" for i in range(n)] + ["SE"]:
        for filed in ("2025-08-01", "2025-11-01", "2026-05-01", "2026-08-01"):
            fund.append({"cik": 1, "period_end": pd.Timestamp(filed) - pd.Timedelta(days=35), "filed": pd.Timestamp(filed),
                         "rev_ttm": 1e9, "gp_ttm": 4e8 * rng.uniform(0.5, 1.5), "cogs_ttm": 6e8, "opinc_ttm": 1e8,
                         "cfo_ttm": 1.2e8, "assets": 2e9, "equity": 1e9 * rng.uniform(0.5, 1.5), "debt": 5e8,
                         "ni_ttm": 1e8 * rng.uniform(0.5, 1.5), "shares": 5e7, "shares_asof": pd.Timestamp(filed),
                         "shares_flag": None, "ticker": t, "assets_1y": 1.9e9})
    f = pd.DataFrame(fund)
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM f")
    store.con.execute("CREATE TABLE spread_grid (bucket VARCHAR, year INT, median_spread_pct DOUBLE)")
    store.con.execute("INSERT INTO spread_grid VALUES ('mid', 2026, 0.1), ('small', 2026, 0.2)")
    store.upsert_index(pd.DataFrame({"symbol": "SPY", "trade_date": [d.date() for d in days], "open": 500.0, "high": 500.0,
                                     "low": 500.0, "close": 500.0, "adj_close": 500.0, "source": "t",
                                     "fetched_at": pd.Timestamp("2026-09-28")}))
    return days


def test_market_factors_and_the_live_list_work_with_a_pseudo_ticker(store, monkeypatch):
    from agent.books.factors import factor_scores
    from agent.books.live import targets
    monkeypatch.setattr(segments, "supplement", lambda path=None: NONE)   # NRG plays no part here
    days = _panel(store)
    m = data.load_market(store, "2026-01-02")
    assert "SE@2007" in m.adj.columns
    assert m.adj.loc[:"2026-02-13", "SE"].isna().all() and m.adj.loc["2026-02-20":, "SE"].notna().all()
    assert m.listed.loc["2026-01-30", "SE@2007"] and not m.listed.loc["2026-01-30", "SE"]
    assert not m.listed.iloc[-1]["SE@2007"] and m.listed.iloc[-1]["SE"]
    fund = data.fundamentals(store)
    assert set(fund.loc[fund["ticker"] == "SE@2007", "filed"].dt.strftime("%Y-%m-%d")) == {"2025-08-01", "2025-11-01"}
    # a backtest day in the old security's life scores the pseudo ticker with its own fundamentals
    old_day = pd.Timestamp("2026-01-28")
    uni = m.tradable(1e6, np.inf).loc[old_day]
    fs = factor_scores(m, fund, old_day, uni[uni].index)
    assert "SE@2007" in fs.index and "SE" not in fs.index and fs.loc["SE@2007", "value"] == fs.loc["SE@2007", "value"]
    # the live list is for the last bar: the real ticker may be on it, the pseudo never
    lst = targets(store, m, m.adj.index[-1])
    assert lst and not any(segments.is_pseudo(r["ticker"]) for r in lst)
    assert days[-1] == m.adj.index[-1]


def test_a_pseudo_ticker_listed_on_the_last_bar_stops_the_market(store, monkeypatch):
    monkeypatch.setattr(segments, "supplement", lambda path=None: NONE)
    put_listing(store, ("ZZ", "Delisted", "2001-01-02", "2026-09-28"), ("ZZ", "Active", "2026-10-05", None))
    put_bars(store, "ZZ", DAYS, 10.0)
    store.upsert_index(pd.DataFrame({"symbol": "SPY", "trade_date": [d.date() for d in DAYS], "open": 1.0, "high": 1.0,
                                     "low": 1.0, "close": 1.0, "adj_close": 1.0, "source": "t", "fetched_at": pd.Timestamp("2026-09-28")}))
    store.con.execute("CREATE TABLE spread_grid (bucket VARCHAR, year INT, median_spread_pct DOUBLE)")
    with pytest.raises(RuntimeError, match=r"pseudo tickers listed on 2026-09-28: \['ZZ@2001'\]"):
        data.load_market(store, "2026-06-01")
