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
interval's end and the new start, and the last close on or before the old end and the first close on or
after the new start differ by less than SAME_MAX_MOVE of the earlier one (a redomicile, a reorganisation
that kept the stock).

A split ticker's bars before `cutoff` go to a pseudo ticker '<TICKER>@<YYYY>' (the year of the old
interval's ipo_date), whose listing is the old interval(s); the real ticker keeps the bars from `cutoff`
and the current interval. `cutoff` is the new start less WHEN_ISSUED_DAYS (when-issued trading), but
never on or before the old interval's end: the old security's last bars are never the new one's first.
Bars in the gap before `cutoff` go to the pseudo ticker, where the mask calls them unlisted. Fundamentals
filed before the new start follow the bars. A pseudo ticker is never listed after its old interval ended,
so it can be picked in a backtest of its own years and never on the last bar, which is what live lists
are built on (agent.books.data.load_market refuses a market where one is).

Point in time: the split uses the listing rows and the bars as they stand today, so a backtest day before
a ticker was reused sees the same series it would have seen then, under another name. The only thing the
past could not have known is the name itself.

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
PSEUDO_MARK = "@"
LISTING = ["symbol", "status", "ipo_date", "delisting_date"]
SPLIT_COLS = ["symbol", "pseudo", "old_ipo", "old_end", "new_start", "cutoff", "n_old", "first_bar",
              "gap_sessions", "close_before", "close_after", "split"]


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
def seams(listing: pd.DataFrame) -> pd.DataFrame:
    """Per symbol with a Delisted interval that ends before its current Active interval starts: new_start (the
    latest Active ipo_date), old_end (the latest such delisting), old_ipo (that row's ipo_date), n_old."""
    ls = listing.copy()
    for c in ("ipo_date", "delisting_date"):
        ls[c] = pd.to_datetime(ls[c])
    new = ls[ls["status"] == "Active"].groupby("symbol")["ipo_date"].max().dropna().rename("new_start")
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
    symbol (the listing symbol it matched), first_bar, close_before (last close on or before old_end),
    close_after (first close on or after new_start). `sessions`: the trading days, to count the gap."""
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
    same = (df["gap_sessions"] <= SAME_MAX_GAP) & (move < SAME_MAX_MOVE)          # NaN (a close missing) is not same
    df["split"] = ~same
    df["cutoff"] = np.maximum(df["new_start"] - pd.Timedelta(days=WHEN_ISSUED_DAYS), df["old_end"] + pd.Timedelta(days=1))
    df["pseudo"] = [pseudo_name(t, y) for t, y in zip(df.index, df["old_ipo"])]
    df["first_bar"] = pd.to_datetime(df["first_bar"])
    return df[SPLIT_COLS].sort_index()


# ---------------------------------------------------------------- from the store -------
def load_splits(store, table: str = "listing_status", extra: pd.DataFrame | None = None,
                listing: pd.DataFrame | None = None, all_seams: bool = False) -> pd.DataFrame:
    """split_points on the panel: the listing rows of `table` (+ the supplement), each seam's facts from the
    whole bars table (not a load window, so a backtest and the live list split the same way). Only the rows
    that split unless `all_seams`."""
    ls = listing_rows(store, table, extra) if listing is None else listing
    seam = seams(ls)
    if seam.empty:
        return pd.DataFrame(columns=SPLIT_COLS)
    s = seam.reset_index()
    s["key"] = s["symbol"].map(spelling)
    s = s[["symbol", "key", "old_end", "new_start"]]
    con = store.con
    con.register("_s47b_seams", s)
    try:
        facts = con.execute("""
            SELECT b.ticker, s.symbol, min(b.trade_date) AS first_bar,
                   arg_max(b.close, b.trade_date) FILTER (WHERE b.trade_date <= s.old_end) AS close_before,
                   arg_min(b.close, b.trade_date) FILTER (WHERE b.trade_date >= s.new_start) AS close_after
            FROM bars b JOIN _s47b_seams s ON replace(b.ticker, '.', '-') = s.key
            GROUP BY 1, 2""").df()
        lo = min(s["old_end"]).date()
        sessions = con.execute("SELECT DISTINCT trade_date FROM bars WHERE trade_date >= ?", [lo]).df()["trade_date"]
    finally:
        con.unregister("_s47b_seams")
    facts = _prefer_exact(facts).set_index("ticker")
    out = split_points(seam, facts, pd.DatetimeIndex(pd.to_datetime(sessions)))
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


def routes(splits: pd.DataFrame) -> dict[str, tuple[str, pd.Timestamp]]:
    """ticker -> (pseudo, new_start): the listing rows that end before new_start are the pseudo ticker's."""
    if splits is None or splits.empty:
        return {}
    return {t: (r["pseudo"], r["new_start"]) for t, r in splits.iterrows()}
