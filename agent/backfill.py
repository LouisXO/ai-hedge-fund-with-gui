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

index_daily (SPY / ^VIX / IWM / QQQ, and the factor ETFs) is downloaded whole every time, stops at yesterday
while the session is open (download_end: New York's date and clock), and goes through basis_guard before the upsert
(2026-10-21, S54 investigation): a response that leaves stored days out while its adjustment basis moved is not
written for that symbol, unless every day it leaves out lies between two days it has with the same basis change
(those stored days are rescaled to the new basis and written with it).

Usage:
  python -m agent.backfill bars [--start 2013-01-01] [--chunk 50]
  python -m agent.backfill bars --incremental
  python -m agent.backfill index
  python -m agent.backfill factors      the research ETFs (FACTOR_ETFS), every day from agent/bin/postclose.sh (S54)
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import time

import numpy as np
import pandas as pd
import yfinance as yf

from agent.universe import INDEX, Interval, data_ticker, intervals
from hedge_fund.features.panel import PanelStore

LOOKBACK_DAYS = 400   # 12-1 momentum needs 252 trading days before the first member date
TAIL_DAYS = 45        # forward returns up to 20 trading days after the last member date
INDEX_SYMBOLS = ("SPY", "^VIX", "IWM", "QQQ")
SOURCE = "yfinance"
# adj_close / close of one stored day may move by float noise between two downloads (~1e-7 at most); a distribution
# moves it by its yield (a tenth of a percent and more): beyond this, the response is on a new adjustment basis
BASIS_TOL = 1e-6


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


def session_closed(now: dt.datetime | None = None) -> bool:
    """True once today's US session is over (16:15 ET or later), or on a weekend."""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo("America/New_York"))
    return now.weekday() >= 5 or (now.hour, now.minute) >= (16, 15)


def download_end(now: dt.datetime | None = None) -> str:
    """yfinance's end (exclusive) for a daily download: tomorrow once today's session is over (session_closed), else
    today, so an open session's in-progress bar is never fetched. The date and the clock are both New York's
    (2026-10-21 review): from 21:00 PT the ET clock is past midnight while the PT date is still the session's, and
    "the PT date + 0" left out the session that had just closed (a late manual re-run of agent.execute after a failed
    16:10 download would not have fetched the day's SPY bar). The scheduled runs (08:41, 13:25, 16:10 PT) fall on the
    same date in both zones, so they ask for the same end as before."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = (now.astimezone(et) if now.tzinfo else now) if now is not None else dt.datetime.now(et)
    return (now.date() + dt.timedelta(days=1 if session_closed(now) else 0)).isoformat()


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
    # yfinance returns the in-progress session as a daily row while the market is open; a
    # half-day bar stored as a close corrupts every signal scored on it (seen 2026-09-22).
    # Before ~16:15 ET, stop at yesterday (end is exclusive); the after-close Alpaca update
    # supplies today's completed bar. New York's date and clock (download_end, 2026-10-21).
    dl_end = download_end()

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


def _days(ds: list) -> str:
    """Up to three days by name, then the last: '2026-10-14' or '2012-01-03, 2012-01-04, 2012-01-05 .. 2020-03-16'."""
    return ", ".join(map(str, ds)) if len(ds) <= 3 else f"{ds[0]}, {ds[1]}, {ds[2]} .. {ds[-1]}"


def basis_guard(store: PanelStore, df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """(the rows of `df` to upsert into index_daily, {symbol: why it was not written}, {symbol: what was rescaled}).

    Yahoo rescales Adj Close back to the start at every distribution, and the upsert replaces only the days a
    response has. A response that leaves stored days out (it starts after the stored first day, ends before the
    stored last one, or misses days in between: _frame drops rows with a NaN close) keeps those days on the stored
    basis; if its own basis moved (adj_close / close differs from the stored ratio by more than BASIS_TOL on a day
    both have), writing it would put a spurious return about the size of the distribution's yield at each junction.
    Such a symbol keeps all its stored rows (a missing day then shows in the drift alarm and the health page). A
    partial response on the stored basis is written; one that shares no day with the stored rows cannot be checked
    and is not written either.

    One case is repaired instead (2026-10-21 review): a stored day the response leaves out BETWEEN two days it has,
    when adj_close / close moved by the same factor (within BASIS_TOL) on the shared day before and the shared day after
    it. A new distribution moves every day before its ex-date by one factor, so equal factors on both sides mean no new
    ex-date in between, and that factor is the one the left-out day takes: its stored adj_close is rescaled by it and
    written with the response (its close and fetched_at stay as stored). Without this a day Yahoo stops returning (a
    row it now leaves NaN) would refuse the symbol at every download from its next distribution on, for good. A
    left-out day before the response's first day or after its last, or with an ex-date next to it, is still refused."""
    keep, refused, rescaled = [], {}, {}
    cols = ["symbol", "trade_date", "open", "high", "low", "close", "adj_close", "source", "fetched_at"]
    for sym, new in df.groupby("symbol", sort=False):
        old = store.con.execute(f"SELECT {', '.join(cols)} FROM index_daily WHERE symbol = ?", [sym]).df()
        old["trade_date"] = pd.to_datetime(old["trade_date"]).dt.date
        gone = sorted(set(old["trade_date"]) - set(new["trade_date"]))
        if not gone:                                   # every stored day is rewritten: no junction is left
            keep.append(new)
            continue
        both = new.merge(old[["trade_date", "close", "adj_close"]], on="trade_date", suffixes=("", "_stored")).sort_values("trade_date")
        ratio = ((both["adj_close"] / both["close"]) / (both["adj_close_stored"] / both["close_stored"])).to_numpy()
        moved = float(np.abs(ratio - 1).max()) if len(both) else None
        if moved is not None and moved <= BASIS_TOL:
            keep.append(new)
            continue
        shared = list(both["trade_date"])
        factor = {}                                    # left-out day -> the basis change on both sides of it
        for g in gone:
            k = bisect.bisect_left(shared, g)          # shared[k - 1] < g < shared[k]
            if 0 < k < len(shared) and abs(ratio[k] / ratio[k - 1] - 1) <= BASIS_TOL:
                factor[g] = float(ratio[k])
        if moved is not None and len(factor) == len(gone):
            fix = old[old["trade_date"].isin(list(factor))].copy()
            fix["adj_close"] = fix["adj_close"] * fix["trade_date"].map(factor)
            keep += [new, fix[cols]]
            f = sorted(factor.values())
            rescaled[sym] = (f"{len(gone)} stored day(s) the response left out ({_days(gone)}) lie between days it has: their "
                             f"adj_close rescaled to its basis (x{f[0]:.6f}" + (f" .. x{f[-1]:.6f}" if f[-1] != f[0] else "") + ") and written")
            continue
        refused[sym] = (f"the response leaves out {len(gone)} stored day(s) ({_days(gone)}"
                        + (f") and its adj_close / close differs from the stored one by up to {moved:.4%}" if moved is not None
                           else ") and shares no day with them")
                        + ": a new adjustment basis, stored rows kept. If the next run leaves the same days out, see "
                          "docs/RUNBOOK.md §4 (指数 / 因子 ETF 没写入)")
    return (pd.concat(keep, ignore_index=True) if keep else df.iloc[0:0]), refused, rescaled


def backfill_index(store: PanelStore, start: str = "2012-01-01", download=None) -> int:
    """SPY / ^VIX / IWM / QQQ into index_daily; agent.execute (13:25 and 16:10 PT, on the order path) and agent.daily
    (08:41 PT) call it. The whole history every time (Adj Close is rescaled at every distribution, see FACTOR_ETFS),
    through basis_guard. Returns the rows written; a symbol that returned nothing or was refused keeps its stored rows
    and is printed, never raised."""
    download = download or _download
    # Same cut as backfill_bars and backfill_factor_etfs: while the session is open yfinance returns today's
    # in-progress bar as a daily row, and a manual intraday agent.execute or agent.daily would store it as a close
    # (2026-10-21). Stop at yesterday (yf's end is exclusive) unless today's session is over; the scheduled runs are
    # all after 16:15 ET (13:25 PT = 16:25 ET, 16:10 PT = 19:10 ET) and still get today's bar; a late-evening re-run
    # does too (download_end takes New York's date).
    end = download_end()
    raw = download(list(INDEX_SYMBOLS), start, end)
    frames = []
    for sym in INDEX_SYMBOLS:
        sub = _frame(raw, sym) if raw is not None and not raw.empty else pd.DataFrame()
        if sub.empty:                              # a failed download must not become a KeyError
            print(f"  index {sym}: no rows returned, kept existing")
            continue
        sub = sub.reset_index().rename(columns={"Date": "trade_date"})
        sub.insert(0, "symbol", sym)
        sub["trade_date"] = pd.to_datetime(sub["trade_date"]).dt.date
        sub["source"] = SOURCE
        sub["fetched_at"] = pd.Timestamp.now()
        frames.append(sub[["symbol", "trade_date", "open", "high", "low", "close", "adj_close",
                           "source", "fetched_at"]])
    if not frames:
        return 0
    df, refused, rescaled = basis_guard(store, pd.concat(frames, ignore_index=True))
    for sym, why in rescaled.items():
        print(f"  index {sym}: {why}")
    for sym, why in refused.items():                # "failed": health's job-log scan warns on it (agent/health.py)
        print(f"  index {sym}: update failed, not written: {why}")
    return store.upsert_index(df)


# Research-only factor ETFs for the S45 D3 attribution (momentum MTUM/SPMO, value VLUE, quality QUAL,
# low vol USMV, small value AVUV/IJS/VBR/IWN, small growth IWO). Kept apart from INDEX_SYMBOLS on
# purpose (S48): backfill_index runs inside execute.py and daily.py, and one throttled research
# download must never cost SPY its close. The index audit keeps checking SPY/IWM/QQQ/^VIX. AVUV starts
# 2019-09: regressions that use it cover a shorter window and must say so.
# S54 (2026-10-20): this comment said "run by hand or by the weekly job", but no job ever called it, so after the
# one manual backfill (2026-09-29) index_daily stopped at 2026-09-28 for all ten. The long book's frozen ETF
# benchmark (agent/drift.py) needs MTUM/VLUE/QUAL/USMV every day: agent/bin/postclose.sh now runs
# `python -m agent.backfill factors` after the close, as a step of its own (non-fatal, off the order path). The
# whole history is downloaded each time, as backfill_index does: Yahoo's Adj Close is rescaled back to the start
# at every distribution, and rows written from a short window would not match the older ones. Nothing checked that a
# response did cover the stored history; basis_guard does since 2026-10-21.
FACTOR_ETFS = ("MTUM", "VLUE", "QUAL", "USMV", "AVUV", "IJS", "VBR", "IWN", "IWO", "SPMO")


def backfill_factor_etfs(store: PanelStore | None = None, start: str = "2012-01-01",
                         download=None, end: str | None = None) -> dict:
    """Download FACTOR_ETFS daily bars and upsert them into index_daily.

    Downloads first, then opens the panel only for the write (when no store is passed), so the
    write lock is held for seconds, not for the download. A symbol with no rows keeps whatever
    is stored; so does one basis_guard refuses. Returns {"rows": n, "symbols": {sym: n_rows returned},
    "missing": [...], "refused": {sym: why}, "rescaled": {sym: the left-out stored days basis_guard rescaled}}.
    Manual use:
      python -c "from agent.backfill import backfill_factor_etfs; print(backfill_factor_etfs())"
    """
    download = download or _download
    # Same cut as backfill_bars: while the session is open yfinance returns today's in-progress bar
    # as a daily row, which would sit in index_daily as a close until the next run. Stop at
    # yesterday (yf's end is exclusive) unless today's session is over (download_end).
    end = end or download_end()
    raw = download(list(FACTOR_ETFS), start, end)
    frames, per_symbol, missing = [], {}, []
    now = pd.Timestamp.now()
    for sym in FACTOR_ETFS:
        sub = _frame(raw, sym) if raw is not None and not raw.empty else pd.DataFrame()
        if sub.empty:
            missing.append(sym)
            continue
        sub = sub.reset_index().rename(columns={"Date": "trade_date"})
        sub.insert(0, "symbol", sym)
        sub["trade_date"] = pd.to_datetime(sub["trade_date"]).dt.date
        sub["source"] = SOURCE
        sub["fetched_at"] = now
        per_symbol[sym] = int(len(sub))
        frames.append(sub[["symbol", "trade_date", "open", "high", "low", "close", "adj_close",
                           "source", "fetched_at"]])
    if not frames:
        return {"rows": 0, "symbols": {}, "missing": missing, "refused": {}, "rescaled": {}}
    df = pd.concat(frames, ignore_index=True)
    if store is not None:
        df, refused, rescaled = basis_guard(store, df)
        n = store.upsert_index(df)
    else:
        with PanelStore() as st:                   # the stored rows are read inside the write's short lock
            df, refused, rescaled = basis_guard(st, df)
            n = st.upsert_index(df)
    return {"rows": n, "symbols": per_symbol, "missing": missing, "refused": refused, "rescaled": rescaled}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["bars", "index", "factors"])
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--incremental", action="store_true")
    args = ap.parse_args()
    if args.cmd == "factors":                      # downloads first; the panel is opened only for the write
        out = backfill_factor_etfs()
        for sym, why in (out.get("rescaled") or {}).items():
            print(f"  factor ETF {sym}: {why}")
        for sym, why in (out.get("refused") or {}).items():
            print(f"  factor ETF {sym}: update failed, not written: {why}")
        print({"factor_etfs": out})
        # 1: nothing written; 2: written except the refused ETFs (agent/bin/postclose.sh logs which, both non-fatal)
        return 1 if not out["rows"] else 2 if out.get("refused") else 0
    with PanelStore() as store:
        if args.cmd == "bars":
            print(backfill_bars(store, args.start, args.chunk, args.incremental))
        else:
            print({"index_rows": backfill_index(store)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
