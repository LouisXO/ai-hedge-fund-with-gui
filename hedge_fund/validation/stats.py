"""Inference helpers for daily IC series — numpy only.

Daily IC at horizon h overlaps h-1 days with its neighbours and inherits the
signal's own persistence, so i.i.d. tests overstate significance. Two tools
handle that here:

- newey_west_t: HAC t-stat of the mean with a Bartlett kernel.
- stationary_bootstrap: Politis & Romano (1994) resampling with the
  Politis & White (2004) automatic block length (Patton, Politis & White
  2009 correction), instead of a hand-picked block.
"""
from __future__ import annotations

import math

import numpy as np


def _autocov(x: np.ndarray, k: int) -> float:
    n = len(x)
    return float(np.dot(x[: n - k], x[k:]) / n)


def newey_west_t(x: np.ndarray, lag: int) -> float:
    """t-stat of mean(x) with Bartlett-weighted autocovariances up to `lag`."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return float("nan")
    d = x - x.mean()
    var = _autocov(d, 0)
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * _autocov(d, k)
    if var <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(var / n))


def optimal_block_length(x: np.ndarray) -> float:
    """Politis-White automatic block length for the stationary bootstrap."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return 1.0
    d = x - x.mean()
    kn = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    mmax = int(math.ceil(math.sqrt(n))) + kn
    bmax = math.ceil(min(3 * math.sqrt(n), n / 3))
    c = 2.0
    g0 = _autocov(d, 0)
    if g0 <= 0:
        return 1.0
    rho = np.array([_autocov(d, k) / g0 for k in range(1, mmax + kn + 1)])
    crit = c * math.sqrt(math.log10(n) / n)
    mhat = mmax
    for m in range(1, mmax + 1):
        if np.all(np.abs(rho[m - 1 + 1: m - 1 + 1 + kn]) < crit):
            mhat = m
            break
    M = min(2 * mhat, mmax)

    def lam(t: float) -> float:
        t = abs(t)
        return 1.0 if t <= 0.5 else (2 * (1 - t) if t <= 1 else 0.0)

    g_hat = 0.0
    spec = g0
    for k in range(1, M + 1):
        gk = _autocov(d, k)
        w = lam(k / M)
        g_hat += 2 * w * k * gk
        spec += 2 * w * gk
    d_sb = 2 * spec ** 2
    if d_sb <= 0 or g_hat == 0:
        return 1.0
    b = (2 * g_hat ** 2 / d_sb) ** (1 / 3) * n ** (1 / 3)
    return float(min(max(b, 1.0), bmax))


def stationary_bootstrap_means(x: np.ndarray, n_boot: int = 2000, block: float | None = None,
                               seed: int = 7) -> np.ndarray:
    """Bootstrap distribution of mean(x) under the stationary bootstrap."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return np.array([])
    b = block or optimal_block_length(x)
    p = 1.0 / b
    rng = np.random.default_rng(seed)
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_boot)
    jump = rng.random((n_boot, n)) < p
    starts = rng.integers(0, n, (n_boot, n))
    for t in range(1, n):
        idx[:, t] = np.where(jump[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return x[idx].mean(axis=1)


def bootstrap_ci(x: np.ndarray, n_boot: int = 2000, seed: int = 7, alpha: float = 0.05) -> dict:
    means = stationary_bootstrap_means(x, n_boot=n_boot, seed=seed)
    if len(means) == 0:
        return {"lo": float("nan"), "hi": float("nan"), "block": float("nan"), "p_two_sided": float("nan")}
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    m = float(np.nanmean(x))
    # share of bootstrap means on the other side of zero, centred on the estimate
    centred = means - means.mean()
    p = float((np.abs(centred) >= abs(m)).mean())
    return {"lo": float(lo), "hi": float(hi), "block": optimal_block_length(x), "p_two_sided": p}
