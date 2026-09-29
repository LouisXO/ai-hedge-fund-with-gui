"""S48: the ledger's de-duplication, S30 rows, pre-audit flags, and the deflated Sharpe."""
from __future__ import annotations

import json
import math

import numpy as np

from hedge_fund.validation.family_log import (collect, deflated_sharpe, expected_max_t, holm, render)


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
