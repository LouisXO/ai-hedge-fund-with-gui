"""Realized volatility estimators over wide OHLC panels (date x ticker).

Yang-Zhang is the default: it uses the overnight gap, the open-to-close
move and the Rogers-Satchell range term, so it is far less noisy than
close-to-close at the same window length — which matters when the number
is compared against an option's implied vol.

Inputs are split-adjusted OHLC (never dividend-adjusted closes mixed with
raw ranges). Outputs are annualized percentages, the same units as IV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def close_to_close(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    r = np.log(close / close.shift(1))
    return r.rolling(window).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100


def yang_zhang(open_: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
               window: int = 20) -> pd.DataFrame:
    """Yang-Zhang volatility, annualized %, over a rolling window of bars."""
    prev_close = close.shift(1)
    o = np.log(open_ / prev_close)        # overnight
    c = np.log(close / open_)             # open-to-close
    u = np.log(high / open_)
    d = np.log(low / open_)
    rs = u * (u - c) + d * (d - c)        # Rogers-Satchell
    var_o = o.rolling(window).var(ddof=1)
    var_c = c.rolling(window).var(ddof=1)
    mean_rs = rs.rolling(window).mean()
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    var = var_o + k * var_c + (1 - k) * mean_rs
    return np.sqrt(var.clip(lower=0) * TRADING_DAYS) * 100
