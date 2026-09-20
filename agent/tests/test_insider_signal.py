"""Insider candidate rules — the S11-S13 cuts, on a synthetic panel."""
import pandas as pd
import pytest

from agent import signals_insider
from hedge_fund.features.panel import PanelStore


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "p.db")
    dates = pd.bdate_range("2026-08-01", "2026-09-18")
    rows = []
    # MICRO ~ $0.3M ADV, SMALLC ~ $10M, MIDC ~ $50M, BIGC ~ $500M
    for t, px, vol in [("MICRO", 5.0, 60_000), ("SMALLC", 20.0, 500_000),
                       ("MIDC", 50.0, 1_000_000), ("BIGC", 100.0, 5_000_000)]:
        for d in dates:
            rows.append({"ticker": t, "trade_date": d.date(), "open": px, "high": px, "low": px,
                         "close": px, "adj_close": px, "volume": float(vol), "source": "test",
                         "fetched_at": pd.Timestamp.now()})
    s.upsert_bars(pd.DataFrame(rows))
    s.con.execute("""CREATE TABLE IF NOT EXISTS spread_grid (bucket VARCHAR, year INT, n_obs INT,
                     median_spread_pct DOUBLE, p75_spread_pct DOUBLE, measured_at TIMESTAMP,
                     PRIMARY KEY (bucket, year))""")
    s.insert("spread_grid", pd.DataFrame([
        {"bucket": b, "year": 2026, "n_obs": 20, "median_spread_pct": v, "p75_spread_pct": v,
         "measured_at": pd.Timestamp.now()}
        for b, v in [("micro", 1.41), ("small", 0.39), ("mid", 0.21), ("large", 0.11)]]))
    yield s
    s.close()


def insider(store, ticker, owners, usd, day="2026-09-17", code="P"):
    rows = [{"accession": f"{ticker}{i}", "ticker": ticker, "issuer_cik": "1",
             "filing_date": pd.Timestamp(day).date(), "trans_date": pd.Timestamp(day).date(),
             "owner_name": f"OWNER{i}", "relationship": "isOfficer", "officer_title": "CEO",
             "trans_code": code, "acq_disp": "A", "shares": usd / owners / 10.0, "price": 10.0,
             "value_usd": usd / owners, "shares_after": 0.0, "source": "test",
             "fetched_at": pd.Timestamp.now()} for i in range(owners)]
    store.insert("insider_tx", pd.DataFrame(rows))


def test_cluster_and_big_dollar_qualify_single_small_buy_does_not(store):
    insider(store, "SMALLC", owners=2, usd=20_000)        # cluster
    insider(store, "MIDC", owners=1, usd=300_000)         # big dollar
    insider(store, "BIGC", owners=1, usd=1_000)           # neither
    df = signals_insider.candidates(store, pd.Timestamp("2026-09-18"), window=3)
    assert set(df["ticker"]) == {"SMALLC", "MIDC"}
    assert dict(zip(df["ticker"], df["kind"])) == {"SMALLC": "cluster", "MIDC": "big_usd"}


def test_micro_and_large_are_excluded_with_a_reason(store):
    insider(store, "MICRO", owners=3, usd=50_000)
    insider(store, "BIGC", owners=3, usd=50_000)
    insider(store, "MIDC", owners=3, usd=50_000)
    df = signals_insider.candidates(store, pd.Timestamp("2026-09-18"), window=3)
    by = df.set_index("ticker")
    assert by.loc["MICRO", "reason"] == "adv_below_floor"
    assert by.loc["BIGC", "reason"] == "adv_above_ceiling"
    assert by.loc["MIDC", "eligible"]


def test_only_open_market_purchases_count(store):
    insider(store, "MIDC", owners=3, usd=500_000, code="S")   # sales
    assert signals_insider.candidates(store, pd.Timestamp("2026-09-18"), window=3).empty


def test_window_is_by_filing_date(store):
    insider(store, "MIDC", owners=3, usd=500_000, day="2026-09-01")
    assert signals_insider.candidates(store, pd.Timestamp("2026-09-18"), window=3).empty
    assert not signals_insider.candidates(store, pd.Timestamp("2026-09-18"), window=30).empty


def test_expected_edge_charges_half_the_measured_spread(store):
    e = signals_insider.expected_edge(store, "small", 2026)
    assert e["quoted_spread_pct"] == pytest.approx(0.39)
    assert e["net_at_half_spread_pct"] == pytest.approx(0.378 - 0.195)
    assert signals_insider.expected_edge(store, "micro", 2026)["net_at_half_spread_pct"] < 0
