"""Which bars belong to which listing, and the listing rows the vendor lacks (docs/AGENT_PLAN.md S47b items 2, 3).

Item 2, a reused or relisted ticker. The vendor's list gives a ticker one row per listing: SE was Spectra
Energy (Delisted 2017-07-10) before it was Sea Ltd (Active from 2017-10-20), WOLF the old Wolfspeed stock
before the post-reorganisation one. The bars table has one series per ticker, so factors that look back by
row position (12-1 momentum, 252-day volatility) read the other security's prices for a year after the new
listing starts. A ticker is split when

  - a Delisted interval of it ends before its current Active interval (the Active row that starts last)
    starts, and
  - it has bars more than WHEN_ISSUED_DAYS calendar days before that start,

unless it is one security under two listing rows: at most SAME_MAX_GAP sessions strictly between the old
interval's end and the new start, the last close on or before the old end and the first close on or
after the new start differ by less than SAME_MAX_MOVE of the earlier one, and no close in between moves
that much in a day (a redomicile, a reorganisation that kept the stock).

S47b addendum (2026-09-29, recorded before it ran): with no traded bar inside the old interval, nothing in
the bars is the old security's. When the traded bars before the new start run on into it (the last one at
most SAME_MAX_GAP sessions before the start, the first close after it within SAME_MAX_MOVE of that one),
they are the current security under a date the vendor set late (a SPAC that completed, a rename: MRX, CCC,
DRS as RADA) and the ticker is not split; the mask still calls those days unlisted. Bars that stop or jump
there are still split off. On 2026-09-29 this keeps 8 tickers whole (ALTI CCC DRS MRX PRE STI TGE WEST);
HOS and GOLD have bars in the old interval and stay split.

Defined here, not in the plan (review of 2026-09-29): a "close" is one of a bar that TRADED (volume > 0).
The vendor fills a halted or delisted stock with zero-volume bars at its last price (BTU: 2.07 from
2016-04-13 to 2017-04-03, the new stock at 27.25 on 04-04; GPOR: 0.1383 to 2021-05-17, the new stock at
72.95 on 05-18, still inside the old interval), so a filler close on both sides is the same number and
proves nothing. The day-move test (`seam_move`) takes each traded close dated from SEAM_SESSIONS sessions
before the old end to the first traded bar on or after the new start, against the traded close before it
(however far back); it catches GPOR, whose jump falls before the old end.

A split ticker's bars before `cutoff` go to a pseudo ticker '<TICKER>@<YYYY>' (the year of the old
interval's ipo_date), whose listing is the old interval(s); the real ticker keeps the bars from `cutoff`
and the current interval. `cutoff` is the first TRADED bar on or after the new start less
WHEN_ISSUED_DAYS (when-issued trading), and never on or before the old interval's end: the old security's
last bars are never the new one's first, and the zero-volume filler at the old price before the new
security's first trade (SE: 40.68, Spectra's last price, to 2017-10-19; Sea at 16.26 on 10-20) is the
old security's too. With no traded bar from there on, the cutoff is that earliest day. Bars in the gap
before `cutoff` go to the pseudo ticker, where the mask calls them unlisted. Fundamentals filed before
the new start follow the bars; insider events filed before `cutoff` follow them too (`split_events`). A
pseudo ticker is never listed after its old interval ended, so it can be picked in a backtest of its own
years and never on the last bar, which is what live lists are built on (agent.books.data.load_market
refuses a market where one is).

Point in time: the split uses the listing rows and the bars as they stand today, so a backtest day before
a ticker was reused sees the same series it would have seen then, under another name. The only thing the
past could not have known is the name itself. A current listing that has not started by the last bar
(an Active row dated in the future) is no seam: nobody could see it yet (`seams(as_of=)`).
Left as it is: where the vendor's old interval ends on the new security's first trade (GPOR 2021-05-18),
that one bar stays with the pseudo ticker, on its last listed day.

Item 3, spelling and missing rows. A class share is 'BRK-B' in the listing and 'BRK.B' in the yfinance
bars and at the broker: `spelling` is the one key both sides are matched on. A common stock the vendor's
list does not carry at all (NRG) gets a hand-checked row in agent/listing_supplement.yaml.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

SUPPLEMENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "listing_supplement.yaml")
SUPPLEMENT_KEYS = ("symbol", "ipo_date", "exchange", "source", "checked")
WHEN_ISSUED_DAYS = 15            # bars this close before a new listing starts are the new security's (when-issued)
SAME_MAX_GAP = 5                 # sessions strictly between the two intervals, at most, for "one security"
SAME_MAX_MOVE = 0.5              # |first close after / last close before - 1| below this, for "one security"
SEAM_SESSIONS = 10               # the day-move test starts this many sessions before the old end
PSEUDO_MARK = "@"
LISTING = ["symbol", "status", "ipo_date", "delisting_date"]
SPLIT_COLS = ["symbol", "pseudo", "old_ipo", "old_end", "new_start", "cutoff", "n_old", "first_bar",
              "gap_sessions", "close_before", "close_after", "seam_move", "split"]


def spelling(sym: str) -> str:
    """One key for both spellings of a class share: 'BRK.B' (yfinance bars, the broker) is 'BRK-B' (the listing)."""
    return sym.replace(".", "-")


def is_pseudo(ticker: str) -> bool:
    return PSEUDO_MARK in ticker


def pseudo_name(ticker: str, old_ipo) -> str:
    year = pd.Timestamp(old_ipo).year if pd.notna(old_ipo) else 0
    return f"{ticker}{PSEUDO_MARK}{year:04d}"


# ---------------------------------------------------------------- listing rows ---------
def supplement(path: str = SUPPLEMENT) -> pd.DataFrame:
    """agent/listing_supplement.yaml as listing rows: each an Active listing from its ipo_date. A row without
    its source or the date it was checked is refused, not skipped."""
    import yaml
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    out = []
    for r in doc.get("rows") or []:
        miss = [k for k in SUPPLEMENT_KEYS if not r.get(k)]
        if miss:
            raise ValueError(f"{path}: row {r.get('symbol', '?')} lacks {', '.join(miss)}")
        out.append({"symbol": str(r["symbol"]), "status": "Active", "ipo_date": pd.Timestamp(r["ipo_date"]),
                    "delisting_date": pd.NaT, "exchange": str(r["exchange"]), "source": str(r["source"]),
                    "checked": pd.Timestamp(r["checked"])})
    return pd.DataFrame(out, columns=LISTING + ["exchange", "source", "checked"])


def listing_rows(store, table: str = "listing_status", extra: pd.DataFrame | None = None) -> pd.DataFrame:
    """The Stock rows of `table` and the supplement's (`extra`; None reads the file): symbol, status, ipo_date,
    delisting_date, dates as Timestamps."""
    ls = store.con.execute(f"SELECT {', '.join(LISTING)} FROM {table} WHERE asset_type = 'Stock'").df()
    sup = supplement() if extra is None else extra
    if len(sup):
        ls = pd.concat([ls, sup[LISTING]], ignore_index=True)
    for c in ("ipo_date", "delisting_date"):
        ls[c] = pd.to_datetime(ls[c])
    return ls


# ---------------------------------------------------------------- split points (pure) --
def seams(listing: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """Per symbol with a Delisted interval that ends before its current Active interval starts: new_start (the
    latest Active ipo_date), old_end (the latest such delisting), old_ipo (that row's ipo_date), n_old.
    `as_of`: only Active rows that started by then count (a listing dated after the last bar is not one yet)."""
    ls = listing.copy()
    for c in ("ipo_date", "delisting_date"):
        ls[c] = pd.to_datetime(ls[c])
    act = ls[ls["status"] == "Active"]
    if as_of is not None:
        act = act[act["ipo_date"] <= pd.Timestamp(as_of)]
    new = act.groupby("symbol")["ipo_date"].max().dropna().rename("new_start")
    old = ls[ls["status"] != "Active"].join(new, on="symbol", how="inner")
    old = old[old["delisting_date"].notna() & (old["delisting_date"] < old["new_start"])]
    cols = ["new_start", "old_end", "old_ipo", "n_old"]
    if old.empty:
        return pd.DataFrame(columns=cols, index=pd.Index([], name="symbol"))
    old = old.sort_values(["symbol", "delisting_date", "ipo_date"])
    g = old.groupby("symbol")
    out = pd.DataFrame({"new_start": g["new_start"].first(), "old_end": g["delisting_date"].max(),
                        "old_ipo": g["ipo_date"].last(), "n_old": g.size()})
    out.index.name = "symbol"
    return out[cols]


def split_points(seam: pd.DataFrame, facts: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Every seam whose ticker has bars more than WHEN_ISSUED_DAYS before the new start, indexed by the bars'
    ticker, `split` False where the exception holds (one security). `facts` is indexed by the bars' ticker:
    symbol (the listing symbol it matched), first_bar, close_before (last traded close on or before old_end),
    close_after (first traded close on or after new_start), and optionally seam_move (largest one-day
    |close / previous close - 1| over the traded bars around the seam, missing = none) and first_traded (first
    traded bar on or after the earliest cutoff, missing = none). `sessions`: the trading days."""
    if seam.empty or facts.empty:
        return pd.DataFrame(columns=SPLIT_COLS)
    df = facts.join(seam, on="symbol", how="inner")
    df = df[pd.to_datetime(df["first_bar"]) < df["new_start"] - pd.Timedelta(days=WHEN_ISSUED_DAYS)].copy()
    if df.empty:
        return pd.DataFrame(columns=SPLIT_COLS)
    days = np.sort(pd.DatetimeIndex(sessions).unique().to_numpy())
    lo = np.searchsorted(days, df["old_end"].to_numpy(), side="right")
    hi = np.searchsorted(days, df["new_start"].to_numpy(), side="left")
    df["gap_sessions"] = np.maximum(hi - lo, 0)
    move = (df["close_after"].astype(float) / df["close_before"].astype(float) - 1).abs()
    df["seam_move"] = pd.to_numeric(df.get("seam_move", np.nan), errors="coerce")
    same = ((df["gap_sessions"] <= SAME_MAX_GAP) & (move < SAME_MAX_MOVE)            # NaN (a close missing) is not same
            & ~(df["seam_move"] >= SAME_MAX_MOVE))                                  # no day in between jumps that much
    if "pre_date" in df:                                                            # S47b addendum: nothing of the old one
        pre = pd.to_datetime(df["pre_date"]).to_numpy()
        pre_gap = np.searchsorted(days, df["new_start"].to_numpy(), side="left") - np.searchsorted(days, pre, side="right")
        pre_move = (df["close_after"].astype(float) / df["pre_close"].astype(float) - 1).abs()
        same |= (df["close_before"].isna() & pd.notna(pre) & (pre_gap <= SAME_MAX_GAP) & (pre_move < SAME_MAX_MOVE))
    df["split"] = ~same
    df["cutoff"] = earliest_cutoff(df["old_end"], df["new_start"])
    first = pd.to_datetime(df.get("first_traded", pd.NaT))
    df["cutoff"] = pd.Series(first, index=df.index).where(lambda x: x >= df["cutoff"]).fillna(df["cutoff"])
    df["pseudo"] = [pseudo_name(t, y) for t, y in zip(df.index, df["old_ipo"])]
    df["first_bar"] = pd.to_datetime(df["first_bar"])
    return df[SPLIT_COLS].sort_index()


def earliest_cutoff(old_end, new_start):
    """The first day the new security's bars may start: WHEN_ISSUED_DAYS before its listing, after the old end."""
    return np.maximum(pd.to_datetime(new_start) - pd.Timedelta(days=WHEN_ISSUED_DAYS),
                      pd.to_datetime(old_end) + pd.Timedelta(days=1))


# ---------------------------------------------------------------- from the store -------
def load_splits(store, table: str = "listing_status", extra: pd.DataFrame | None = None,
                listing: pd.DataFrame | None = None, all_seams: bool = False) -> pd.DataFrame:
    """split_points on the panel: the listing rows of `table` (+ the supplement) that started by the last bar,
    each seam's facts from the whole bars table (not a load window, so a backtest and the live list split
    the same way). Only the rows that split unless `all_seams`."""
    ls = listing_rows(store, table, extra) if listing is None else listing
    con = store.con
    last = con.execute("SELECT max(trade_date) FROM bars").fetchone()[0]
    seam = seams(ls, as_of=last)
    if seam.empty or last is None:
        return pd.DataFrame(columns=SPLIT_COLS)
    lo = (seam["old_end"].min() - pd.Timedelta(days=3 * SEAM_SESSIONS)).date()
    sessions = pd.DatetimeIndex(pd.to_datetime(
        con.execute("SELECT DISTINCT trade_date FROM bars WHERE trade_date >= ? ORDER BY 1", [lo]).df()["trade_date"]))
    s = seam.reset_index()
    s["key"] = s["symbol"].map(spelling)
    s["lo_cut"] = earliest_cutoff(s["old_end"], s["new_start"])
    i = np.clip(np.searchsorted(sessions.to_numpy(), s["old_end"].to_numpy(), side="right") - SEAM_SESSIONS, 0, None)
    s["win_lo"] = sessions[np.minimum(i, len(sessions) - 1)] if len(sessions) else s["old_end"]
    s = s[["symbol", "key", "old_end", "new_start", "lo_cut", "win_lo"]]
    con.register("_s47b_seams", s)
    try:
        facts = con.execute("""
            WITH b AS (SELECT b.ticker, s.symbol, b.trade_date, b.close, b.volume > 0 AS traded,
                              s.old_end, s.new_start, s.lo_cut, s.win_lo
                       FROM bars b JOIN _s47b_seams s ON replace(b.ticker, '.', '-') = s.key),
            f AS (SELECT ticker, symbol, min(trade_date) AS first_bar,
                         arg_max(close, trade_date) FILTER (WHERE trade_date <= old_end AND traded) AS close_before,
                         arg_min(close, trade_date) FILTER (WHERE trade_date >= new_start AND traded) AS close_after,
                         min(trade_date) FILTER (WHERE trade_date >= new_start AND traded) AS traded_after,
                         min(trade_date) FILTER (WHERE trade_date >= lo_cut AND traded) AS first_traded,
                         max(trade_date) FILTER (WHERE trade_date < new_start AND traded) AS pre_date,
                         arg_max(close, trade_date) FILTER (WHERE trade_date < new_start AND traded) AS pre_close
                  FROM b GROUP BY 1, 2),
            t AS (SELECT ticker, symbol, trade_date, win_lo,          -- each traded close against the traded one before it
                         abs(close / lag(close) OVER (PARTITION BY ticker, symbol ORDER BY trade_date) - 1) AS mv
                  FROM b WHERE traded),
            m AS (SELECT t.ticker, t.symbol, max(t.mv) AS seam_move
                  FROM t JOIN f USING (ticker, symbol)
                  WHERE t.trade_date >= t.win_lo AND t.trade_date <= f.traded_after GROUP BY 1, 2)
            SELECT f.*, m.seam_move FROM f LEFT JOIN m USING (ticker, symbol)""").df()
    finally:
        con.unregister("_s47b_seams")
    facts = _prefer_exact(facts).set_index("ticker")
    out = split_points(seam, facts, sessions)
    return out if all_seams else out[out["split"].astype(bool)]


def _prefer_exact(facts: pd.DataFrame) -> pd.DataFrame:
    """A bars ticker matched by two listing symbols ('BRK.B' by 'BRK-B' and 'BRK.B'): the exact spelling."""
    if facts.empty:
        return facts
    exact = facts["ticker"] == facts["symbol"]
    dup = facts["ticker"].duplicated(keep=False)
    return facts[~dup | exact].drop_duplicates("ticker")


# ---------------------------------------------------------------- applying a split -----
def split_bars(frames: dict[str, pd.DataFrame], splits: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Wide (date x ticker) frames of one load, split alike: a split ticker's values before its cutoff move to
    its pseudo ticker's column. The pseudo column is added only when the first frame has a value there."""
    if splits is None or splits.empty or not frames:
        return frames
    first = next(iter(frames.values()))
    idx = first.index
    moves = []
    for t, r in splits.iterrows():
        if t not in first.columns:
            continue
        before = idx < r["cutoff"]
        moves.append((t, r["pseudo"], before, bool(first.loc[before, t].notna().any())))
    out = {}
    for name, f in frames.items():
        f = f.copy()
        add = {}
        for t, p, before, keep in moves:
            if keep:
                add[p] = f[t].where(before)
            f[t] = f[t].where(~before)
        if add:
            f = pd.concat([f, pd.DataFrame(add, index=f.index)], axis=1)
        out[name] = f
    return out


def split_fundamentals(df: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Rows of a split ticker filed before its new listing started are the old security's: its pseudo ticker."""
    if splits is None or splits.empty or df is None or df.empty:
        return df
    df = df.copy()
    filed = pd.to_datetime(df["filed"])
    for t, r in splits.iterrows():
        old = (df["ticker"] == t) & (filed < r["new_start"])
        df.loc[old, "ticker"] = r["pseudo"]
    return df


def split_events(df: pd.DataFrame, splits: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    """Dated rows (insider filings) of a split ticker dated before its cutoff are the old security's: its pseudo
    ticker, the column whose bars and listing they were traded on."""
    if splits is None or splits.empty or df is None or df.empty:
        return df
    df = df.copy()
    day = pd.to_datetime(df[date_col])
    for t, r in splits.iterrows():
        old = (df["ticker"] == t) & (day < r["cutoff"])
        df.loc[old, "ticker"] = r["pseudo"]
    return df


def routes(splits: pd.DataFrame) -> dict[str, tuple[str, pd.Timestamp]]:
    """ticker -> (pseudo, new_start): the listing rows that end before new_start are the pseudo ticker's."""
    if splits is None or splits.empty:
        return {}
    return {t: (r["pseudo"], r["new_start"]) for t, r in splits.iterrows()}
