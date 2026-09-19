"""Signal tearsheet — Alphalens-style evaluation over a wide panel.

Inputs are wide frames (date x ticker): a signal, adjusted open/close
prices, and a boolean universe mask (point-in-time membership). The signal
on row t is known at the close of t; the trade is entered on day t+1.

What it reports, per horizon h (trading days):
- daily rank-IC series and its mean, Newey-West t (lag h), stationary
  bootstrap CI, non-overlapping ICIR (every h-th date), yearly means;
- (optional, n_perm > 0) whole-panel label permutation. NOT a gate: it
  tests "any association", and shuffling labels also removes the factor's
  time-varying payoff, so its null is far narrower than the real sampling
  error of mean IC (S1: null sd 0.003 vs NW standard error 0.010 for
  12-1 momentum). Significance of mean IC comes from the NW t and the
  stationary bootstrap, which do include that variation;
- quantile mean returns, raw and in excess of the cross-sectional mean;
- tail returns: raw (not demeaned) mean return of the top-k and bottom-k
  names — what an option buyer on those names is actually exposed to;
- top-quantile turnover.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hedge_fund.validation.stats import bootstrap_ci, newey_west_t


def forward_returns(adj_close: pd.DataFrame, adj_open: pd.DataFrame | None, h: int,
                    entry: str = "open") -> pd.DataFrame:
    """Return earned by a position opened on day t+1 and closed at the close of t+h.

    entry="open": adj_close[t+h] / adj_open[t+1] - 1 (the morning run trades
    after the open, so this is the nearest daily proxy). entry="close":
    adj_close[t+h] / adj_close[t] - 1.
    """
    if entry == "open":
        if adj_open is None:
            raise ValueError("entry='open' needs adj_open")
        return adj_close.shift(-h) / adj_open.shift(-1) - 1
    return adj_close.shift(-h) / adj_close - 1


def rank_ic(signal: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 30) -> pd.Series:
    """Per-date Spearman correlation over names where both are present."""
    both = signal.notna() & fwd.notna()
    s = signal.where(both).rank(axis=1)
    r = fwd.where(both).rank(axis=1)
    s = s.sub(s.mean(axis=1), axis=0)
    r = r.sub(r.mean(axis=1), axis=0)
    num = (s * r).sum(axis=1)
    den = np.sqrt((s ** 2).sum(axis=1) * (r ** 2).sum(axis=1))
    ic = num / den
    ic[both.sum(axis=1) < min_names] = np.nan
    return ic.dropna()


def permutation_p(signal: pd.DataFrame, fwd: pd.DataFrame, h: int, n_perm: int = 200,
                  seed: int = 7, min_names: int = 30) -> dict:
    """Whole-panel label permutation on non-overlapping dates (every h-th)."""
    rows = signal.index[::h]
    s0, f0 = signal.loc[rows], fwd.loc[rows]
    obs = rank_ic(s0, f0, min_names).mean()
    rng = np.random.default_rng(seed)
    cols = np.array(s0.columns)
    null = np.empty(n_perm)
    for i in range(n_perm):
        perm = s0.copy()
        perm.columns = rng.permutation(cols)
        null[i] = rank_ic(perm.reindex(columns=f0.columns), f0, min_names).mean()
    null = null[np.isfinite(null)]
    p = float((np.sum(np.abs(null) >= abs(obs)) + 1) / (len(null) + 1))
    return {"obs_nonoverlap": float(obs), "null_sd": float(null.std()), "p": p, "n_perm": int(len(null))}


def quantile_returns(signal: pd.DataFrame, fwd: pd.DataFrame, q: int = 5) -> dict:
    both = signal.notna() & fwd.notna()
    s, r = signal.where(both), fwd.where(both)
    pct = s.rank(axis=1, pct=True)
    bucket = np.ceil(pct * q).clip(1, q)
    excess = r.sub(r.mean(axis=1), axis=0)
    raw_m, ex_m = {}, {}
    for b in range(1, q + 1):
        sel = bucket == b
        raw_m[b] = float(r.where(sel).stack().groupby(level=0).mean().mean())
        ex_m[b] = float(excess.where(sel).stack().groupby(level=0).mean().mean())
    return {"raw": raw_m, "excess": ex_m, "spread_raw": raw_m[q] - raw_m[1]}


def tail_returns(signal: pd.DataFrame, fwd: pd.DataFrame, k: int = 5) -> dict:
    """Mean raw return of the k highest and k lowest names per date."""
    both = signal.notna() & fwd.notna()
    s, r = signal.where(both), fwd.where(both)
    rk_hi = s.rank(axis=1, ascending=False, method="first")
    rk_lo = s.rank(axis=1, ascending=True, method="first")
    top = r.where(rk_hi <= k).mean(axis=1).dropna()
    bot = r.where(rk_lo <= k).mean(axis=1).dropna()
    allm = r.mean(axis=1).dropna()
    return {"top_mean": float(top.mean()), "bottom_mean": float(bot.mean()), "all_mean": float(allm.mean()),
            "top_hit": float((top > 0).mean()), "bottom_hit_down": float((bot < 0).mean()),
            "n_dates": int(len(top))}


def top_turnover(signal: pd.DataFrame, mask: pd.DataFrame, q: int = 5) -> float:
    s = signal.where(mask)
    top = s.rank(axis=1, pct=True) > 1 - 1 / q
    prev = top.shift(1, fill_value=False)
    changed = (top & ~prev).sum(axis=1)
    size = top.sum(axis=1).replace(0, np.nan)
    return float((changed / size).iloc[1:].mean())


def summarize_ic(ic: pd.Series, h: int, n_boot: int = 2000) -> dict:
    x = ic.to_numpy()
    nonover = ic.iloc[::h]
    boot = bootstrap_ci(x, n_boot=n_boot)
    by_year = ic.groupby(ic.index.year).mean()
    return {
        "mean": float(ic.mean()), "std": float(ic.std()), "n_dates": int(len(ic)),
        "t_nw": newey_west_t(x, lag=h),
        "icir_nonoverlap": float(nonover.mean() / nonover.std()) if nonover.std() > 0 else float("nan"),
        "boot_ci95": [boot["lo"], boot["hi"]], "boot_block": boot["block"], "boot_p": boot["p_two_sided"],
        "hit_rate": float((ic > 0).mean()),
        "by_year": {int(y): round(float(v), 4) for y, v in by_year.items()},
        "share_years_positive": float((by_year > 0).mean()),
        "first_half": float(ic.iloc[: len(ic) // 2].mean()), "second_half": float(ic.iloc[len(ic) // 2:].mean()),
    }


def tearsheet(signal: pd.DataFrame, adj_close: pd.DataFrame, adj_open: pd.DataFrame, mask: pd.DataFrame,
              horizons=(1, 5, 10), entry: str = "open", k: int = 5, q: int = 5, n_perm: int = 0,
              n_boot: int = 2000, min_names: int = 30) -> dict:
    cols = signal.columns.intersection(adj_close.columns).intersection(mask.columns)
    idx = signal.index.intersection(adj_close.index).intersection(mask.index)
    sig = signal.loc[idx, cols].where(mask.loc[idx, cols])
    out = {"n_dates": int(sig.notna().any(axis=1).sum()),
           "names_per_date_median": float(sig.notna().sum(axis=1).replace(0, np.nan).median()),
           "turnover_top": top_turnover(sig, mask.loc[idx, cols], q), "horizons": {}}
    for h in horizons:
        fwd = forward_returns(adj_close.loc[idx, cols], adj_open.loc[idx, cols] if adj_open is not None else None,
                              h, entry).where(mask.loc[idx, cols])
        ic = rank_ic(sig, fwd, min_names)
        out["horizons"][h] = {
            "ic": summarize_ic(ic, h, n_boot),
            "quantiles": quantile_returns(sig, fwd, q),
            "tails": tail_returns(sig, fwd, k),
        }
        if n_perm:
            out["horizons"][h]["perm_diagnostic"] = permutation_p(sig, fwd, h, n_perm, min_names=min_names)
    return out


def planted_signal(fwd: pd.DataFrame, rho: float, seed: int = 7) -> pd.DataFrame:
    """Synthetic signal correlated rho with forward-return ranks (self-test)."""
    rng = np.random.default_rng(seed)
    z = fwd.rank(axis=1, pct=True)
    z = (z - z.mean(axis=1).to_numpy()[:, None]) / z.std(axis=1).to_numpy()[:, None]
    noise = rng.standard_normal(fwd.shape)
    out = rho * z.to_numpy() + np.sqrt(max(1 - rho ** 2, 0)) * noise
    return pd.DataFrame(out, index=fwd.index, columns=fwd.columns).where(fwd.notna())


def noise_signal(like: pd.DataFrame, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.standard_normal(like.shape), index=like.index, columns=like.columns).where(like.notna())
