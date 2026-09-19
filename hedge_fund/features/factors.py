"""Vectorized price factors over a wide (date x ticker) adjusted-close frame.

Each returns a frame aligned to the input: the value on row t uses only
rows <= t. Membership masking and standardization happen downstream.
"""
from __future__ import annotations

import pandas as pd


def mom_12_1(adj_close: pd.DataFrame, min_obs: int = 230) -> pd.DataFrame:
    """12-1 momentum: return from t-252 to t-21 (skips the reversal month)."""
    out = adj_close.shift(21) / adj_close.shift(252) - 1
    enough = adj_close.notna().rolling(252, min_periods=1).sum() >= min_obs
    return out.where(enough)


def reversal_5(adj_close: pd.DataFrame) -> pd.DataFrame:
    """Raw 5-day reversal: minus the last 5-day return."""
    return -(adj_close / adj_close.shift(5) - 1)
