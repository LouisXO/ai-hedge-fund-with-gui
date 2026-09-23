"""Standard cross-sectional factors on the point-in-time panel.

Everything is computed as of a rebalance day D from what was filed on or
before D. Definitions follow the academic conventions, not tuned variants:

  value      book/market (equity / market cap), earnings yield (NI_ttm / mcap)
  quality    gross profitability (GP_ttm / assets), ROE (NI_ttm / equity),
             accruals = (NI_ttm - CFO_ttm) / assets (lower is better),
             asset growth (lower is better)
  momentum   12-1 month return
  low vol    minus trailing 252-day realized vol

Each raw score is winsorized at 1/99% and z-scored across the day's
universe; a family score is the mean of its members; the composite is the
equal-weight mean of the four families. Nothing is fitted to returns.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.data import Market


def _z(s: pd.Series) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan)
    if s.notna().sum() < 20:
        return s * np.nan
    lo, hi = s.quantile(0.01), s.quantile(0.99)
    s = s.clip(lo, hi)
    return (s - s.mean()) / (s.std() or np.nan)


def _rank_z(s: pd.Series) -> pd.Series:
    """Rank -> inverse-normal score (van der Waerden). Bounded, no pile-up at a winsor cap, and every
    family gets the same scale, so a heavy-tailed family (momentum) cannot outvote a compressed one
    (low vol). S24 variant `rankz`, pre-registered 2026-09-22 after the audit showed 26 names at the
    momentum cap and low vol's z never above 1.03."""
    from scipy.stats import norm
    s = s.replace([np.inf, -np.inf], np.nan)
    if s.notna().sum() < 20:
        return s * np.nan
    r = s.rank(method="average")
    n = s.notna().sum()
    return pd.Series(norm.ppf((r - 0.5) / n), index=s.index)


def latest_before(fund: pd.DataFrame, day: pd.Timestamp, max_age_days: int = 200) -> pd.DataFrame:
    """Most recent fundamentals row per ticker filed on or before `day`, not stale."""
    f = fund[(fund["filed"] <= day) & (fund["filed"] >= day - pd.Timedelta(days=max_age_days))]
    return f.sort_values("filed").drop_duplicates("ticker", keep="last").set_index("ticker")


def _z_by_group(out: pd.DataFrame, groups: pd.Series, min_n: int = 15) -> pd.DataFrame:
    """z within industry group; groups too small fall back to the global z (S24 sector-neutral variant)."""
    g = groups.reindex(out.index).fillna("Other")
    glob = out.apply(_z)
    parts = []
    for name, idx in out.groupby(g).groups.items():
        sub = out.loc[idx]
        parts.append(sub.apply(_z) if len(sub) >= min_n else glob.loc[idx])
    return pd.concat(parts).reindex(out.index)


def factor_scores(market: Market, fund: pd.DataFrame, day: pd.Timestamp, universe: pd.Index,
                  groups: pd.Series | None = None, issuance: bool = False, drop_momentum: bool = False,
                  norm: str = "winsor_z", shares_override: pd.Series | None = None) -> pd.DataFrame:
    """v1 when called with defaults. S24 options: `groups` = sector-neutral z-scores; `issuance` adds
    net share issuance (lower is better) to the quality family; `drop_momentum` = momentum-crash
    filter (composite is the mean of the other three families that day)."""
    f = latest_before(fund, day).reindex(universe)
    raw_px = market.close.loc[day].reindex(universe)
    shares = f["shares"]
    if shares_override is not None:                      # names whose XBRL share count is missing/per-class (V, BRK.B)
        shares = shares.fillna(shares_override.reindex(universe))
    mcap = raw_px * shares
    # Ratios need sane denominators. Negative or near-zero equity turns ROE and B/M into
    # nonsense (a loss over negative equity is a large positive "ROE" — S16's quality book
    # was 60-80% such names), and tiny asset bases explode accruals and GP/A.
    ok_equity = f["equity"] > 0.05 * f["assets"]
    ok_assets = f["assets"] > 1e7
    ok_mcap = mcap > 1e8
    f = f.where(ok_equity & ok_assets & ok_mcap.reindex(f.index).fillna(False))
    mcap = mcap.where(ok_mcap)
    out = pd.DataFrame(index=universe)
    # value
    out["bm"] = f["equity"] / mcap
    out["ey"] = f["ni_ttm"] / mcap
    # quality
    gp = f["gp_ttm"].where(f["gp_ttm"].notna(), f["rev_ttm"] - f["cogs_ttm"])
    out["gpa"] = gp / f["assets"]
    out["roe"] = f["ni_ttm"] / f["equity"]
    out["accruals"] = -(f["ni_ttm"] - f["cfo_ttm"]) / f["assets"]
    out["asset_growth"] = -(f["assets"] / f["assets_1y"] - 1)
    if issuance:
        f1 = latest_before(fund, day - pd.Timedelta(days=365), max_age_days=200).reindex(universe)
        out["issuance"] = -(f["shares"] / f1["shares"] - 1)          # buybacks score high, dilution low
    # momentum, low vol
    hist = market.adj.loc[:day]
    out["mom"] = (hist.iloc[-22] / hist.iloc[-253] - 1).reindex(universe) if len(hist) > 253 else np.nan
    rets = np.log(hist.iloc[-253:] / hist.iloc[-253:].shift(1))
    out["lowvol"] = -rets.std().reindex(universe)
    zfun = _rank_z if norm == "rank" else _z
    z = _z_by_group(out, groups) if groups is not None else out.apply(zfun)
    qcols = ["gpa", "roe", "accruals", "asset_growth"] + (["issuance"] if issuance else [])
    fam = pd.DataFrame({"value": z[["bm", "ey"]].mean(axis=1),
                        "quality": z[qcols].mean(axis=1),
                        "momentum": z["mom"], "lowvol": z["lowvol"]})
    fams = ["value", "quality", "lowvol"] if drop_momentum else ["value", "quality", "momentum", "lowvol"]
    fam["composite"] = fam[fams].mean(axis=1)
    fam["n_families"] = fam[["value", "quality", "momentum", "lowvol"]].notna().sum(axis=1)
    fam["mcap"] = mcap
    return fam
