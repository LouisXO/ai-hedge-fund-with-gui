"""S48: the ledger's de-duplication, S30 rows, pre-audit flags, and the deflated Sharpe."""
from __future__ import annotations

import json
import math

import numpy as np

import pandas as pd
import pytest

from hedge_fund.validation.family_log import (BASE_NAV, base_book_dsr, collect, deflated_sharpe, expected_max_t, holm,
                                              render)


def _book(t, n, a=5.0):
    return {"alpha2_t_nw": t, "n_trades": n, "alpha2_ann_pct": a, "cagr_pct": 10.0}


def _write(d, name, obj):
    (d / name).write_text(json.dumps(obj))


def test_collect_merges_reruns_counts_s30_and_flags_pre_audit(tmp_path):
    _write(tmp_path, "s24_long_v2_2026-09-22.json", {"books": {"base": _book(1.461, 1431), "n50": _book(1.89, 2132),
                                                               "rankz": _book(0.14, 824)}})
    _write(tmp_path, "s27_exec_cost_2026-09-24.json", {"books": {"long_half_spread": _book(1.4610004, 1431)}})
    _write(tmp_path, "s30_momentum_book_2026-09-22.json", {"full": {"v1_base": _book(1.4610001, 1431), "mom_top": _book(0.67, 1202)},
                                                           "ytd_2026": {"mom_top": _book(3.0, 50)}})
    _write(tmp_path, "s40_price_floor_2026-09-24.json", {"books": {"long_base": _book(1.461, 1431), "long_floor2": _book(1.57, 1435),
                                                                   "insider_x": _book(1.461, 999)}})
    _write(tmp_path, "s24_long_v2_2026-09-22_fund_v1.json", {"books": {"base": _book(2.25, 1500)}})     # archived: ignored
    rows = {r["variant"]: r for r in collect(str(tmp_path))}
    assert set(rows) == {"base", "n50", "rankz", "mom_top", "long_floor2", "insider_x"}
    assert len(rows["base"]["same_as"]) == 3                       # long_half_spread, v1_base, long_base
    assert rows["mom_top"]["t"] == 0.67                            # S30's full window, not its YTD slice
    assert rows["n50"].get("pre_audit") and not rows["rankz"].get("pre_audit") and not rows["base"].get("pre_audit")
    assert "same_as" not in rows["insider_x"]                      # same t, different trades: a different book
    text = render(holm(list(rows.values())))
    assert "修正前数据" in text and "Not computed" in text


def test_s37_baseline_is_not_a_variant(tmp_path):
    _write(tmp_path, "s37_largecap_2026-10-06.json", {"books": {"lc_mom": _book(0.58, 1049), "lc_ew": _book(-1.10, 6440)}})
    assert [r["variant"] for r in collect(str(tmp_path))] == ["lc_mom"]


def test_expected_max_t_matches_the_audit():
    assert round(expected_max_t(5), 2) == 1.19
    assert round(expected_max_t(13), 2) == 1.70
    assert round(expected_max_t(96), 2) == 2.52
    assert expected_max_t(1) == 0.0


def test_deflated_sharpe_is_psr_at_one_trial_and_falls_with_more_trials():
    rng = np.random.default_rng(0)
    x = rng.normal(0.0004, 0.01, 2400)                 # t around 2
    t_iid = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    psr = deflated_sharpe(x, 1)["dsr"]
    assert abs(psr - 0.5 * math.erfc(-t_iid / math.sqrt(2))) < 0.01        # normal data: PSR(0) ~ Phi(t)
    d = [deflated_sharpe(x, n)["dsr"] for n in (1, 5, 13, 100)]
    assert all(a > b for a, b in zip(d, d[1:]))
    assert deflated_sharpe(x, 1, nw_ratio=0.5)["dsr"] < psr                 # serial correlation widens it


def test_a_later_s24_rerun_replaces_the_row_without_the_pre_audit_flag(tmp_path):
    _write(tmp_path, "s24_long_v2_2026-09-22.json", {"books": {"n50": _book(1.89, 2132), "invvol": _book(1.20, 1500)}})
    _write(tmp_path, "s24_long_v2_2026-10-20.json", {"books": {"n50": _book(1.10, 2100)}})      # re-run on corrected data
    rows = {r["variant"]: r for r in collect(str(tmp_path))}
    assert rows["n50"]["report"] == "s24_long_v2_2026-10-20.json" and not rows["n50"].get("pre_audit")
    assert rows["invvol"].get("pre_audit")                        # not re-run yet: still the old data


def _panel_and_nav(tmp_path, n=600):
    from hedge_fund.features.panel import PanelStore

    rng = np.random.default_rng(1)
    days = pd.bdate_range("2020-01-02", periods=n)
    spy = rng.normal(0.0004, 0.01, n)
    iwm = spy + rng.normal(0.0, 0.006, n)
    book = 0.0003 + 0.9 * spy + 0.3 * (iwm - spy) + rng.normal(0.0, 0.006, n)
    frames = []
    for sym, r in (("SPY", spy), ("IWM", iwm)):
        px = 100 * np.cumprod(1 + r)
        frames.append(pd.DataFrame({"symbol": sym, "trade_date": days.date, "open": px, "high": px, "low": px,
                                    "close": px, "adj_close": px, "source": "test", "fetched_at": pd.Timestamp.now()}))
    panel = tmp_path / "panel.db"
    with PanelStore(panel) as st:
        st.upsert_index(pd.concat(frames, ignore_index=True))
    nav = tmp_path / BASE_NAV
    pd.DataFrame({"trade_date": days, "v1c": 60_000 * np.cumprod(1 + book)}).to_csv(nav, index=False)
    return str(nav), str(panel)


@pytest.mark.filterwarnings("ignore:.*encountered in matmul:RuntimeWarning")   # macOS Accelerate BLAS, results are finite
def test_base_book_dsr_reads_the_nav_and_panel_and_renders(tmp_path):
    nav, panel = _panel_and_nav(tmp_path)
    d = base_book_dsr(nav, panel, [5, 13, 40])
    assert d["nav_file"] == BASE_NAV and d["column"] == "v1c" and d["T"] == 599
    assert d["start"] == "2020-01-02" and abs(d["alpha2_ann_pct"] - 0.0003 * 252 * 100) < 5
    assert 0 < d["psr0"] < 1 and [x["n_trials"] for x in d["trials"]] == [5, 13, 40]
    dsrs = [x["dsr"] for x in d["trials"]]
    assert all(a > b for a, b in zip(dsrs, dsrs[1:])) and d["psr0"] > dsrs[0]
    assert d["dsr_range"] == [min(dsrs), max(dsrs)]
    assert abs(d["t_nw"] / d["t_iid"] - 1) < 0.5                  # iid data: NW and iid t are close
    text = render(holm([{"variant": "base", "report": "r.json", "t": 1.46, "alpha2_ann_pct": 8.1, "n_trades": 10}]), d)
    assert "PSR(0) = " in text and "DSR range " in text and "Not computed" not in text
    assert "latest restatement" in text                            # the ledger row and this NAV are different sources


# ---- S54: S53's top-level book, the S27 re-run, and the hand-kept rows (signal-level tests, measurements) ----
def test_s53_top_level_book_and_the_s27_rerun_are_counted_once(tmp_path):
    _write(tmp_path, "s53_fpi_backtest_2026-10-14.json", {"v1": _book(1.15, 1541), "fpi": _book(0.72, 1600), "meta": {"x": 1}})
    _write(tmp_path, "s27_exec_cost_2026-09-24.json", {"books": {"long_measured_0.61pct": _book(0.65, 1417)}})
    _write(tmp_path, "s27_exec_cost_2026-10-06.json", {"books": {"long_half_spread": _book(1.24, 1523),
                                                                 "long_measured_0.59pct": _book(0.43, 1517),
                                                                 "insider_half_spread": _book(2.04, 6745),
                                                                 "insider_measured_1.04pct": _book(-12.09, 6745)}})
    rows = {r["variant"]: r for r in collect(str(tmp_path))}
    assert rows["composite_long_fpi"]["t"] == 0.72 and "v1" not in rows                 # S53a's restated base is +0
    assert rows["long_measured_cost"]["report"] == "s27_exec_cost_2026-10-06.json"      # the re-run replaces 0.61%
    assert rows["insider_measured_cost"]["t"] == -12.09
    assert rows["base"]["n_trades"] == 1523 and rows["insider_v1_5d"]["n_trades"] == 6745  # the books themselves, re-run
    assert "long_half_spread" not in rows and "insider_half_spread" not in rows


def test_extra_rows_are_listed_apart_and_counted_in_the_trial_total(tmp_path):
    from hedge_fund.validation.family_log import extra_rows, trial_total
    (tmp_path / "family_log_extra.yaml").write_text(
        "rows:\n"
        "  - {variant: s32_unusual_options, family: signal, session: S32, n_trials: 2, best_t: -2.3, source: 'L542'}\n"
        "  - {variant: s54_insider_all_event_sprt, family: measurement, session: S54, n_trials: 1, best_t: null, source: 'L1051'}\n")
    _write(tmp_path, "s21_13d_2026-09-22.json", {"books": {"sch13d_h5": _book(-0.46, 300)}})
    rows, extra = holm(collect(str(tmp_path))), extra_rows(str(tmp_path))
    assert [r["variant"] for r in rows] == ["sch13d_h5"] and rows[0]["p_holm"] == rows[0]["p_raw"]   # Holm over books only
    s32 = extra[0]
    assert s32["p_group"] == pytest.approx(min(1.0, 2 * s32["p_raw"])) and "p_raw" not in extra[1]
    assert trial_total(rows, extra) == 1 + 2 + 1
    text = render(rows, None, "no panel", extra)
    assert "## Signal-level trials (2 in 1 tests" in text and "| s54_insider_all_event_sprt | S54 | — | L1051 |" in text
    assert "**Trial total: 4** = 1 book rows + 2 signal-level trials + 1 measurement(s)." in text


def test_the_repository_ledger_has_the_s54_rows():
    import os
    from hedge_fund.validation.family_log import REPORTS, extra_rows
    extra = {r["variant"]: r for r in extra_rows(REPORTS)}
    assert extra["s54_insider_all_event_sprt"]["family"] == "measurement" and extra["s54_insider_all_event_sprt"]["best_t"] is None
    assert all(r.get("source", "").startswith("AGENT_PLAN.md L") for r in extra.values())
    lines = [json.loads(l) for l in open(os.path.join(REPORTS, "family_log.jsonl"))]
    assert "composite_long_fpi" in {r["variant"] for r in lines}
    assert all("p_holm" in r and "family" not in r for r in lines)       # the Holm family only: its lines are counted as variants
    assert "| s54_insider_all_event_sprt | S54 |" in open(os.path.join(REPORTS, "family_log.md")).read()
