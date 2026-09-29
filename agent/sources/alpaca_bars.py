"""Alpaca daily bars → panel.bars, including delisted names (free tier).

Measured 2026-09-20 on 150 non-S&P names listed in 2016q1: NYSE 98%,
NASDAQ 85%, AMEX 80% have that year's bars, versus 44% from yfinance,
which deletes a company's history when it delists. That gap is what makes
a small/mid-cap study possible at all (docs/AGENT_PLAN.md §9).

Free-plan notes that matter:
- historical SIP (full-market) bars ARE served; only recent timestamps
  (< 15 minutes) need a subscription. IEX has no useful pre-2018 history.
- `adjustment=all` gives split+dividend adjusted prices; we store raw
  close in `close` and adjusted in `adj_close` by asking twice, so the
  panel keeps its convention (RV from raw OHLC, returns from adj_close).
- Up to ~100 symbols per request, paginated with next_page_token.

Universe: exchange-listed names from panel.listing_status (NYSE, NASDAQ,
AMEX and variants), restricted to those seen in panel.issuer_seen, so OTC
shells stay out.

Two rules since the 2026-09-28 audit (S47 items 4 and 5):
- A request that fails is never silent. Status 0 / 5xx / 429 is retried;
  a batch that still fails is halved until the symbols behind the failure
  stand alone, and they come back in stats["failed_symbols"]. Nothing is
  stored for a symbol unless both its raw and its adjusted bars arrived:
  the raw close is never written as adj_close.
- One symbol, one adjustment basis. adj_close is adjusted as of the day it
  was fetched, so a 7-day update leaves the older rows on the basis of
  their own fetch day: every later dividend then shows as a false drop at
  the edge of the window, every split as a false jump. update() writes the
  window, then looks for such a break between adjacent Alpaca rows of the
  symbols it wrote and re-fetches the whole history of a symbol that has
  one. A full re-fetch multiplies the history by a constant, so past
  returns do not change.

Usage:
  python -m agent.sources.alpaca_bars backfill [--start 2015-01-01] [--limit 500]
  python -m agent.sources.alpaca_bars update [--days 7] [--extra AAPL,MSFT]   # after the close, whole universe
  python -m agent.sources.alpaca_bars missing [--dry-run]                     # universe symbols without an Alpaca row: full history
  python -m agent.sources.alpaca_bars repair [--since 2026-09-14] [--dry-run] # symbols with a break in the adjustment basis: full history
  python -m agent.sources.alpaca_bars coverage
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Callable

import numpy as np
import pandas as pd

from agent.sources.price_probe import _get, keys_from_env
from hedge_fund.features.panel import PanelStore

BASE = "https://data.alpaca.markets/v2/stocks/bars"
EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "NYSE MKT", "NYSE ARCA", "BATS")
BATCH = 100
SOURCE = "alpaca"
HISTORY_START = "2015-01-01"
RETRIES = 3                       # of status 0 (timeout, DNS), 5xx and 429, per page
ABORT_AFTER = 3                   # consecutive batches without any answer: the vendor is down, stop asking
FACTOR_TOL = 0.0005               # 0.05%: adj_close/close moving by more than this is another adjustment basis
SPLIT_MIN = 1.25                  # a factor change of this ratio or more is a split (or a false one), not a dividend
REPAIR_SINCE = "2026-09-14"       # the first trade date written by the incremental update
OVERLAP = 3                       # update(): the window reaches this many days behind the newest stored bar ...
MAX_WINDOW = 60                   # ... but never further back than this: a longer gap is a backfill by hand
LOOKBACK = 30                     # update(): a break this many days before the window is still found (a failed re-fetch)
CLASS_SHARE = re.compile(r"^[A-Z]+-[A-Z]$")
VENDOR_CLASS = re.compile(r"^[A-Z]+\.[A-Z]$")

Http = Callable[[str, dict], tuple[int, str]]
Sleep = Callable[[float], None]


def universe(store: PanelStore, limit: int | None = None, extra: pd.DataFrame | None = None) -> list[str]:
    """Exchange-listed stocks with a Form 4 issuer. The listing rows are listing_status's and the hand-checked
    ones of agent/listing_supplement.yaml (S47b item 3: NRG is not in the vendor's list; `extra`, None reads
    the file)."""
    from agent.books.segments import supplement
    sup = supplement() if extra is None else extra
    sup = pd.DataFrame({"symbol": pd.Series(sup["symbol"], dtype=str), "exchange": pd.Series(sup["exchange"], dtype=str)})
    store.con.register("_listing_supplement", sup)
    try:
        q = """
            SELECT DISTINCT l.symbol FROM (SELECT symbol, exchange FROM listing_status WHERE asset_type = 'Stock'
                                           UNION ALL SELECT symbol, exchange FROM _listing_supplement) l
            JOIN (SELECT DISTINCT ticker FROM issuer_seen) i ON i.ticker = l.symbol
            WHERE l.exchange IN ('NYSE','NASDAQ','AMEX','NYSE MKT','NYSE ARCA','BATS')
            ORDER BY l.symbol"""
        if limit:
            q += f" LIMIT {limit}"
        return store.con.execute(q).df()["symbol"].tolist()
    finally:
        store.con.unregister("_listing_supplement")


def vendor_symbol(sym: str) -> str:
    """The panel's spelling → Alpaca's. A class share is 'BRK-A' in listing_status and 'BRK.A' at Alpaca,
    which refuses the whole request when one symbol has a hyphen (batches 11, 12, 44 until 2026-09-28)."""
    return sym.replace("-", ".") if CLASS_SHARE.match(sym) else sym


def panel_symbol(sym: str) -> str:
    """Alpaca's spelling (and the broker's) → listing_status's: 'BRK.A' → 'BRK-A'. A held 'BRK.A' passed to
    update() next to the universe's 'BRK-A' is then one symbol, not two spellings of one request symbol.
    The yfinance rows of S&P names keep the dot ('BRK.B', 'BF.B'), so update() maps only into the universe."""
    return sym.replace(".", "-") if VENDOR_CLASS.match(sym) else sym


# ---------------------------------------------------------------- requests -------------
def _request(symbols: list[str], start: str, end: str, headers: dict, adjustment: str,
             http: Http, sleep: Sleep) -> tuple[int, dict[str, list[dict]]]:
    """Every page of one request. (200, bars) only when all pages arrived: a page that still fails after
    the retries fails the request, so half a history is never returned as if it were whole."""
    out: dict[str, list[dict]] = {}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                  "limit": 10000, "feed": "sip", "adjustment": adjustment}
        if token:
            params["page_token"] = token
        url = f"{BASE}?{urllib.parse.urlencode(params)}"
        for attempt in range(RETRIES + 1):
            code, body = http(url, headers)
            if code == 200 or attempt == RETRIES or not (code in (0, 429) or code >= 500):
                break
            sleep(20.0 * (attempt + 1) if code == 429 else 2.0 * 3 ** attempt)   # free tier is 200 requests/min
        if code != 200:
            return code, {}
        try:
            payload = json.loads(body)
        except ValueError:                   # a truncated body
            return 0, {}
        if not isinstance(payload, dict) or not isinstance(payload.get("bars") or {}, dict):
            return 0, {}                     # 200 with a list or a string: a failed page, not a crash of the run
        for sym, bars in (payload.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = payload.get("next_page_token")
        if not token:
            return 200, out


def _isolate(symbols: list[str], code: int, call) -> tuple[dict[str, list[dict]], list[str]]:
    """A request for `symbols` failed with `code`: halve it until what fails stands alone.

    Returns (bars of the symbols that were answered, symbols that were not). 401/403 is the key or the
    plan, not a symbol. A 4xx is the request itself being refused, so a symbol in it is at fault and the
    halving goes on to the end. For 0 / 5xx / 429 it stops as soon as both halves fail too: the vendor
    is not answering, and an outage must cost three requests per batch, not two hundred."""
    if len(symbols) == 1 or code in (401, 403):
        return {}, list(symbols)
    mid = len(symbols) // 2
    halves = [symbols[:mid], symbols[mid:]]
    answers = [call(h) for h in halves]
    refused = 400 <= code < 500 and code != 429
    if not refused and all(c != 200 for c, _ in answers):
        return {}, list(symbols)
    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    for half, (c, bars) in zip(halves, answers):
        if c != 200:
            bars, lost = _isolate(half, c, call)
            failed += lost
        out.update(bars)
    return out, failed


def fetch_batch(symbols: list[str], start: str, end: str, key: str, secret: str, adjustment: str,
                http: Http = _get, sleep: Sleep = time.sleep) -> tuple[dict[str, list[dict]], list[str]]:
    """(bars by panel symbol, symbols the vendor did not answer for). A symbol in neither was answered
    with no bars: unknown to Alpaca, or no trade in the period."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    panel: dict[str, list[str]] = {}                     # vendor spelling -> every panel spelling asked for
    for s in dict.fromkeys(symbols):                     # 'BRK-A' and 'BRK.A' in one batch: both get the bars
        panel.setdefault(vendor_symbol(s), []).append(s)

    def call(batch: list[str]) -> tuple[int, dict[str, list[dict]]]:
        return _request(batch, start, end, headers, adjustment, http, sleep)

    code, bars = call(list(panel))
    failed: list[str] = []
    if code != 200:
        bars, failed = _isolate(list(panel), code, call)
    return {p: b for s, b in bars.items() for p in panel.get(s, [s])}, [p for s in failed for p in panel[s]]


def _end_now() -> str:
    # the free tier 403s when the query's END bound is inside the last 15 minutes; a bound
    # 16 minutes back still returns today's bar in full once the session has closed
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- writes ---------------
def _rows(sym: str, raw: list[dict], adj: list[dict], now: pd.Timestamp) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    df["trade_date"] = pd.to_datetime(df["t"]).dt.date
    a = pd.DataFrame(adj)
    a["trade_date"] = pd.to_datetime(a["t"]).dt.date
    df = df.merge(a[["trade_date", "c"]].rename(columns={"c": "adj_close"}), on="trade_date", how="left")
    df.loc[df["adj_close"] <= 0, "adj_close"] = None            # Alpaca sometimes returns 0 (audit 2026-09-22)
    return pd.DataFrame({"ticker": sym, "trade_date": df["trade_date"], "open": df["o"],
                         "high": df["h"], "low": df["l"], "close": df["c"],
                         "adj_close": df["adj_close"], "volume": df["v"],
                         "source": SOURCE, "fetched_at": now})


def _drop_unreturned(store: PanelStore, frame: pd.DataFrame, now: pd.Timestamp) -> None:
    """After a full re-fetch: a stored Alpaca row inside the span the vendor just answered for, which the
    vendor no longer returns, would keep the old basis. It goes; rows outside the span are left alone."""
    span = frame.groupby("ticker")["trade_date"].agg(["min", "max"]).reset_index()
    span.columns = ["ticker", "first", "last"]
    store.con.register("_span_in", span)
    store.con.execute("""DELETE FROM bars USING _span_in s
                         WHERE bars.ticker = s.ticker AND bars.source = ? AND bars.fetched_at < ?
                           AND bars.trade_date BETWEEN s.first AND s.last""", [SOURCE, now])
    store.con.unregister("_span_in")


def _load(store: PanelStore, syms: list[str], start: str, end: str, pause: float, quiet: bool, replace: bool,
          http: Http, sleep: Sleep, keys: tuple[str, str] | None) -> tuple[dict, list[str]]:
    """The loop behind backfill(). Returns (stats, symbols written)."""
    key, secret = keys or keys_from_env("alpaca")
    stats = {"symbols": len(syms), "rows": 0, "with_data": 0, "failed_symbols": [], "n_batches_failed": 0}
    written: list[str] = []
    now = pd.Timestamp.now()
    dead = 0
    for i in range(0, len(syms), BATCH):
        batch = syms[i:i + BATCH]
        if dead >= ABORT_AFTER:                                  # not asked: reported, not dropped in silence
            stats["failed_symbols"] += batch
            stats["n_batches_failed"] += 1
            continue
        raw, failed = fetch_batch(batch, start, end, key, secret, "raw", http, sleep)
        asked = [s for s in batch if s not in failed]
        adj, lost = fetch_batch(asked, start, end, key, secret, "all", http, sleep) if asked else ({}, [])
        failed = failed + lost
        frames = []
        for sym, bars in raw.items():
            if not bars or sym in failed:
                continue
            if not adj.get(sym):                                 # raw bars and no adjusted ones: the raw close
                failed.append(sym)                               # is not an adjusted close (audit 2026-09-28)
                continue
            try:
                frames.append(_rows(sym, bars, adj[sym], now))
            except (KeyError, TypeError, ValueError):            # a bar without its fields
                failed.append(sym)
        dead = dead + 1 if len(failed) == len(batch) else 0
        if failed:
            stats["failed_symbols"] += failed
            stats["n_batches_failed"] += 1
        if frames:
            frame = pd.concat(frames, ignore_index=True)
            stats["rows"] += store.upsert_bars(frame)
            if replace:
                _drop_unreturned(store, frame, now)
            names = frame["ticker"].unique().tolist()
            stats["with_data"] += len(names)
            written += names
        if not quiet:
            print(f"  [{min(i + BATCH, len(syms))}/{len(syms)}] rows {stats['rows']} "
                  f"symbols with data {stats['with_data']} failed {len(stats['failed_symbols'])}", flush=True)
        sleep(pause)
    if dead >= ABORT_AFTER:
        stats["aborted"] = f"{ABORT_AFTER} batches in a row without an answer"
    return stats, written


def backfill(store: PanelStore, start: str, end: str, limit: int | None = None,
             pause: float = 0.35, symbols: list[str] | None = None, quiet: bool = False,
             replace: bool = False, http: Http = _get,
             sleep: Sleep = time.sleep, keys: tuple[str, str] | None = None) -> dict:
    """Raw and adjusted bars of [start, end], 100 symbols per request.

    stats["failed_symbols"]: symbols with nothing stored because a request failed (after retries and
    halving); stats["n_batches_failed"]: batches of 100 with at least one of them. `replace`: the fetch
    is a symbol's whole history, so its stored rows are replaced, not merged. `http` and `keys` are
    there for the tests: no credentials, no network.
    """
    syms = symbols if symbols is not None else universe(store, limit)
    return _load(store, syms, start, end, pause, quiet, replace, http, sleep, keys)[0]


# ---------------------------------------------------------------- adjustment basis -----
def _tick(px: pd.Series) -> np.ndarray:
    """The increment the vendor rounded a price to, as the stored value shows it: $0.01 when it has at most
    two decimals (almost every adj_close from $10 up), $0.001 with three, else $0.0001."""
    p = px.to_numpy(dtype=float)
    return np.where(np.abs(p - p.round(2)) < 1e-9, 0.01, np.where(np.abs(p - p.round(3)) < 1e-9, 0.001, 0.0001))


def adjustment_breaks(con, since: str = REPAIR_SINCE, tickers: list[str] | None = None) -> pd.DataFrame:
    """Rows where the adjustment basis breaks between two fetches, from `since` on (of `tickers` if given).

    Two adjacent Alpaca rows of one symbol, fetched at different times, whose adj_close/close differs by
    more than FACTOR_TOL plus the vendor's rounding: half a tick of adj_close on each row. On one basis a
    $12 stock with a two-decimal adj_close moves its factor by up to 0.09% from row to row (NEWT
    2026-09-21, -0.076%); without the rounding every dividend payer between $10 and $20 would show
    breaks once its rows come from different evenings. What the vendor's own corporate actions explain
    is not a break either:
    - the factor rises by less than SPLIT_MIN: an ex-dividend date;
    - the factor moves by SPLIT_MIN or more and the raw close moves the other way, so that the adjusted
      return is the smaller of the two: a split on its ex-date (HUBC 2026-09-14, 1:25: factor 25 → 1,
      raw close 0.34 → 7.64).
    The rest is an older row that kept the basis of its own fetch day. kind 'drop': a dividend or a
    forward split after the older row was fetched (ADAM 2026-09-15, -3.4%); kind 'jump': a reverse
    split (IBO 2026-09-16, adj_close 0.529 → 6.285 on a raw close of 0.498).
    """
    df = con.execute("""
        WITH x AS (
            SELECT ticker, trade_date, close, adj_close, adj_close / close AS factor, fetched_at,
                   lag(trade_date) OVER w AS prev_date, lag(close) OVER w AS prev_close, lag(adj_close) OVER w AS prev_adj,
                   lag(adj_close / close) OVER w AS prev_factor, lag(fetched_at) OVER w AS prev_fetched_at
            FROM bars WHERE source = ? AND close > 0 AND adj_close > 0     -- the previous Alpaca row can be weeks back:
                        AND trade_date >= CAST(? AS DATE) - INTERVAL 30 DAY  -- the 08:41 job rewrites S&P rows as yfinance
            WINDOW w AS (PARTITION BY ticker ORDER BY trade_date))
        SELECT * FROM x
        WHERE trade_date >= CAST(? AS DATE) AND fetched_at <> prev_fetched_at
          AND abs(factor / prev_factor - 1) > ?
        ORDER BY ticker, trade_date""", [SOURCE, since, since, FACTOR_TOL]).df()
    if tickers is not None:
        df = df[df["ticker"].isin(set(tickers))]
    tol = FACTOR_TOL + _tick(df["adj_close"]) / 2 / df["adj_close"] + _tick(df["prev_adj"]) / 2 / df["prev_adj"]
    df = df[(df["factor"] / df["prev_factor"] - 1).abs() > tol].reset_index(drop=True)
    g = np.log(df["factor"] / df["prev_factor"])
    raw = np.log(df["close"] / df["prev_close"])
    big = g.abs() >= np.log(SPLIT_MIN)
    explained = (big & ((raw + g).abs() < raw.abs())) | (~big & (g > 0))
    df["kind"] = np.where(g > 0, "jump", "drop")
    df["change_pct"] = (np.exp(g) - 1) * 100
    return df[~explained].reset_index(drop=True)


def missing_symbols(store: PanelStore) -> list[str]:
    """Universe symbols with no Alpaca row at all (2026-09-28: the three batches of 100 that held
    BRK-A, BWL-A and LGF-B, and the names Alpaca does not serve)."""
    have = {r[0] for r in store.con.execute("SELECT DISTINCT ticker FROM bars WHERE source = ?", [SOURCE]).fetchall()}
    return [s for s in universe(store) if s not in have]


def backfill_missing(store: PanelStore, start: str = HISTORY_START, end: str | None = None, **kw) -> dict:
    """S47 item 4: the history of every universe symbol that never received a row."""
    stats = backfill(store, start, end or _end_now(), symbols=missing_symbols(store), **kw)
    stats["still_missing"] = len(missing_symbols(store))
    return stats


def repair_adjustments(store: PanelStore, since: str = REPAIR_SINCE, end: str | None = None, **kw) -> dict:
    """S47 item 5, one-off: every symbol with a break in its adjustment basis since `since` is fetched
    again in full and its rows replaced."""
    breaks = adjustment_breaks(store.con, since)
    syms = sorted(breaks["ticker"].unique())
    stats = backfill(store, HISTORY_START, end or _end_now(), symbols=syms, replace=True, **kw)
    left = adjustment_breaks(store.con, since)
    stats.update({"breaks_before": len(breaks), "breaks_after": len(left),
                  "symbols_after": sorted(left["ticker"].unique())})
    return stats


def update(store: PanelStore, days: int = 7, extra: list[str] | None = None, http: Http = _get,
           sleep: Sleep = time.sleep, keys: tuple[str, str] | None = None) -> dict:
    """Incremental: the last `days` calendar days for the whole listed universe (+ `extra`).

    Today's daily bar is served once the session has closed (the free tier only
    withholds the last 15 minutes of intraday data), so an after-close run gets
    the completed bar the books need. ~170 requests, about two minutes.

    After missed evenings the window starts OVERLAP days before the newest stored Alpaca bar instead,
    so the gap is filled and the window still overlaps stored rows; never more than MAX_WINDOW days
    back (stats["gap"]: the rest is a backfill by hand).

    The window is written first, so the day's bar exists whatever happens next. Then the symbols just
    written are checked for a break in the adjustment basis between ADJACENT Alpaca rows: an ex-date
    since the older rows were fetched shows at the edge of the window. Not a comparison of the same
    date: the 08:41 yfinance job rewrites the recent rows of S&P names, so the window often has no
    stored Alpaca row of its own dates to compare with. A symbol with a break, or with no Alpaca row
    before the window (a new listing, a name that never had data), is fetched again in full from
    HISTORY_START, 100 per request, and its rows replaced. If that fails the window rows stay, the
    symbol is in failed_symbols, and the next evenings find the same break (LOOKBACK days back) and
    try again.
    """
    uni = set(universe(store))                           # the broker's 'BRK.A' is the universe's 'BRK-A'; a name outside
    syms = sorted(uni | {panel_symbol(s) if panel_symbol(s) in uni else s for s in extra or []})   # it keeps its spelling
    today = dt.date.today()
    newest = store.con.execute("SELECT max(trade_date) FROM bars WHERE source = ?", [SOURCE]).fetchone()[0]
    start = today - dt.timedelta(days=days)
    if newest is not None:
        start = min(start, newest - dt.timedelta(days=OVERLAP))
    gap = None
    if start < today - dt.timedelta(days=MAX_WINDOW):
        start = today - dt.timedelta(days=MAX_WINDOW)
        gap = f"no Alpaca bar since {newest}: window cut to {start}, run backfill --start {newest} by hand"
    end = _end_now()
    stats, written = _load(store, syms, start.isoformat(), end, 0.2, True, False, http, sleep, keys)
    older = {r[0] for r in store.con.execute("SELECT DISTINCT ticker FROM bars WHERE source = ? AND trade_date < ?",
                                             [SOURCE, start]).fetchall()}
    breaks = adjustment_breaks(store.con, (start - dt.timedelta(days=LOOKBACK)).isoformat(), written)
    full = sorted(set(breaks["ticker"]) | (set(written) - older))
    stats.update({"breaks": sorted(set(breaks["ticker"])), "refetched": []})
    if full:
        again, done = _load(store, full, HISTORY_START, end, 0.2, True, True, http, sleep, keys)
        stats["refetched"] = done
        stats["rows"] += again["rows"]
        stats["failed_symbols"] += [s for s in full if s not in done]
        stats["n_batches_failed"] += again["n_batches_failed"]
    if gap:
        stats["gap"] = gap
    stats["last_bar"] = store.con.execute("SELECT max(trade_date) FROM bars WHERE source = ?", [SOURCE]).fetchone()[0]
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "update", "missing", "repair", "coverage"])
    ap.add_argument("--start", default=HISTORY_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--extra", default="", help="comma-separated symbols to include (e.g. current positions)")
    ap.add_argument("--since", default=REPAIR_SINCE, help="repair: first trade date to look for breaks")
    ap.add_argument("--dry-run", action="store_true", help="missing / repair: list the symbols, fetch nothing")
    args = ap.parse_args()
    end = args.end or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    with PanelStore(read_only=args.dry_run) as store:
        if args.cmd == "backfill":
            print(backfill(store, args.start, end, args.limit))
        elif args.cmd == "update":
            print(update(store, args.days, [s for s in args.extra.split(",") if s]))
        elif args.cmd == "missing":
            syms = missing_symbols(store)
            print(f"{len(syms)} universe symbols without an Alpaca row: {syms}")
            if not args.dry_run:
                print(backfill_missing(store, args.start, args.end))
        elif args.cmd == "repair":
            breaks = adjustment_breaks(store.con, args.since)
            print(f"{len(breaks)} breaks in {breaks['ticker'].nunique()} symbols since {args.since}: "
                  f"{breaks.groupby('kind').size().to_dict()}")
            if not args.dry_run:
                print(repair_adjustments(store, args.since, args.end))
        else:
            print(store.con.execute("""SELECT source, count(*) AS n_rows, count(DISTINCT ticker) AS n_tickers,
                                              min(trade_date) AS first, max(trade_date) AS last
                                       FROM bars GROUP BY 1""").df().to_dict("records"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
