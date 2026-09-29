"""S48 (audit research #2): event_stats' t with overlapping h-day windows.

Events that come in runs (the same name flagged on consecutive days, as in the long book's veto
tests) make neighbouring h-day forward returns share h-1 days. With no true effect, the original
`t_nw` (lag max(h // 5, 1)) rejects far too often; `t_nonoverlap` stays near the nominal 5%, and
`t_nw_h` (lag h - 1) sits in between.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from agent import s8_insider, s11_insider_wide
from agent.s8_insider import event_stats, nonoverlap_positions, overlap_robust_t
from hedge_fund.validation.stats import newey_west_t

H = 20


def _null_world(seed: int, n_days: int = 2600, n_runs: int = 30, run_len: int = 10, h: int = H):
    """One stock, zero drift; events = n_runs runs of run_len consecutive trading days."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2017-01-02", periods=n_days)
    e = rng.normal(0, 2.0, n_days + h + 1)
    cs = np.concatenate([[0.0], np.cumsum(e)])
    fwd = cs[np.arange(n_days) + 1 + h] - cs[np.arange(n_days) + 1]      # % return over the next h days
    starts = np.sort(rng.choice(n_days - run_len - h, n_runs, replace=False))
    pos = np.unique(np.concatenate([np.arange(s, s + run_len) for s in starts]))
    return days, fwd, pos


def test_nonoverlap_positions_are_h_apart():
    pos = np.array([0, 1, 2, 19, 20, 21, 45, 46, 70])
    keep = nonoverlap_positions(pos, 20)
    assert list(pos[keep]) == [0, 20, 45, 70]
    assert all(b - a >= 20 for a, b in zip(pos[keep][:-1], pos[keep][1:]))


def test_old_t_is_inflated_new_ones_are_not():
    old, nw_h, no = [], [], []
    for seed in range(300):
        _, fwd, pos = _null_world(seed)
        x = fwd[pos]
        old.append(newey_west_t(x, lag=max(H // 5, 1)))          # what event_stats' t_nw computes
        r = overlap_robust_t(x, pos, H)
        nw_h.append(r["t_nw_h"])
        no.append(r["t_nonoverlap"])
    old, nw_h, no = map(np.asarray, (old, nw_h, no))
    rate = lambda t: float((np.abs(t) >= 2).mean())
    # the original t: sd ~1.7 and ~20% two-sided rejections where 5% is nominal
    assert old.std() > 1.4 and rate(old) > 0.15
    # non-overlapping subsample: an honest t
    assert 0.85 < no.std() < 1.2 and rate(no) < 0.09
    # lag h-1: much closer, not perfect with ~30 independent clusters (documented in the docstring)
    assert nw_h.std() < old.std() - 0.25 and rate(nw_h) < 0.6 * rate(old)


def _frames(seed: int):
    days, fwd, pos = _null_world(seed)
    fwd_df = pd.DataFrame({"AAA": fwd}, index=days)
    mkt = pd.Series(0.0, index=days)
    ev = pd.DataFrame({"date": days[pos], "ticker": "AAA"})
    return ev, fwd_df, mkt, pos


def test_event_stats_keeps_t_nw_and_adds_robust_fields():
    ev, fwd_df, mkt, pos = _frames(3)
    for fn in (s8_insider.event_stats, s11_insider_wide.event_stats):
        s = fn(ev, fwd_df, mkt, H, "sim")
        x = fwd_df["AAA"].to_numpy()[pos]
        assert s["n_dates"] == len(pos)
        assert math.isclose(s["t_nw"], newey_west_t(x, lag=4), rel_tol=1e-12)      # unchanged definition
        assert math.isclose(s["t_nw_h"], newey_west_t(x, lag=H - 1), rel_tol=1e-12)
        assert s["n_nonoverlap"] == len(nonoverlap_positions(pos, H))
        assert np.isfinite(s["t_nonoverlap"])
        assert abs(s["t_nw_h"]) <= abs(s["t_nw"]) * 1.05


def test_event_stats_signature_unchanged_for_callers():
    ev, fwd_df, mkt, _ = _frames(4)
    s = event_stats(ev, fwd_df, mkt, H, "sim")
    for k in ("label", "horizon", "n_events", "n_dates", "mean_abn_pct", "t_nw", "boot_ci95", "hit_rate",
              "share_years_positive", "first_half", "second_half"):
        assert k in s
