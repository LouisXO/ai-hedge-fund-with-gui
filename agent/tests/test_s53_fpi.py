"""S53 shadow line: foreign-issuer fundamentals (IFRS, currencies, 6-K, annual-only) and the scoring-day rows.
Temp DuckDB, no network; made-up values."""
import numpy as np
import pandas as pd
import pytest

from agent.books import fundamentals_fpi as FF
from agent.s53_fpi import day_rows
from agent.sources import ads_ratio as A
from agent.sources import fpi_data
from agent.sources.sec_xbrl import DDL as XBRL_DDL
from hedge_fund.features.panel import PanelStore


@pytest.fixture
def fpi(tmp_path):
    s = PanelStore(tmp_path / "fpi.db")
    s.con.execute("""CREATE TABLE xbrl_facts (cik INT, ns VARCHAR, tag VARCHAR, unit VARCHAR, period_start DATE,
        period_end DATE, is_instant BOOLEAN, val DOUBLE, accn VARCHAR, fy INT, fp VARCHAR, form VARCHAR, filed DATE,
        frame VARCHAR, PRIMARY KEY (cik, ns, tag, unit, period_start, period_end, accn))""")
    s.con.execute(A.DDL)
    s.con.execute(fpi_data.ISSUER_DDL)
    yield s
    s.close()


def fact(cik, ns, tag, unit, start, end, val, form, filed, accn, instant=False):
    return dict(cik=cik, ns=ns, tag=tag, unit=unit, period_start=start, period_end=end, is_instant=instant, val=val,
                accn=accn, fy=None, fp=None, form=form, filed=filed, frame=None)


def load(s, rows, issuers, ratios=()):
    df = pd.DataFrame(rows)
    for c in ("period_start", "period_end", "filed"):
        df[c] = pd.to_datetime(df[c]).dt.date
    s.insert("xbrl_facts", df)
    s.insert("fpi_issuers", pd.DataFrame([dict(cik=c, ticker=t, exchange="NYSE", name=t, n20f=1, n40f=0, n6k=0, in_panel=False,
                                               as_of=pd.Timestamp("2026-10-14").date()) for c, t in issuers]))
    if ratios:
        r = pd.DataFrame([dict(cik=c, accn=a, form="20-F", filed=pd.Timestamp(f).date(), ratio=x, status=st, cover_text="",
                               doc="", source="t", fetched_at=pd.Timestamp("2026-10-14")) for c, a, f, x, st in ratios])
        s.insert("ads_ratio", r)


def year(cik, accn, filed, fy_end, unit, rev, ni, assets, equity, form="20-F", ns="ifrs-full"):
    start = (pd.Timestamp(fy_end) - pd.Timedelta(days=364)).date().isoformat()
    return [fact(cik, ns, "Revenue", unit, start, fy_end, rev, form, filed, accn),
            fact(cik, ns, "ProfitLossAttributableToOwnersOfParent", unit, start, fy_end, ni, form, filed, accn),
            fact(cik, ns, "Assets", unit, fy_end, fy_end, assets, form, filed, accn, True),
            fact(cik, ns, "EquityAttributableToOwnersOfParent", unit, fy_end, fy_end, equity, form, filed, accn, True)]


def test_ifrs_facts_in_the_reporting_currency_and_listed_units(fpi):
    rows = year(1, "a1", "2026-03-01", "2025-12-31", "EUR", 1000.0, 100.0, 5000.0, 2000.0)
    # a convenience translation of the same year in USD: dropped
    rows += year(1, "a1", "2026-03-01", "2025-12-31", "USD", 1100.0, 110.0, 5500.0, 2200.0)
    rows += [fact(1, "dei", "EntityCommonStockSharesOutstanding", "shares", "2026-02-28", "2026-02-28", 8.0e8, "20-F",
                  "2026-03-01", "a1", True)]
    load(fpi, rows, [(1, "EUA")], ratios=[(1, "a1", "2026-03-01", 4.0, "parsed_12b")])
    out = FF.build_fpi(fpi)
    r = out[out["ticker"] == "EUA"].iloc[-1]
    assert r["currency"] == "EUR"
    assert r["rev_ttm"] == 1000.0 and r["ni_ttm"] == 100.0 and r["equity"] == 2000.0
    assert r["shares"] == pytest.approx(2.0e8)                      # 8e8 ordinary / 4 per ADS


def test_a_6k_count_takes_the_latest_20f_ratio(fpi):
    rows = year(1, "a1", "2026-03-01", "2025-12-31", "EUR", 1000.0, 100.0, 5000.0, 2000.0)
    rows += [fact(1, "ifrs-full", "WeightedAverageShares", "shares", "2026-01-01", "2026-03-31", 8.0e8, "6-K", "2026-05-01", "k1"),
             fact(1, "ifrs-full", "Assets", "EUR", "2026-03-31", "2026-03-31", 5100.0, "6-K", "2026-05-01", "k1", True)]
    load(fpi, rows, [(1, "EUA")], ratios=[(1, "a1", "2026-03-01", 4.0, "parsed_12b")])
    out = FF.build_fpi(fpi)
    r = out[(out["ticker"] == "EUA") & (out["filed"] == pd.Timestamp("2026-05-01"))].iloc[0]
    assert r["shares"] == pytest.approx(2.0e8)


def test_annual_only_flag(fpi):
    rows = year(1, "a1", "2025-03-01", "2024-12-31", "DKK", 1.0e4, 1.0e3, 5.0e4, 2.0e4)
    rows += year(1, "a2", "2026-03-01", "2025-12-31", "DKK", 1.1e4, 1.1e3, 5.5e4, 2.2e4)
    rows += year(2, "b1", "2026-03-01", "2025-12-31", "CAD", 1.0e4, 1.0e3, 5.0e4, 2.0e4, form="40-F")
    rows += [fact(2, "ifrs-full", "Revenue", "CAD", "2025-07-01", "2025-09-30", 2.6e3, "6-K", "2025-11-01", "b0")]
    load(fpi, rows, [(1, "ANN"), (2, "QTR")])
    out = FF.build_fpi(fpi)
    assert bool(out[out["ticker"] == "ANN"].iloc[-1]["annual_only"]) is True
    assert bool(out[(out["ticker"] == "QTR") & (out["filed"] == pd.Timestamp("2026-03-01"))].iloc[0]["annual_only"]) is False


def rows_frame():
    return pd.DataFrame([
        dict(ticker="US1", cik=1, filed=pd.Timestamp("2026-06-01"), currency="USD", annual_only=False, assets=100.0, equity=50.0),
        dict(ticker="EU1", cik=2, filed=pd.Timestamp("2026-06-01"), currency="EUR", annual_only=False, assets=100.0, equity=50.0),
        dict(ticker="OLD", cik=3, filed=pd.Timestamp("2025-09-01"), currency="TWD", annual_only=True, assets=3200.0, equity=1600.0),
        dict(ticker="STALE", cik=4, filed=pd.Timestamp("2025-09-01"), currency="USD", annual_only=False, assets=1.0, equity=1.0),
        dict(ticker="NOFX", cik=5, filed=pd.Timestamp("2026-06-01"), currency="XYZ", annual_only=False, assets=1.0, equity=1.0)])


def test_day_rows_converts_money_at_the_previous_days_rate_and_applies_staleness():
    fx = pd.DataFrame({"EUR": [1.10, 1.20, 9.99], "TWD": [1 / 32, 1 / 32, 1 / 30]},
                      index=pd.to_datetime(["2026-10-12", "2026-10-13", "2026-10-14"]))
    f = day_rows(rows_frame(), fx, pd.Timestamp("2026-10-14"))
    assert f.loc["EU1", "assets"] == pytest.approx(120.0)            # 10-13's rate, not 10-14's
    assert f.loc["US1", "assets"] == 100.0
    assert f.loc["OLD", "assets"] == pytest.approx(100.0)            # annual-only: 408 days old still stands
    assert "STALE" not in f.index                                    # 408 days, not annual-only
    assert "NOFX" not in f.index                                     # no rate for the currency: dropped
