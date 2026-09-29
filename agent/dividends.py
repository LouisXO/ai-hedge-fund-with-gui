"""Dividends the paper lots were entitled to, read from the panel's adjustment factors (S48).

Why: the Alpaca paper account does not simulate dividends (its docs say so), while the backtests,
the rule replay (agent/drift.py) and the SPY / QQQ comparisons are all total return (adj_close).
Without this the long book's paper NAV runs about 3% a year behind its own yardstick (S45 D6).
Per lot, this records the cash dividends a real account holding the same shares would have been
paid; agent/auction_basis.py adds their running sum to agent_auction_nav, where the price NAV and
the total-return NAV are both kept. Dividends are NOT added to agent_books.cash_usd: the paper
account never receives that money, and plan_book would spend cash the broker does not have.
(A real account books its DIV activity as cash instead; that is the migration's job.)

Method. The panel stores the raw close in `close` and the split+dividend adjusted close in
`adj_close`, one adjustment basis per symbol (agent/sources/alpaca_bars.py re-fetches a symbol's
whole history when its basis breaks). The factor f = adj_close / close steps down going back
across an ex-date: f[t-1] / f[t] = 1 - D / close[t-1], so D = close[t-1] * (1 - f[t-1] / f[t]).
SPY / QQQ / IWM come from index_daily (yfinance, long history), everything else from bars.

Caveats, each handled here:
  - rounding: adj_close carries two decimals from $10 up, so f wobbles by up to half a tick per
    row with no event; a step inside FACTOR_TOL plus half a tick on each row is noise (the same
    tolerance as alpaca_bars.adjustment_breaks).
  - splits: a factor ratio beyond 1 / SPLIT_MIN .. 1 is not read as a dividend (a 2:1 split looks
    exactly like a 50% distribution, a reverse split like a negative one). It is recorded with
    kind 'split_or_special' and $0 for a person to check; a special distribution above 20% of the
    price lands there too.
  - basis breaks: a step between two rows that were not fetched together (different source, or
    fetched more than an hour apart) is an older row still on the basis of its own fetch day, not
    an ex-date; it is recorded as kind 'basis_break' and $0. The re-fetch that alpaca_bars makes
    the same evening turns the real ex-date into a step between rows of one fetch.
  - timing: the step exists once the panel holds the ex-date's bar and the re-fetched history (the
    13:25 job); every run recomputes the whole table, so a late or corrected step is picked up.
  - entitlement: bought before the ex-date and still held at the close before it,
    entry_day < ex_date <= exit_day (a lot sold at the ex-date's open was the holder of record).
  - amounts are gross (before withholding), counted on the ex-date as adj_close counts them, not
    on the pay date.

Usage: python -m agent.dividends     (also run by agent.auction_basis before it builds the NAV)
"""
from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd

from agent import ledger
from hedge_fund.features.panel import PanelStore

FACTOR_TOL = 0.0005          # as agent/sources/alpaca_bars.FACTOR_TOL
SPLIT_MIN = 1.25             # as alpaca_bars.SPLIT_MIN: a factor ratio this far from 1 is a split, not a dividend
SAME_FETCH = pd.Timedelta(hours=1)
INDEX_SYMBOLS = ("SPY", "QQQ", "IWM")
LOOKBACK_DAYS = 10           # price history loaded before the earliest entry (the step needs the row before)

DDL = """CREATE TABLE IF NOT EXISTS agent_dividends (
    lot_id VARCHAR, book VARCHAR, ticker VARCHAR, ex_date DATE, per_share DOUBLE, qty DOUBLE, usd DOUBLE,
    kind VARCHAR, computed_at TIMESTAMP, PRIMARY KEY (lot_id, ex_date))"""


def _tick(px: np.ndarray) -> np.ndarray:
    """The increment the vendor rounded a price to: $0.01 with at most two decimals, $0.001 with three, else $0.0001."""
    p = np.asarray(px, dtype=float)
    return np.where(np.abs(p - p.round(2)) < 1e-9, 0.01, np.where(np.abs(p - p.round(3)) < 1e-9, 0.001, 0.0001))


def factor_events(px: pd.DataFrame) -> pd.DataFrame:
    """Ex-date steps of adj_close / close per ticker.

    px: ticker, trade_date, close, adj_close, source, fetched_at (one row per ticker and day).
    Returns ticker, ex_date, per_share, kind ('dividend' | 'split_or_special' | 'basis_break').
    """
    cols = ["ticker", "ex_date", "per_share", "kind"]
    if px.empty:
        return pd.DataFrame(columns=cols)
    df = px[(px["close"] > 0) & (px["adj_close"] > 0)].sort_values(["ticker", "trade_date"]).copy()
    df["f"] = df["adj_close"] / df["close"]
    g = df.groupby("ticker")
    df["prev_f"], df["prev_close"], df["prev_adj"] = g["f"].shift(), g["close"].shift(), g["adj_close"].shift()
    df["prev_source"], df["prev_fetched"] = g["source"].shift(), g["fetched_at"].shift()
    df = df.dropna(subset=["prev_f"])
    ratio = df["prev_f"] / df["f"]
    tol = FACTOR_TOL + _tick(df["adj_close"]) / 2 / df["adj_close"] + _tick(df["prev_adj"]) / 2 / df["prev_adj"]
    step = df[(ratio - 1).abs() > tol].copy()
    if step.empty:
        return pd.DataFrame(columns=cols)
    r = step["prev_f"] / step["f"]
    fetched = pd.to_datetime(step["fetched_at"])
    prev_fetched = pd.to_datetime(step["prev_fetched"])
    together = (step["source"] == step["prev_source"]) & ((fetched - prev_fetched).abs() <= SAME_FETCH)
    split = (r < 1 / SPLIT_MIN) | (r > 1)            # a dividend only ever lowers the earlier factor
    step["kind"] = np.where(~together, "basis_break", np.where(split, "split_or_special", "dividend"))
    step["per_share"] = np.where(step["kind"] == "dividend", (step["prev_close"] * (1 - r)).round(4), 0.0)
    step["ex_date"] = pd.to_datetime(step["trade_date"]).dt.date
    return step[cols].reset_index(drop=True)


def entitled(lots: pd.DataFrame, events: pd.DataFrame, computed_at: dt.datetime | None = None) -> pd.DataFrame:
    """One row per (lot, ex-date) the lot was holder of record for: entry_day < ex_date <= exit_day (or still open).

    lots: lot_id, book, ticker, qty, entry_day, exit_day (NaT / None while open).
    """
    cols = ["lot_id", "book", "ticker", "ex_date", "per_share", "qty", "usd", "kind", "computed_at"]
    if lots.empty or events.empty:
        return pd.DataFrame(columns=cols)
    m = lots.merge(events, on="ticker")
    entry = pd.to_datetime(m["entry_day"]).dt.date
    exit_ = [None if pd.isna(x) else pd.Timestamp(x).date() for x in m["exit_day"]]
    keep = [e < x and (out is None or x <= out) for e, x, out in zip(entry, m["ex_date"], exit_)]
    m = m[keep].copy()
    m["usd"] = (m["qty"].astype(float) * m["per_share"].astype(float)).round(2)
    m["computed_at"] = computed_at or dt.datetime.now()
    return m[cols].reset_index(drop=True)


def load_prices(store: PanelStore, tickers: list[str], since: dt.date) -> pd.DataFrame:
    """close / adj_close rows of `tickers` from `since`: index_daily for the ETFs it carries, bars for the rest."""
    idx = [t for t in tickers if t in INDEX_SYMBOLS]
    rest = [t for t in tickers if t not in INDEX_SYMBOLS]
    frames = []
    if rest:
        q = f"""SELECT ticker, trade_date, close, adj_close, source, fetched_at FROM bars
                WHERE ticker IN ({','.join('?' * len(rest))}) AND trade_date >= ?"""
        frames.append(store.con.execute(q, rest + [since]).df())
    if idx:
        q = f"""SELECT symbol AS ticker, trade_date, close, adj_close, source, fetched_at FROM index_daily
                WHERE symbol IN ({','.join('?' * len(idx))}) AND trade_date >= ?"""
        frames.append(store.con.execute(q, idx + [since]).df())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["ticker", "trade_date", "close", "adj_close", "source", "fetched_at"])


def compute(con, store: PanelStore) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(entitled rows for every paper lot, all factor steps seen on the lots' tickers)."""
    lots = con.execute("""SELECT l.lot_id, l.book, l.ticker, l.qty, l.entry_day, l.exit_day FROM agent_lots l
                          LEFT JOIN agent_orders o ON o.client_order_id = l.entry_order
                          WHERE coalesce(o.dry_run, FALSE) = FALSE""").df()
    if lots.empty:
        return entitled(lots, pd.DataFrame()), pd.DataFrame(columns=["ticker", "ex_date", "per_share", "kind"])
    since = pd.to_datetime(lots["entry_day"]).min().date() - dt.timedelta(days=LOOKBACK_DAYS)
    ev = factor_events(load_prices(store, sorted(set(lots["ticker"])), since))
    return entitled(lots, ev), ev


def update(con, store: PanelStore) -> pd.DataFrame:
    """Recompute agent_dividends from scratch (the factors can be corrected after the fact). Returns the rows."""
    rows, _ = compute(con, store)
    con.execute(DDL)
    con.execute("BEGIN TRANSACTION")               # delete + insert together: a failure keeps the previous rows
    try:
        con.execute("DELETE FROM agent_dividends")
        if len(rows):
            con.register("_div_in", rows)
            con.execute("INSERT INTO agent_dividends SELECT lot_id, book, ticker, ex_date, per_share, qty, usd, kind, computed_at FROM _div_in")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        if len(rows):
            try:
                con.unregister("_div_in")
            except Exception:
                pass
    return rows


def cumulative(rows: pd.DataFrame, days: list[dt.date], book: str) -> dict[dt.date, float]:
    """Dividends of `book` with ex_date <= day, for each day (only kind 'dividend' carries money)."""
    r = rows[(rows["book"] == book) & (rows["kind"] == "dividend")] if len(rows) else rows
    ex = [pd.Timestamp(x).date() for x in r["ex_date"]] if len(r) else []
    usd = list(r["usd"]) if len(r) else []
    return {d: float(sum(u for x, u in zip(ex, usd) if x <= d)) for d in days}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB, help="the ledger (a copy, for a test run)")
    args = ap.parse_args(argv)
    con = ledger.connect(args.db)
    try:
        with PanelStore(read_only=True) as store:
            rows = update(con, store)
    finally:
        con.close()
    paid = rows[rows["kind"] == "dividend"] if len(rows) else rows
    other = rows[rows["kind"] != "dividend"] if len(rows) else rows
    for b, g in (paid.groupby("book") if len(paid) else []):
        print(f"{b:8s} dividends ${g['usd'].sum():,.2f} over {len(g)} lot ex-dates: "
              + ", ".join(f"{t} {x} {p:.4f}" for t, x, p in zip(g["ticker"], g["ex_date"], g["per_share"])))
    if len(other):
        print("to check (not counted):", ", ".join(f"{t} {x} {k}" for t, x, k in zip(other["ticker"], other["ex_date"], other["kind"])))
    if not len(rows):
        print("dividends: no lot was holder of record on an ex-date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
