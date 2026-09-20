"""yfinance daily-bar backfill into panel.db (research history only).

Live signals and spot come from moomoo; yfinance is the free source for the
multi-year panel. Fetch once and keep it: 2025-26 Yahoo bans clients that
re-download large histories, so there is no weekly full refresh — only
`--incremental` for recent days.

Each membership symbol is fetched under its Yahoo symbol (agent.universe.
data_ticker) and clipped to [first membership start - LOOKBACK_DAYS,
last membership end + TAIL_DAYS], so a reused symbol cannot contribute an
unrelated company's prices. Every symbol gets a fetch_log row
(ok / partial / missing / reused) — the coverage report reads it.

Usage:
  python -m agent.backfill bars [--start 2013-01-01] [--chunk 50]
  python -m agent.backfill bars --incremental
  python -m agent.backfill index
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import pandas as pd
import yfinance as yf

from agent.universe import INDEX, Interval, data_ticker, intervals
from hedge_fund.features.panel import PanelStore

LOOKBACK_DAYS = 400   # 12-1 momentum needs 252 trading days before the first member date
TAIL_DAYS = 45        # forward returns up to 20 trading days after the last member date
INDEX_SYMBOLS = ("SPY", "^VIX", "IWM", "QQQ")
SOURCE = "yfinance"


def _windows(ivs: list[Interval], start: pd.Timestamp, today: pd.Timestamp) -> dict[str, tuple]:
    by: dict[str, list[Interval]] = {}
    for iv in ivs:
        if iv.end is None or iv.end >= start:
            by.setdefault(iv.ticker, []).append(iv)
    out = {}
    for t, lst in by.items():
        lo = min(iv.start for iv in lst) - pd.Timedelta(days=LOOKBACK_DAYS)
        hi = max((iv.end or today) for iv in lst) + pd.Timedelta(days=TAIL_DAYS)
        first_member = min(iv.start for iv in lst)
        last_member = max((iv.end or today) for iv in lst)
        out[t] = (max(lo, start - pd.Timedelta(days=LOOKBACK_DAYS)), min(hi, today), first_member, last_member)
    return out


def _download(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    for attempt in range(4):
        try:
            return yf.download(symbols, start=start, end=end, auto_adjust=False, actions=False,
                               group_by="ticker", threads=True, progress=False)
        except Exception as exc:  # network / throttling
            wait = 15 * (attempt + 1)
            print(f"  download error ({exc}); retry in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"yfinance failed for {symbols[:3]}...")


def _frame(raw: pd.DataFrame, sym: str) -> pd.DataFrame:
    try:
        sub = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
    except KeyError:
        return pd.DataFrame()
    sub = sub.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                              "Adj Close": "adj_close", "Volume": "volume"})
    sub = sub.dropna(subset=["close", "adj_close"])
    sub = sub[sub["close"] > 0]
    return sub[["open", "high", "low", "close", "adj_close", "volume"]]


def backfill_bars(store: PanelStore, start: str = "2014-01-01", chunk: int = 50,
                  incremental: bool = False, pause: float = 2.0) -> dict:
    today = pd.Timestamp(dt.date.today())
    ivs = intervals(store.membership_changes(INDEX))
    wins = _windows(ivs, pd.Timestamp(start), today)
    members = sorted(wins)
    if incremental:
        dl_start = (today - pd.Timedelta(days=10)).date().isoformat()
    else:
        dl_start = (pd.Timestamp(start) - pd.Timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    dl_end = (today + pd.Timedelta(days=1)).date().isoformat()

    stats = {"ok": 0, "partial": 0, "missing": 0, "reused": 0, "rows": 0}
    now = pd.Timestamp.now()
    for i in range(0, len(members), chunk):
        batch = members[i:i + chunk]
        syms = sorted({data_ticker(t) for t in batch})
        raw = _download(syms, dl_start, dl_end)
        frames, logs = [], []
        for t in batch:
            sym = data_ticker(t)
            lo, hi, first_member, last_member = wins[t]
            sub = _frame(raw, sym)
            sub = sub[(sub.index >= lo) & (sub.index <= hi)] if not sub.empty else sub
            status, note = "ok", None
            if sub.empty:
                full = _frame(raw, sym)
                status = "reused" if (not full.empty and full.index.min() > last_member) else "missing"
                note = f"yahoo has {len(full)} rows from {full.index.min().date()}" if not full.empty else None
            elif not incremental and sub.index.min() > max(first_member, lo) + pd.Timedelta(days=30):
                status, note = "partial", f"first bar {sub.index.min().date()} > needed from {max(first_member, lo).date()}"
            stats[status] += 1
            if not sub.empty:
                df = sub.reset_index().rename(columns={"Date": "trade_date"})
                df.insert(0, "ticker", t)
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
                df["source"] = SOURCE
                df["fetched_at"] = now
                frames.append(df)
            logs.append({"ticker": t, "data_ticker": sym, "source": SOURCE, "status": status,
                         "n_rows": int(len(sub)),
                         "first_date": sub.index.min().date() if not sub.empty else None,
                         "last_date": sub.index.max().date() if not sub.empty else None,
                         "note": note, "fetched_at": now})
        if frames:
            stats["rows"] += store.upsert_bars(pd.concat(frames, ignore_index=True))
        if not incremental:
            store.log_fetch(logs)
        print(f"  [{min(i + chunk, len(members))}/{len(members)}] rows {stats['rows']} "
              f"ok {stats['ok']} partial {stats['partial']} missing {stats['missing']} reused {stats['reused']}",
              flush=True)
        time.sleep(pause)
    return stats


def backfill_index(store: PanelStore, start: str = "2012-01-01") -> int:
    raw = _download(list(INDEX_SYMBOLS), start, (dt.date.today() + dt.timedelta(days=1)).isoformat())
    frames = []
    for sym in INDEX_SYMBOLS:
        sub = _frame(raw, sym).reset_index().rename(columns={"Date": "trade_date"})
        sub.insert(0, "symbol", sym)
        sub["trade_date"] = pd.to_datetime(sub["trade_date"]).dt.date
        sub["source"] = SOURCE
        sub["fetched_at"] = pd.Timestamp.now()
        frames.append(sub[["symbol", "trade_date", "open", "high", "low", "close", "adj_close",
                           "source", "fetched_at"]])
    return store.upsert_index(pd.concat(frames, ignore_index=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["bars", "index"])
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--incremental", action="store_true")
    args = ap.parse_args()
    with PanelStore() as store:
        if args.cmd == "bars":
            print(backfill_bars(store, args.start, args.chunk, args.incremental))
        else:
            print({"index_rows": backfill_index(store)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
