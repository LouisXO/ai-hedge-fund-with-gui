"""Large-cap check after the close: who moved the market, and did the typical large cap move with it.

Display only (private site): not a signal and not read by any book. S37 found no selection edge among
large caps with our factors, so this is for looking at the large caps, not for picking them
(user 2026-10-06: "我们要多看大盘的股票").

  universe   the 500 largest US filers by market cap on the last bar: close x the newest share count we
             have (the fundamentals row or a moomoo / yfinance override, whichever is newer; a display
             needs no point in time, and V has no XBRL count at all). One ticker per CIK (the most traded:
             GOOGL, not GOOG). 20-F / 40-F filers are left out: their count is ordinary shares against a
             price per ADS (TSM x5), and the S&P 500 holds none of them; TSM and ASML are shown apart.
  breadth    cap-weighted vs equal-weighted return of the 500 over 1 / 5 / 20 sessions, share up on the
             day, share above the 50-day average, new 52-week highs and lows
  movers     contribution to the cap-weighted day (weight at the previous close x return, in bp)
  leaders    the 15 largest: market cap, 1 / 5 / 20 sessions, distance from the 52-week high, next earnings
  indexes    SPY / QQQ / IWM (index_daily)
Earnings dates: Alpha Vantage EARNINGS_CALENDAR (3 months, one call on the shared 25/day quota), cached in
~/.hedge-fund/agent/earnings_calendar.csv and fetched again when older than CAL_MAX_AGE_DAYS.

Output: out/agent/market_breadth.json. Usage: python -m agent.market_breadth [--no-earnings] [--out PATH]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

from agent.books.data import share_overrides
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

OUT = "/Users/louis/optradar/out/agent/market_breadth.json"
N_UNIVERSE = 500
N_LEADERS = 15
N_UP, N_DOWN = 8, 5
LOOKBACK_DAYS = 400                  # calendar days of bars: 252 sessions for the 52-week high, with room
EXTRA = ("TSM", "ASML")              # foreign filers we follow, shown apart from the 500
INDEXES = ("SPY", "QQQ", "IWM")
FOREIGN_FORMS = ("20-F", "20-F/A", "40-F", "40-F/A")
CAL_PATH = AGENT_DIR / "earnings_calendar.csv"
CAL_MAX_AGE_DAYS = 3
CAL_MIN_BUDGET = 3                   # leave the news job its calls


def _ret(s: pd.Series, n: int) -> float:
    s = s.dropna()
    return float(s.iloc[-1] / s.iloc[-1 - n] - 1) if len(s) > n else np.nan


def universe(last: pd.DataFrame, shares: pd.Series, cik: pd.Series, foreign: set[int], n: int = N_UNIVERSE) -> pd.DataFrame:
    """last: one row per ticker (close, dollar volume `dv`) on the last bar. Market cap = close x shares;
    one ticker per CIK (highest dv), foreign filers out, the n largest."""
    d = last.copy()
    d["shares"] = shares.reindex(d.index)
    d["cik"] = cik.reindex(d.index)
    d = d[d["shares"].notna() & d["cik"].notna() & ~d["cik"].isin(foreign)]
    d["mcap"] = d["close"] * d["shares"]
    d = d.sort_values("dv", ascending=False).drop_duplicates("cik", keep="first")
    return d.sort_values("mcap", ascending=False).head(n)


def compute(adj: pd.DataFrame, close: pd.DataFrame, uni: pd.DataFrame, idx: pd.DataFrame,
            extra: tuple[str, ...] = EXTRA) -> dict:
    """adj / close: dates x tickers (adjusted for returns, raw for market cap); uni from universe();
    idx: dates x index symbols. Weights for an n-session return are the market caps n sessions back."""
    tick = [t for t in uni.index if t in adj.columns]
    a = adj[tick]
    shares = uni["shares"].reindex(tick)
    out: dict = {"date": str(pd.Timestamp(adj.index[-1]).date()), "n": len(tick)}
    cw, ew = {}, {}
    for n in (1, 5, 20):
        if len(a) <= n:
            continue
        r = a.iloc[-1] / a.iloc[-1 - n] - 1
        w = (close[tick].iloc[-1 - n] * shares).where(r.notna())
        cw[f"r{n}"] = float((w * r).sum() / w.sum())
        ew[f"r{n}"] = float(r.mean())
    r1 = a.iloc[-1] / a.iloc[-2] - 1
    w1 = (close[tick].iloc[-2] * shares).where(r1.notna())
    w1 = w1 / w1.sum()
    contrib = (w1 * r1 * 1e4).dropna()
    ma50 = a.tail(50).mean()
    prior_hi, prior_lo = a.iloc[-252:-1].max(), a.iloc[-252:-1].min()
    out["breadth"] = {
        "cw": cw, "ew": ew,
        "pct_up": float((r1 > 0).sum() / r1.notna().sum()),
        "pct_above_50d": float((a.iloc[-1] > ma50).sum() / a.iloc[-1].notna().sum()),
        "new_highs": int((a.iloc[-1] >= prior_hi).sum()),
        "new_lows": int((a.iloc[-1] <= prior_lo).sum()),
    }
    row = lambda t, bp: {"ticker": t, "r1": float(r1[t]), "w": float(w1[t]), "bp": float(bp)}
    srt = contrib.sort_values()
    out["movers"] = {"up": [row(t, b) for t, b in srt[::-1].head(N_UP).items() if b > 0],
                     "down": [row(t, b) for t, b in srt.head(N_DOWN).items() if b < 0]}

    def stats(t: str, s: pd.Series) -> dict:
        s = s.dropna()
        return {"ticker": t, "r1": _ret(s, 1), "r5": _ret(s, 5), "r20": _ret(s, 20),
                "off_high": float(s.iloc[-1] / s.tail(252).max() - 1) if len(s) else np.nan}
    out["leaders"] = [{**stats(t, a[t]), "mcap_b": float(uni.loc[t, "mcap"] / 1e9)} for t in tick[:N_LEADERS]]
    out["extra"] = [stats(t, adj[t]) for t in extra if t in adj.columns]
    out["indexes"] = {s: {"r1": _ret(idx[s], 1), "r5": _ret(idx[s], 5), "r20": _ret(idx[s], 20),
                          "date": str(pd.Timestamp(idx[s].dropna().index[-1]).date())}
                      for s in INDEXES if s in idx.columns and idx[s].notna().any()}
    return out


def _clean(x):
    """NaN -> None, so the file is valid JSON."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, float) and np.isnan(x):
        return None
    return x


def parse_calendar(text: str) -> pd.DataFrame:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "symbol" not in rows[0] or "reportDate" not in rows[0]:
        raise RuntimeError(f"not an earnings calendar: {text[:120]!r}")
    df = pd.DataFrame(rows)
    return pd.DataFrame({"ticker": df["symbol"].str.upper().str.replace(".", "-", regex=False),
                         "date": pd.to_datetime(df["reportDate"], errors="coerce").dt.date,
                         "time": df.get("timeOfTheDay", pd.Series([""] * len(df))).fillna("")}).dropna(subset=["date"])


def earnings_calendar(fetch: bool = True, today: dt.date | None = None) -> tuple[pd.DataFrame, str]:
    """(calendar, note). Fetched again when the cache is older than CAL_MAX_AGE_DAYS and the quota allows;
    a failed fetch keeps the old cache."""
    today = today or dt.date.today()
    note = ""
    age = (today - dt.date.fromtimestamp(CAL_PATH.stat().st_mtime)).days if CAL_PATH.exists() else None
    if fetch and (age is None or age >= CAL_MAX_AGE_DAYS):
        from agent.sources import av_news
        if av_news.budget_left() >= CAL_MIN_BUDGET:
            try:
                q = urllib.parse.urlencode({"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": av_news.api_key()})
                with urllib.request.urlopen(urllib.request.Request(f"{av_news.URL}?{q}", headers={"User-Agent": "optradar-agent"}),
                                            timeout=60) as resp:
                    text = resp.read().decode()
                av_news._spend(1)
                parse_calendar(text)                                     # refuse to cache a throttle answer
                CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
                CAL_PATH.write_text(text)
                note = "earnings calendar fetched"
            except Exception as exc:                                     # noqa: BLE001 — the old cache stands
                note = f"earnings calendar not refreshed: {type(exc).__name__}"
        else:
            note = "earnings calendar not refreshed: no AV quota left"
    if not CAL_PATH.exists():
        return pd.DataFrame(columns=["ticker", "date", "time"]), note or "no earnings calendar yet"
    return parse_calendar(CAL_PATH.read_text()), note


def add_earnings(out: dict, cal: pd.DataFrame, uni_tickers: list[str], asof: dt.date, days: int = 10) -> dict:
    """next report date on each leader; the 500's reports in the next `days` calendar days."""
    key = lambda t: t.replace(".", "-")                                  # the calendar says BRK-B, the bars BRK.B
    nxt = cal[cal["date"] > asof].sort_values("date").drop_duplicates("ticker").set_index("ticker")
    for x in out.get("leaders", []) + out.get("extra", []):
        k = key(x["ticker"])
        x["next_earnings"] = str(nxt.loc[k, "date"]) if k in nxt.index else None
    horizon = asof + dt.timedelta(days=days)
    soon = [(t, nxt.loc[key(t)]) for t in uni_tickers if key(t) in nxt.index and nxt.loc[key(t), "date"] <= horizon]
    rank = {t: i for i, t in enumerate(uni_tickers)}
    out["earnings_soon"] = [{"ticker": t, "date": str(r["date"]), "time": r["time"], "rank": rank[t] + 1}
                            for t, r in sorted(soon, key=lambda kv: (kv[1]["date"], rank[kv[0]]))]
    return out


def load(store: PanelStore) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series, set[int], pd.DataFrame]:
    con = store.con
    last_day = con.execute("SELECT max(trade_date) FROM bars").fetchone()[0]
    since = last_day - dt.timedelta(days=LOOKBACK_DAYS)
    bars = con.execute("SELECT ticker, trade_date, close, adj_close, volume FROM bars WHERE trade_date >= ?", [since]).df()
    bars = bars[~bars["ticker"].str.contains("@", regex=False)]
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    last_ts = pd.Timestamp(last_day)
    adj = bars.pivot(index="trade_date", columns="ticker", values="adj_close").sort_index()
    close = bars.pivot(index="trade_date", columns="ticker", values="close").sort_index()
    recent = bars[bars["trade_date"] > last_ts - pd.Timedelta(days=30)]
    lastrow = bars[bars["trade_date"] == last_ts].set_index("ticker")[["close"]]
    lastrow["dv"] = (recent["close"] * recent["volume"]).groupby(recent["ticker"]).mean().reindex(lastrow.index)
    f = con.execute("SELECT ticker, cik, shares, filed AS asof FROM fundamentals_pit WHERE shares > 1000").df()
    cik = con.execute("SELECT ticker, cik, filed FROM fundamentals_pit").df().sort_values("filed").groupby("ticker")["cik"].last()
    ov = share_overrides(store).rename(columns={"as_of": "asof"})
    both = pd.concat([f[["ticker", "shares", "asof"]], ov[["ticker", "shares", "asof"]]], ignore_index=True)
    both["asof"] = pd.to_datetime(both["asof"])
    shares = both.dropna().sort_values("asof").groupby("ticker")["shares"].last()

    def spellings(s: pd.Series) -> pd.Series:                            # the bars hold BRK.B and BRK-A
        alias = [s.rename(index=lambda t: t.replace(".", "-")), s.rename(index=lambda t: t.replace("-", "."))]
        out = pd.concat([s, *alias])
        return out[~out.index.duplicated(keep="first")]
    shares, cik = spellings(shares), spellings(cik)
    q = ", ".join(f"'{x}'" for x in FOREIGN_FORMS)
    foreign = {int(c) for (c,) in con.execute(f"SELECT DISTINCT cik FROM xbrl_facts WHERE form IN ({q})").fetchall()}
    idx = con.execute(f"SELECT symbol, trade_date, adj_close FROM index_daily WHERE trade_date >= ? AND symbol IN "
                      f"({', '.join(repr(s) for s in INDEXES)})", [since]).df()
    idx = idx.pivot(index="trade_date", columns="symbol", values="adj_close").sort_index()
    return adj, close, lastrow, shares, cik, foreign, idx


def summary(out: dict) -> str:
    b = out["breadth"]
    up = "、".join(f"{m['ticker']} {m['bp']:+.0f}bp" for m in out["movers"]["up"][:3])
    dn = "、".join(f"{m['ticker']} {m['bp']:+.0f}bp" for m in out["movers"]["down"][:3])
    return (f"大盘体检 {out['date']}:前 {out['n']} 大市值加权 {b['cw'].get('r1', np.nan):+.2%} / 等权 {b['ew'].get('r1', np.nan):+.2%};"
            f"上涨 {b['pct_up']:.0%},站上 50 日线 {b['pct_above_50d']:.0%},新高 {b['new_highs']} / 新低 {b['new_lows']};"
            f"拉动 {up or '—'};拖累 {dn or '—'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-earnings", action="store_true", help="do not fetch the earnings calendar (use the cache)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    with PanelStore(read_only=True) as store:
        adj, close, lastrow, shares, cik, foreign, idx = load(store)
    uni = universe(lastrow, shares, cik, foreign)
    out = compute(adj, close, uni, idx)
    cal, note = earnings_calendar(fetch=not args.no_earnings)
    out = add_earnings(out, cal, list(uni.index), dt.date.fromisoformat(out["date"]))
    out["notes"] = [n for n in (note,) if n]
    out["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(_clean(out), f, ensure_ascii=False, indent=1, default=str)
    print(summary(out))
    for n in out["notes"]:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
