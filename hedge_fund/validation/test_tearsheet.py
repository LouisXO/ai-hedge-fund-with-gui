"""Self-tests for the evaluation tools: a planted signal must be found, noise must not."""
import numpy as np
import pandas as pd
import pytest

from hedge_fund.validation import stats
from hedge_fund.validation.tearsheet import (forward_returns, noise_signal, permutation_p, planted_signal,
                                             rank_ic, summarize_ic, tail_returns, tearsheet)


def make_panel(n_days=320, n_names=150, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    cols = [f"T{i:03d}" for i in range(n_names)]
    rets = rng.normal(0.0003, 0.02, (n_days, n_names))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    mask = pd.DataFrame(True, index=idx, columns=cols)
    return close, opn, mask


def test_forward_returns_open_entry_uses_next_open_and_close_h():
    close, opn, _ = make_panel(30, 5)
    fwd = forward_returns(close, opn, 3, "open")
    t = close.index[10]
    expected = close.iloc[13, 0] / opn.iloc[11, 0] - 1
    assert fwd.loc[t].iloc[0] == pytest.approx(expected)


def test_rank_ic_is_one_for_identical_ranks():
    close, opn, _ = make_panel(40, 50)
    fwd = forward_returns(close, opn, 1)
    ic = rank_ic(fwd, fwd, min_names=10)
    assert np.allclose(ic.to_numpy(), 1.0)


def test_planted_signal_is_detected():
    close, opn, mask = make_panel()
    fwd = forward_returns(close, opn, 5).where(mask)
    sig = planted_signal(fwd, rho=0.05, seed=1)
    ic = rank_ic(sig, fwd)
    s = summarize_ic(ic, 5, n_boot=500)
    assert s["mean"] > 0.03
    assert s["t_nw"] > 2.5
    assert s["boot_ci95"][0] > 0
    assert permutation_p(sig, fwd, 5, n_perm=100)["p"] < 0.05


def test_noise_is_not_significant_in_most_seeds():
    close, opn, mask = make_panel()
    fwd = forward_returns(close, opn, 5).where(mask)
    rejections = 0
    for seed in range(20):
        ic = rank_ic(noise_signal(fwd, seed=seed), fwd)
        if abs(stats.newey_west_t(ic.to_numpy(), 5)) > 1.96:
            rejections += 1
    assert rejections <= 3   # ~5% expected; allow sampling slack


def test_newey_west_matches_iid_t_when_lag_zero():
    x = np.random.default_rng(0).normal(0.1, 1, 500)
    t_iid = x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))
    assert stats.newey_west_t(x, 0) == pytest.approx(t_iid)


def test_nw_t_shrinks_for_autocorrelated_series():
    rng = np.random.default_rng(1)
    e = rng.normal(0, 1, 3000)
    x = np.convolve(e, np.ones(10) / 10, mode="valid") + 0.02   # MA(9): overlapping-label shape
    assert abs(stats.newey_west_t(x, 10)) < abs(stats.newey_west_t(x, 0))


def test_block_length_grows_with_dependence():
    rng = np.random.default_rng(2)
    iid = rng.normal(size=2000)
    ma = np.convolve(rng.normal(size=2020), np.ones(20) / 20, mode="valid")
    assert stats.optimal_block_length(ma) > stats.optimal_block_length(iid)


def test_bootstrap_ci_covers_true_mean():
    x = np.random.default_rng(4).normal(0.05, 1, 1500)
    ci = stats.bootstrap_ci(x, n_boot=1000)
    assert ci["lo"] < 0.05 < ci["hi"]


def test_tail_returns_pick_extremes():
    idx = pd.bdate_range("2021-01-01", periods=3)
    sig = pd.DataFrame([[1, 2, 3, 4]] * 3, index=idx, columns=list("ABCD"), dtype=float)
    fwd = pd.DataFrame([[0.01, 0.02, 0.03, 0.04]] * 3, index=idx, columns=list("ABCD"))
    t = tail_returns(sig, fwd, k=1)
    assert t["top_mean"] == pytest.approx(0.04) and t["bottom_mean"] == pytest.approx(0.01)


def test_tearsheet_runs_end_to_end_and_respects_mask():
    close, opn, mask = make_panel(300, 80)
    mask.iloc[:, :40] = False
    sig = close.pct_change(5)
    ts = tearsheet(sig, close, opn, mask, horizons=(1, 5), n_perm=20, n_boot=200)
    assert ts["names_per_date_median"] == 40
    assert set(ts["horizons"]) == {1, 5}
