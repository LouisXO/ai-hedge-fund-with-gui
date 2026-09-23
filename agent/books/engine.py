"""Daily portfolio simulator with explicit costs, and the yardstick against SPY.

A book supplies, per trading day, the list of names it wants to be holding
tomorrow (target set). The engine fills the difference at the next open,
charges `exec_frac x quoted spread` on every entry and exit, marks to
adjusted closes, and records a daily NAV. Equal dollar weight across at
most `max_positions` slots; unused slots sit in cash (never redistributed,
the same discipline as hedge_fund/risk/limits.py).

Metrics: CAGR, vol, Sharpe, max drawdown, turnover, and — the part that
answers "did it beat the market" — a daily regression of book returns on
SPY returns: annualized alpha with a Newey-West t, and beta. A long-only
book with beta 1 and alpha 0 has not beaten anything; it has held the
market with extra steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from agent.books.data import Market
from hedge_fund.validation.stats import bootstrap_ci, newey_west_t

TRADING_DAYS = 252


@dataclass
class Position:
    ticker: str
    shares: float
    entry_day: pd.Timestamp
    entry_px: float                      # includes the entry cost
    entry_cost_pct: float
    hold_until: pd.Timestamp | None      # None = until the book drops it
    high: float = 0.0                    # highest mark since entry (stop rule)
    stopped: bool = False                # flagged at a mark, sold at the next open


@dataclass
class Trade:
    ticker: str
    entry_day: pd.Timestamp
    exit_day: pd.Timestamp
    ret_pct: float                       # net of both spreads
    cost_pct: float


@dataclass
class Result:
    nav: pd.Series
    spy_nav: pd.Series
    trades: list[Trade]
    exposure: pd.Series                  # share of NAV invested each day
    turnover: float
    metrics: dict = field(default_factory=dict)
    size_factor: pd.Series | None = None  # daily IWM - SPY return, for the two-factor alpha


def _mark(market: Market, p: Position, day: pd.Timestamp) -> float:
    v = market.adj.at[day, p.ticker]
    if pd.notna(v):
        return float(v)
    last = market.adj[p.ticker].loc[:day].last_valid_index()
    return float(market.adj.at[last, p.ticker]) if last is not None else p.entry_px


def simulate(market: Market, targets: dict[pd.Timestamp, list[str]], start: str, end: str,
             max_positions: int, hold_days: int | None, exec_frac: float, capital: float = 100_000.0,
             cash_in_spy: bool = False, sizes: dict[pd.Timestamp, dict[str, float]] | None = None,
             stop_pct: float | None = None, fixed_cost_pct: float | None = None) -> Result:
    """targets[day] = names the book wants to hold from the NEXT open, chosen with data through `day`.

    sizes[day][ticker] = fraction of equity to put in a new position (default 1/max_positions,
    equal slots). stop_pct: sell at the next open once a position has fallen stop_pct % from
    its highest close since entry (S24 construction variants; None = the v1 rule, no stops).
    fixed_cost_pct: if given, every side costs this % of price instead of exec_frac x quoted
    spread — the measured paper execution cost (S27).
    """
    days = market.adj.loc[start:end].index
    cash, positions = capital, {}
    nav, expo, trades = [], [], []
    traded_value = 0.0
    prev_day = None

    for day in days:
        # 1) exits at today's open: expired holds, or names the book no longer wants
        wanted = set(targets.get(prev_day, [])) if prev_day is not None else set()
        for t in list(positions):
            p = positions[t]
            expired = (p.hold_until is not None and day >= p.hold_until) or p.stopped
            dropped = p.hold_until is None and t not in wanted and prev_day is not None and prev_day in targets
            px = market.adj_open.at[day, t]
            if pd.isna(px):
                # no open today: last valid close (a delisting settles at its final print)
                last = market.adj[t].loc[:day].last_valid_index()
                px = float(market.adj.at[last, t]) if last is not None else p.entry_px
                dead = pd.isna(market.adj.loc[day:, t]).all()
                if not (expired or dropped or dead):
                    continue
            if expired or dropped or pd.isna(market.adj_open.at[day, t]):
                cost = (fixed_cost_pct if fixed_cost_pct is not None else exec_frac * market.spread_pct(t, day)) / 100
                proceeds = p.shares * px * (1 - cost)
                cash += proceeds
                traded_value += p.shares * px
                trades.append(Trade(t, p.entry_day, day, (px * (1 - cost) / p.entry_px - 1) * 100,
                                    p.entry_cost_pct + cost * 100))
                del positions[t]
        # 2) entries at today's open, from yesterday's target list
        if prev_day is not None and prev_day in targets:
            equity = cash + sum(p.shares * _mark(market, p, prev_day) for p in positions.values())
            slot = equity / max_positions
            day_sizes = sizes.get(prev_day, {}) if sizes else {}
            for t in targets[prev_day]:
                if len(positions) >= max_positions or t in positions:
                    continue
                px = market.adj_open.at[day, t] if t in market.adj_open.columns else np.nan
                want = equity * day_sizes[t] if t in day_sizes else slot
                if want <= 0 or pd.isna(px) or px <= 0 or cash < want * 0.5:
                    continue                                   # size 0 = keep if held, never enter (crash regime)
                cost = (fixed_cost_pct if fixed_cost_pct is not None else exec_frac * market.spread_pct(t, day)) / 100
                spend = min(want, cash)
                shares = spend / (px * (1 + cost))
                cash -= spend
                traded_value += spend
                hold_until = None
                if hold_days is not None:
                    pos = days.get_loc(day)
                    hold_until = days[min(pos + hold_days, len(days) - 1)]
                positions[t] = Position(t, shares, day, px * (1 + cost), cost * 100, hold_until, high=px)
        # 3) mark (and the stop rule: flag now, sell at the next open)
        for p in positions.values():
            m = _mark(market, p, day)
            p.high = max(p.high, m)
            if stop_pct is not None and p.high > 0 and m <= p.high * (1 - stop_pct / 100):
                p.stopped = True
        value = cash + sum(p.shares * _mark(market, p, day) for p in positions.values())
        if cash_in_spy and prev_day is not None:
            # idle cash earns the index: removes cash drag from the comparison with SPY
            cash *= float(market.spy.at[day] / market.spy.at[prev_day]) if pd.notna(market.spy.at[prev_day]) else 1.0
            value = cash + sum(p.shares * _mark(market, p, day) for p in positions.values())
        nav.append(value)
        expo.append((value - cash) / value if value > 0 else 0.0)
        prev_day = day

    nav = pd.Series(nav, index=days, name="nav")
    spy = market.spy.reindex(days).ffill()
    spy_nav = spy / spy.iloc[0] * capital
    years = max((days[-1] - days[0]).days / 365.25, 1e-9)
    res = Result(nav, spy_nav, trades, pd.Series(expo, index=days), traded_value / (capital * years) / 2)
    if market.iwm is not None:
        iwm = market.iwm.reindex(days).ffill()
        res.size_factor = iwm.pct_change() - spy.pct_change()
    res.metrics = metrics(res, years)
    return res


def metrics(res: Result, years: float) -> dict:
    r = res.nav.pct_change().dropna()
    m = res.spy_nav.pct_change().reindex(r.index).fillna(0)
    total = res.nav.iloc[-1] / res.nav.iloc[0] - 1
    spy_total = res.spy_nav.iloc[-1] / res.spy_nav.iloc[0] - 1
    dd = (res.nav / res.nav.cummax() - 1).min()
    # alpha/beta vs SPY, daily, then annualized; NW t on the residual mean
    X = np.column_stack([np.ones(len(r)), m.to_numpy()])
    beta_hat = np.linalg.lstsq(X, r.to_numpy(), rcond=None)[0]
    resid = r.to_numpy() - X @ beta_hat
    # two-factor version: SPY + size (IWM - SPY). A small/mid book's "alpha vs SPY" is
    # mostly its size exposure; this is the number that survives that objection.
    two = {}
    if res.size_factor is not None:
        f = res.size_factor.reindex(r.index).fillna(0).to_numpy()
        X2 = np.column_stack([np.ones(len(r)), m.to_numpy(), f])
        b2 = np.linalg.lstsq(X2, r.to_numpy(), rcond=None)[0]
        resid2 = r.to_numpy() - X2 @ b2
        two = {"alpha2_ann_pct": float(b2[0] * TRADING_DAYS * 100), "alpha2_t_nw": newey_west_t(resid2 + b2[0], lag=5),
               "beta_mkt": float(b2[1]), "beta_size": float(b2[2])}
    active = (r - m).to_numpy()
    boot = bootstrap_ci(active, n_boot=2000)
    wins = [t.ret_pct for t in res.trades]
    return {
        "years": round(years, 2), "total_return_pct": total * 100, "cagr_pct": ((1 + total) ** (1 / years) - 1) * 100,
        "spy_total_pct": spy_total * 100, "spy_cagr_pct": ((1 + spy_total) ** (1 / years) - 1) * 100,
        "excess_cagr_pct": (((1 + total) ** (1 / years)) - ((1 + spy_total) ** (1 / years))) * 100,
        "vol_pct": r.std() * np.sqrt(TRADING_DAYS) * 100,
        "sharpe": r.mean() / r.std() * np.sqrt(TRADING_DAYS) if r.std() > 0 else float("nan"),
        "max_drawdown_pct": dd * 100, "spy_max_drawdown_pct": (res.spy_nav / res.spy_nav.cummax() - 1).min() * 100,
        **two,
        "beta": float(beta_hat[1]), "alpha_ann_pct": float(beta_hat[0] * TRADING_DAYS * 100),
        "alpha_t_nw": newey_west_t(resid + beta_hat[0], lag=5),
        "active_ann_pct": float(active.mean() * TRADING_DAYS * 100),
        "active_t_nw": newey_west_t(active, lag=5), "active_ci95_daily_bp": [boot["lo"] * 1e4, boot["hi"] * 1e4],
        "avg_exposure": float(res.exposure.mean()), "turnover_ann": res.turnover,
        "n_trades": len(res.trades), "trade_hit_rate": float(np.mean([w > 0 for w in wins])) if wins else float("nan"),
        "avg_trade_ret_pct": float(np.mean(wins)) if wins else float("nan"),
        "by_year": {int(y): round(float(v), 4) for y, v in
                    (res.nav.groupby(res.nav.index.year).last() / res.nav.groupby(res.nav.index.year).first() - 1).items()},
        "spy_by_year": {int(y): round(float(v), 4) for y, v in
                        (res.spy_nav.groupby(res.spy_nav.index.year).last()
                         / res.spy_nav.groupby(res.spy_nav.index.year).first() - 1).items()},
    }
