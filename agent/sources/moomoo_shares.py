"""Share counts from moomoo market snapshots -> panel.shares_override (S47b item 1).

The XBRL share count is wrong or missing for a few hundred names (per-class counts, ADRs counted in
ordinary shares, facts years old, sources that disagree); the S47 rebuild lists them in
shares_review.csv. moomoo's snapshot carries `issued_shares` in listing units — ADS for an ADR
(TSM 5.19B, BABA 2.49B, AAPL 14.59B, GMRS 54.0M, MCHB 221.4M on 2026-09-29) — and
`total_market_val`; a count is used only when issued_shares x last_price is within 1% of it.

Targets: the review list's liquid names (flag unconfirmed / mismatch / ads / stale, 20-day dollar
volume >= $5M) plus the long book's current top 60 (for the audit's market-cap cross-check; an
unflagged count with a value is never overridden, agent.books.data.fundamentals decides that), plus
every name with a hand-checked override (S28's yfinance counts for V, STZ, ERIE, BRK.B...; manual
rows): an override reaches only rows filed up to 120 days before its as_of, so a count fixed once
would stop reaching new filings unless it is refreshed.
shares_override keeps one row per ticker, latest wins; a manual row newer than the fetch stays.
shares_override_log keeps every (ticker, as_of) row, including the ones shares_override had before
(the fundamentals rows take the earliest override after their filing from either table).
A count here replaces a hand-checked one only on filings after the snapshot: for multi-class names
(moomoo may count one class) check the printed "> 30%" list before the first write and put a
disputed count back as a manual row.

moomoo is read-only here: the quote context and get_market_snapshot, nothing else. The SDK lives in
the moomoo venv:

  PYTHONPATH=<repo> /Users/louis/.moomoo/venv/bin/python -m agent.sources.moomoo_shares [--dry-run] [--tickers A B ...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from collections import deque
from typing import Iterable

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore

SOURCE = "moomoo_snapshot"
BATCH = 200                       # get_market_snapshot takes at most 400 codes; 200 keeps a failed batch cheap
MAX_REQ, WINDOW_S = 60, 30.0      # moomoo's snapshot limit: 60 requests per 30 seconds
MCAP_TOL = 0.01                   # issued_shares x last_price vs total_market_val
ADV_MIN = 5e6                     # the review list is fetched for liquid names only (the long book's floor)
REVIEW_FLAGS = ("unconfirmed", "mismatch", "ads", "stale")
TOP_KEEP = 60                     # the long book keeps a name while it ranks inside this
DDL = ("CREATE TABLE IF NOT EXISTS shares_override(ticker VARCHAR PRIMARY KEY, shares DOUBLE, "
       "source VARCHAR, as_of DATE, xbrl_shares DOUBLE)")
LOG_DDL = ("CREATE TABLE IF NOT EXISTS shares_override_log(ticker VARCHAR, as_of DATE, shares DOUBLE, "
           "source VARCHAR, xbrl_shares DOUBLE, PRIMARY KEY (ticker, as_of))")
COLS = ["ticker", "shares", "source", "as_of", "xbrl_shares"]


class SnapshotError(RuntimeError):
    """moomoo refused a snapshot request (unknown code, throttled, disconnected)."""


class MoomooQuotes:
    """The quote context only. The SDK is imported here, not at module level: this repo's venv may not have it."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        try:
            from moomoo import RET_OK, OpenQuoteContext
        except ImportError as exc:
            raise RuntimeError("the moomoo SDK is not importable in this Python; run with "
                               "/Users/louis/.moomoo/venv/bin/python and PYTHONPATH=<hedge-fund repo>") from exc
        self._ok = RET_OK
        self.q = OpenQuoteContext(host=host, port=port)

    def snapshot(self, codes: list[str]) -> pd.DataFrame:
        ret, df = self.q.get_market_snapshot(list(codes))
        if ret != self._ok:
            raise SnapshotError(str(df)[:200])
        return df

    def close(self) -> None:
        self.q.close()            # the context keeps non-daemon threads alive; without this the process hangs


class _Clock:
    now, sleep = staticmethod(time.monotonic), staticmethod(time.sleep)


def code_of(ticker: str) -> str:
    return "US." + ticker.replace("-", ".")


def fetch(client, tickers: Iterable[str], batch: int = BATCH, max_req: int = MAX_REQ,
          window: float = WINDOW_S, clock=None) -> pd.DataFrame:
    """ticker, issued_shares, last_price, total_market_val for every ticker moomoo answered.

    Requests go in batches of `batch`, paced to `max_req` per `window` seconds. A refused batch is
    split in two until the refusing code stands alone and is dropped (one delisted code refuses the
    whole request)."""
    clock = clock or _Clock()
    back = {}
    for t in tickers:
        back.setdefault(code_of(t), t)
    codes = list(back)
    sent: deque = deque()
    frames, failed = [], []

    def request(chunk):
        while len(sent) >= max_req:
            wait = sent[0] + window - clock.now()
            if wait > 0:
                clock.sleep(wait)
            sent.popleft()
        sent.append(clock.now())
        try:
            frames.append(client.snapshot(chunk))
        except SnapshotError as exc:
            if len(chunk) == 1:
                failed.append((back[chunk[0]], str(exc)[:80]))
            else:
                request(chunk[:len(chunk) // 2])
                request(chunk[len(chunk) // 2:])

    for i in range(0, len(codes), batch):
        request(codes[i:i + batch])
    cols = ["ticker", "issued_shares", "last_price", "total_market_val"]
    if not frames:
        out = pd.DataFrame(columns=cols)
    else:
        snap = pd.concat(frames, ignore_index=True)
        out = snap.assign(ticker=snap["code"].map(back))[cols].dropna(subset=["ticker"])
    out = out.reset_index(drop=True)
    out.attrs["failed"] = failed
    return out


def override_rows(snap: pd.DataFrame, xbrl, as_of: dt.date, tol: float = MCAP_TOL) -> tuple[pd.DataFrame, dict]:
    """shares_override rows from a snapshot, and {ticker: reason} for the names skipped."""
    xbrl = pd.Series(xbrl, dtype=float) if not isinstance(xbrl, pd.Series) else xbrl
    rows, skipped = [], {}
    for r in snap.itertuples(index=False):
        n, px, mv = (pd.to_numeric(pd.Series([r.issued_shares, r.last_price, r.total_market_val]), errors="coerce"))
        if not (n > 0 and px > 0 and mv > 0):
            skipped[r.ticker] = f"no count or price (issued_shares {r.issued_shares}, last {r.last_price}, mcap {r.total_market_val})"
            continue
        if abs(n * px / mv - 1) > tol:
            skipped[r.ticker] = f"issued_shares x last_price is {n * px / mv - 1:+.1%} off total_market_val (tolerance {tol:.0%})"
            continue
        rows.append({"ticker": r.ticker, "shares": float(n), "source": SOURCE, "as_of": as_of,
                     "xbrl_shares": float(xbrl.get(r.ticker, np.nan))})
    return pd.DataFrame(rows, columns=COLS), skipped


def select_targets(review: pd.DataFrame, adv: pd.Series, top: Iterable[str], checked: Iterable[str] = ()) -> list[str]:
    """The review list's flagged names with 20-day dollar volume >= ADV_MIN, plus `top`, plus `checked`."""
    flagged = review.loc[review["shares_flag"].fillna("").isin(REVIEW_FLAGS), "ticker"]
    liquid = [t for t in flagged if adv.get(t, 0) >= ADV_MIN]
    return sorted(set(liquid) | set(top) | set(checked))


def hand_checked(store: PanelStore) -> list[str]:
    """Tickers with an override of another source than moomoo's, now or in the log (never dropped once
    moomoo has replaced the current row)."""
    have = set(store.con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])
    q = [f"SELECT ticker FROM {t} WHERE source IS DISTINCT FROM '{SOURCE}'"
         for t in ("shares_override", "shares_override_log") if t in have]
    return sorted(store.con.execute(" UNION ".join(q)).df()["ticker"]) if q else []


def default_targets(store: PanelStore) -> list[str]:
    """select_targets on today's review list, the last bar's ADV, the long book's top 60 of the last bar
    and the hand-checked names."""
    from agent.books.fundamentals import review_path
    from agent.books.data import load_market
    from agent.books.live import day_scores
    last = pd.Timestamp(store.con.execute("SELECT max(trade_date) FROM bars").fetchone()[0])
    market = load_market(store, (last - pd.Timedelta(days=30)).date().isoformat())
    day = market.adj.index[-1]
    top = day_scores(store, market, day).head(TOP_KEEP).index
    try:
        review = pd.read_csv(review_path(store))
    except FileNotFoundError:
        review = pd.DataFrame(columns=["ticker", "shares_flag"])
    return select_targets(review, market.adv20.loc[day], top, hand_checked(store))


def xbrl_counts(store: PanelStore, tickers: list[str]) -> pd.Series:
    """Our count as built (before any override) on each ticker's latest fundamentals row."""
    df = store.con.execute("SELECT ticker, shares FROM fundamentals_pit QUALIFY row_number() OVER "
                           "(PARTITION BY ticker ORDER BY filed DESC) = 1").df()
    return df.set_index("ticker")["shares"].reindex(tickers)


def _upsert(store: PanelStore, table: str, rows: pd.DataFrame) -> None:
    store.con.register("_moomoo_rows", rows)
    try:
        store.con.execute(f"INSERT OR REPLACE INTO {table} (ticker, shares, source, as_of, xbrl_shares) "
                          "SELECT ticker, shares, source, CAST(as_of AS DATE), xbrl_shares FROM _moomoo_rows")
    finally:
        store.con.unregister("_moomoo_rows")


def write_overrides(store: PanelStore, rows: pd.DataFrame) -> int:
    """INSERT OR REPLACE, one row per ticker in shares_override: a row already there is replaced when it is
    older than the new one, or as old and from the same source (a rerun); a newer row, or a same-day row of
    another source, stays. shares_override_log gets every row by (ticker, as_of) under the same same-day
    rule, after taking in whatever shares_override holds now (the yfinance and manual rows) so that no
    override is lost when its current row is replaced. Returns the shares_override rows written."""
    store.con.execute(DDL)
    store.con.execute(LOG_DDL)
    store.con.execute("INSERT OR IGNORE INTO shares_override_log (ticker, as_of, shares, source, xbrl_shares) "
                      "SELECT ticker, as_of, shares, source, xbrl_shares FROM shares_override WHERE as_of IS NOT NULL")
    rows = rows.drop_duplicates("ticker", keep="last")
    day = lambda x: x.assign(as_of=pd.to_datetime(x["as_of"]).astype("datetime64[ns]"))
    logged = store.con.execute("SELECT ticker, as_of, source AS old_source FROM shares_override_log").df()
    got = day(rows[["ticker", "as_of", "source"]]).merge(day(logged), on=["ticker", "as_of"], how="left")
    same_day_other = (got["old_source"].notna() & (got["old_source"] != got["source"])).to_numpy()
    if (~same_day_other).any():
        _upsert(store, "shares_override_log", rows.loc[~same_day_other, COLS])
    old = store.con.execute("SELECT ticker, source AS old_source, as_of AS old_as_of FROM shares_override").df()
    new = rows.merge(old, on="ticker", how="left")
    new_asof, old_asof = pd.to_datetime(new["as_of"]), pd.to_datetime(new["old_as_of"])
    keep_old = (old_asof > new_asof) | ((old_asof == new_asof) & (new["old_source"] != new["source"]))
    new = new.loc[~keep_old, COLS]
    if new.empty:
        return 0
    _upsert(store, "shares_override", new)
    return len(new)


def main(argv: list[str] | None = None, client_factory=MoomooQuotes) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, write nothing")
    ap.add_argument("--tickers", nargs="+", default=None, help="instead of the review list + top 60 (A B or A,B)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    args = ap.parse_args(argv)
    with PanelStore(read_only=True) as store:          # no write lock while moomoo is being asked
        tickers = (sorted({t.strip().upper() for a in args.tickers for t in a.split(",") if t.strip()})
                   if args.tickers else default_targets(store))
        xbrl = xbrl_counts(store, tickers)
    as_of = dt.date.today()
    client = client_factory(args.host, args.port)
    try:
        snap = fetch(client, tickers)
    finally:
        client.close()
    rows, skipped = override_rows(snap, xbrl, as_of)
    ratio = rows["shares"] / rows["xbrl_shares"].where(rows["xbrl_shares"] > 1000)
    changed = rows[(ratio - 1).abs().gt(0.30) | ratio.isna()]
    print(f"targets {len(tickers)}, answered {len(snap)}, usable {len(rows)}, skipped {len(skipped)}, "
          f"refused {len(snap.attrs['failed'])}; as_of {as_of}")
    for t, why in list(skipped.items()) + snap.attrs["failed"]:
        print(f"  skip {t}: {why}")
    print(f"{len(changed)} usable counts differ from ours by > 30% or we have none:")
    if len(changed):
        print(changed.assign(ratio=ratio[changed.index].round(3)).to_string(index=False))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    with PanelStore() as store:
        n = write_overrides(store, rows)
    print(f"shares_override: {n} rows written ({len(rows) - n} kept: a newer row was there)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
