"""Weekly attribution (style split, stock layer without its largest name) and the industry map (S48).
In-memory DuckDB and a hand-built market; no panel, no network."""
import types

import duckdb
import numpy as np
import pandas as pd
import pytest

from agent import attribution as A
from agent.books import industry as I


def _store(rows):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE index_daily (symbol VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, adj_close DOUBLE)")
    con.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?, ?)", rows)
    return types.SimpleNamespace(con=con)


def _market(days, tickers, rng):
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (len(days), len(tickers))), axis=0)), index=days, columns=tickers)
    opn = close.shift(1).fillna(100.0) * (1 + rng.normal(0, 0.005, close.shape))
    trad = pd.DataFrame(True, index=days, columns=tickers)
    return types.SimpleNamespace(adj=close, adj_open=opn, close=close, tradable=lambda lo, hi: trad)


def test_style_split_adds_up_and_stock_ex_top1():
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2026-09-14", periods=10)
    tickers = [f"T{i}" for i in range(15)]
    m = _market(days, tickers, rng)
    idx_rows = []
    for sym, drift in (("SPY", 0.001), ("IWM", -0.002)):
        c = 500 * np.cumprod(1 + drift + rng.normal(0, 0.01, len(days)))
        for d, cl in zip(days, c):
            idx_rows.append((sym, d.date(), cl * 0.999, cl, cl))
    store = _store(idx_rows)
    lots = pd.DataFrame([
        {"book": "long", "ticker": "T0", "qty": 10, "entry_day": days[5], "entry_px": float(m.adj_open.at[days[5], "T0"]) * 1.01, "exit_day": None, "exit_px": None},
        {"book": "long", "ticker": "T1", "qty": 50, "entry_day": days[4], "entry_px": None, "exit_day": days[8], "exit_px": float(m.adj_open.at[days[8], "T1"]) * 0.99},
        {"book": "long", "ticker": "T2", "qty": 5, "entry_day": days[3], "entry_px": None, "exit_day": None, "exit_px": None},
    ])
    groups = pd.Series({"T0": "Money", "T1": "Hlth"})           # T2 has no mapping -> Unclassified
    df = A.attribute(lots, m, store, groups, days[5:])
    assert len(df)
    assert np.allclose(df["style_size"] + df["style_pond"], df["style"])
    assert set(df.loc[df["ticker"] == "T2", "industry"]) == {"Unclassified"}
    # the layers still add up to the position's dollars (execution aside)
    L = A.legs(m, store, days[5:])
    r_i = [L[leg].at[d, t] for d, t, leg in zip(df["day"], df["ticker"], df["leg"])]
    assert np.allclose(df[["market", "style", "industry_layer", "stock"]].sum(axis=1), df["v0"] * np.array(r_i))
    ex = A.extras(df, 60_000.0)
    st = df.groupby("ticker")["stock"].sum()
    top = st.abs().idxmax()
    assert ex["stock_top1"]["ticker"] == top
    assert ex["stock_ex_top1_usd"] == pytest.approx(st.sum() - st[top])
    assert ex["style_split_usd"]["size_iwm_minus_spy"] + ex["style_split_usd"]["pond_ew_minus_iwm"] == pytest.approx(df["style"].sum())


def test_industry_map_takes_latest_filing_cik_and_unclassified(tmp_path, monkeypatch):
    sic = tmp_path / "company_sic.csv"
    pd.DataFrame({"cik": [1, 2, 3], "sic": [6020.0, 2834.0, None]}).to_csv(sic, index=False)
    monkeypatch.setattr(I, "SIC_FILE", sic)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fundamentals_pit (cik BIGINT, filed TIMESTAMP, ticker VARCHAR)")
    con.executemany("INSERT INTO fundamentals_pit VALUES (?, ?, ?)", [
        (1, "2020-01-01", "AAA"), (1, "2021-01-01", "AAA"), (2, "2025-06-01", "AAA"),   # symbol reused: the newest CIK wins
        (3, "2025-01-01", "BBB"),                                                      # CIK without a SIC code
        (4, "2025-01-01", "CCC"),                                                      # CIK not in the SIC file
        (2, "2024-01-01", "DDD"), (1, "2024-01-01", "DDD"),                            # same date: the larger CIK, every run
    ])
    store = types.SimpleNamespace(con=con)
    g = I.industry_by_ticker(store)
    assert g["AAA"] == "Hlth"
    assert g["BBB"] == "Unclassified" and g["CCC"] == "Unclassified"
    assert g["DDD"] == "Hlth"
    assert I.ff12(9999) == "Other" and I.ff12(None) == "Unclassified"
    assert all(I.industry_by_ticker(store).equals(g) for _ in range(3))
