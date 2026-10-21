"""S54 long frozen-benchmark alarm (agent/drift.py), on synthetic data: the threshold table's construction, the frozen
table against the registration, and the daily check's window, compounding, levels and missing data."""
import hashlib
import json
import os

import numpy as np
import pandas as pd
import pytest

from agent import drift
from hedge_fund.features.panel import PanelStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG = drift.s54_config()
SYMS = ["SPY", "IWM", "MTUM", "VLUE", "QUAL", "USMV"]


def _factors(days: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = pd.DataFrame(rng.normal(0.0004, 0.01, (len(days), len(SYMS))), index=days, columns=SYMS)
    return pd.DataFrame({"SPY": r["SPY"], **{f"{s}-SPY": r[s] - r["SPY"] for s in SYMS[1:]}})


def test_window_percentiles_are_the_percentile_of_every_compounded_run():
    diff = pd.Series(np.random.default_rng(1).normal(0, 0.01, 300))
    got = drift.window_percentiles(diff, 30, 5.0)
    for L in (1, 7, 30):
        runs = (1 + diff).rolling(L).apply(np.prod, raw=True).dropna() - 1        # replica.py's construction
        assert got[L - 1] == pytest.approx(runs.quantile(0.05) * 100, abs=1e-9)


def test_the_frozen_table_reproduces_the_registration_and_matches_the_config():
    t = json.load(open(os.path.join(ROOT, CFG["thresholds"])))
    assert t["fits"] == CFG["fits"] and t["window"] == CFG["window"] == 189 and t["percentile"] == 5.0
    for name, reg in {"alpha2_fit": -13.7, "etf_replica": -18.9}.items():
        thr = t["thresholds_pct"][name]
        assert len(thr) == 189 and round(thr[-1], 1) == reg == CFG["registered_threshold_pct"][name]
        assert all(b <= a + 1e-9 for a, b in zip(thr[:20], thr[1:21]))           # longer windows reach lower, early on
    nav = os.path.join(ROOT, CFG["nav"])
    assert t["inputs"]["nav_sha1"] == hashlib.sha1(open(nav, "rb").read()).hexdigest()   # the NAV it was built from


def test_the_table_is_never_rebuilt(capsys):
    path = os.path.join(ROOT, CFG["thresholds"])
    before = open(path, "rb").read()
    assert drift.main(["--build-alarm-thresholds"]) == 1                              # exists: frozen, no panel opened
    assert open(path, "rb").read() == before and "frozen" in capsys.readouterr().out


def test_build_alarm_thresholds_on_a_synthetic_backtest(tmp_path):
    days = pd.bdate_range("2020-01-01", periods=260)
    f = _factors(days)
    px = (1 + pd.DataFrame({s: f[s if s == "SPY" else f"{s}-SPY"] + (0 if s == "SPY" else f["SPY"]) for s in SYMS})).cumprod() * 100
    with PanelStore(tmp_path / "p.db") as s:
        s.upsert_index(pd.DataFrame([{"symbol": c, "trade_date": d.date(), "open": v, "high": v, "low": v, "close": v,
                                      "adj_close": v, "source": "t", "fetched_at": pd.Timestamp.now()}
                                     for c in SYMS for d, v in px[c].items()]))
        fit = drift.fit_return(drift.factor_returns(s), CFG["fits"]["alpha2_fit"])
        book = fit + np.random.default_rng(2).normal(0, 0.005, len(fit))              # fit + noise: alpha 0
        nav = (1 + book.fillna(0)).cumprod().rename("v1c")
        nav.index.name = "trade_date"
        nav.to_csv(tmp_path / "nav.csv")
        cfg = {**CFG, "window": 20}
        t = drift.build_alarm_thresholds(s, cfg, str(tmp_path / "nav.csv"))
    diff = (nav.pct_change().dropna() - fit.reindex(nav.index)).dropna()
    assert t["thresholds_pct"]["alpha2_fit"] == [round(v, 4) for v in drift.window_percentiles(diff, 20, 5.0)]
    assert t["check_at_window"]["alpha2_fit"]["n_days"] == len(diff) == 259
    assert len(t["thresholds_pct"]["etf_replica"]) == 20 and t["window"] == 20


def _table(L: int = 189, alpha2: float = -5.0, etf: float = -6.0) -> dict:
    return {"thresholds_pct": {"alpha2_fit": [alpha2] * L, "etf_replica": [etf] * L}}


def test_the_alarm_compounds_book_minus_fit_since_the_first_fill():
    days = pd.bdate_range("2026-09-01", periods=40)
    f = _factors(days, 3)
    oc = _factors(days, 13)
    fit = drift.fit_return(f, CFG["fits"]["alpha2_fit"])
    fit.iloc[10] = drift.fit_return(oc, CFG["fits"]["alpha2_fit"]).iloc[10]             # bought at the first-fill open
    first_fill = days[10].date()
    nav = pd.Series(1.0, index=days)
    nav.iloc[10:] = (1 + fit.iloc[10:]).cumprod().to_numpy()                          # the book is the fit from the first fill
    nav.iloc[25] = np.nan                                                              # a day the NAV skipped
    c = {x["fit"]: x for x in drift.long_alarm(nav * 60000, first_fill, f, _table(), CFG, oc=oc)}
    a = c["alpha2_fit"]
    assert a["window"] == 30 and a["level"] == "ok" and a["cum_diff_pct"] == pytest.approx(0.0, abs=1e-9)
    assert "不满 189 天" in a["detail"] and "长线冻结基准报警" in a["name"]
    assert c["etf_replica"]["window"] == 30 and c["etf_replica"]["flagged"] is False
    worse = nav.copy()
    worse.iloc[30:] *= 0.9                                                             # -10% on day 30: below -5%
    c = {x["fit"]: x for x in drift.long_alarm(worse, first_fill, f, _table(), CFG, oc=oc)}
    assert c["alpha2_fit"]["level"] == "bad" and c["alpha2_fit"]["flagged"] and "实盘落在回测分布之外" in c["alpha2_fit"]["detail"]
    assert c["alpha2_fit"]["cum_diff_pct"] == pytest.approx(-10.0, abs=0.6)


def test_the_window_stops_at_189_sessions_and_uses_that_lengths_threshold():
    days = pd.bdate_range("2026-01-01", periods=230)
    f = _factors(days, 4)
    nav = pd.Series(np.r_[1.0, (1 + drift.fit_return(f, CFG["fits"]["alpha2_fit"]).iloc[1:]).cumprod()], index=days)
    table = {"thresholds_pct": {"alpha2_fit": [-1.0] * 188 + [-7.5], "etf_replica": [-1.0] * 188 + [-9.5]}}
    c = drift.long_alarm(nav, days[1].date(), f, table, CFG, oc=f * 0)
    a = [x for x in c if x["fit"] == "alpha2_fit"][0]
    assert a["window"] == 189 and a["threshold_pct"] == -7.5 and str(days[-189].date()) in a["detail"]


def test_missing_factor_data_warns_and_compares_nothing():
    days = pd.bdate_range("2026-09-01", periods=30)
    f = _factors(days, 5)
    f.loc[days[20], "MTUM-SPY"] = np.nan                                               # an ETF without that day's bar
    nav = pd.Series(np.linspace(1, 1.01, 30), index=days)
    c = {x["fit"]: x for x in drift.long_alarm(nav, days[5].date(), f, _table(), CFG, oc=f)}
    assert c["etf_replica"]["level"] == "warn" and "因子数据缺" in c["etf_replica"]["detail"]
    assert c["alpha2_fit"]["level"] in ("ok", "bad")                                   # SPY and IWM are there
    c = drift.long_alarm(nav, days[5].date(), f.iloc[:-1], _table(), CFG, oc=f)         # index_daily one day behind the NAV
    assert [x["level"] for x in c] == ["warn", "warn"]
    assert drift.long_alarm(nav, days[0].date(), f, _table(), CFG, oc=f) == []          # no NAV before the first fill


def test_the_first_fill_session_is_open_to_close_on_both_sides():
    """Review fix (2026-10-21): the NAV's base is the close before the first fill (cash), and the book bought at that
    session's open, so the fit takes the regressors' open-to-close return that day. Here SPY and IWM gap up +1% overnight
    into the first fill and are flat from the open; the book is flat: book - fit is 0, not -1.8% (the 10-22 case)."""
    days = pd.bdate_range("2026-09-14", periods=12)
    f = pd.DataFrame(0.0, index=days, columns=["SPY", "IWM-SPY", "MTUM-SPY", "VLUE-SPY", "QUAL-SPY", "USMV-SPY"])
    oc = f.copy()
    f.loc[days[5], "SPY"] = 0.01                                                       # close to close: the overnight gap
    nav = pd.Series(60000.0, index=days)                                               # flat cash, then flat holdings
    c = {x["fit"]: x for x in drift.long_alarm(nav, days[5].date(), f, _table(), CFG, oc=oc)}
    a = c["alpha2_fit"]
    assert a["window"] == 7 and a["fit_pct"] == pytest.approx(0.0) and a["cum_diff_pct"] == pytest.approx(0.0, abs=1e-12)
    assert "首日按开盘到收盘" in a["detail"]
    later = drift.long_alarm(nav, days[5].date(), f, {"thresholds_pct": {k: [-5.0] * 3 for k in CFG["fits"]}},
                             {**CFG, "window": 3}, oc=oc)                               # window past the first fill: close to close
    assert all("首日" not in x["detail"] and x["window"] == 3 for x in later)
    oc.loc[days[5], "IWM-SPY"] = np.nan                                                # no open for that day: nothing compared
    assert {x["level"] for x in drift.long_alarm(nav, days[5].date(), f, _table(), CFG, oc=oc)} == {"warn"}
