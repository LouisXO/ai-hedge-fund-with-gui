"""Historical option prices from Alpaca (free tier) → ~/.hedge-fund/agent/options.db.

What is there: daily trade bars (o/h/l/c/v/vw/n) for EVERY listed contract,
expired ones included, back to early 2024; contract lists with strike,
expiry, right, last open interest; current snapshots with IV / greeks /
NBBO. This is the real-price history the option gate has been missing
(S2/S3 used IV proxied from realized vol; Balder's 27,829-straddle null
came from this same feed).

Scope (keeps volume sane, matches what the radar would trade):
  underlyings  point-in-time S&P 500 members 2024→ (panel.membership)
  expiries     monthlies (3rd Friday) only
  strikes      within ±15% of the underlying's close ~45 days before expiry
  window       bars from 70 days before expiry to expiry
≈ 500 names × 32 monthlies × ~12 strikes × 2 rights → ~380k contracts, a
few million bars; 100 symbols per bars request, 200 req/min.

Usage:
  python -m agent.sources.alpaca_options backfill [--start 2024-01-01] [--limit-underlyings N]
  python -m agent.sources.alpaca_options status
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import duckdb
import pandas as pd

from agent.sources.price_probe import keys_from_env
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

OPTIONS_DB = AGENT_DIR / "options.db"
CONTRACTS = "https://paper-api.alpaca.markets/v2/options/contracts"
BARS = "https://data.alpaca.markets/v1beta1/options/bars"
STRIKE_BAND = 0.15
BATCH = 100
DDL = [
    """CREATE TABLE IF NOT EXISTS opt_contracts (symbol VARCHAR PRIMARY KEY, underlying VARCHAR, expiry DATE, cp VARCHAR,
        strike DOUBLE, open_interest DOUBLE, oi_date DATE, close_price DOUBLE, close_price_date DATE, status VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS opt_bars (symbol VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        volume DOUBLE, vwap DOUBLE, n_trades INT, PRIMARY KEY (symbol, trade_date))""",
    """CREATE TABLE IF NOT EXISTS opt_load_log (underlying VARCHAR PRIMARY KEY, n_contracts INT, n_bars INT, loaded_at TIMESTAMP)""",
]


def _get(url: str, params: dict, key: str, secret: str) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20)
                continue
            if e.code >= 500:
                time.sleep(5)
                continue
            raise
        except Exception:
            time.sleep(3)
    return {}


def third_friday(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def monthlies(start: dt.date, end: dt.date) -> list[dt.date]:
    out, y, m = [], start.year, start.month
    while dt.date(y, m, 1) <= end:
        f = third_friday(y, m)
        if start <= f <= end + dt.timedelta(days=60):
            out.append(f)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def contracts_for(underlying: str, start: dt.date, key: str, secret: str) -> list[dict]:
    out = []
    for status in ("inactive", "active"):
        token = None
        while True:
            p = {"underlying_symbols": underlying, "expiration_date_gte": start.isoformat(), "status": status, "limit": 10000}
            if token:
                p["page_token"] = token
            d = _get(CONTRACTS, p, key, secret)
            out += d.get("option_contracts") or []
            token = d.get("next_page_token")
            if not token:
                break
    return out


def select_contracts(rows: list[dict], close: pd.Series, expiries: set[dt.date]) -> pd.DataFrame:
    """Monthlies only, strikes within ±15% of the close ~45 days before expiry."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{"symbol": r["symbol"], "underlying": r["underlying_symbol"], "expiry": dt.date.fromisoformat(r["expiration_date"]),
                        "cp": "C" if r["type"] == "call" else "P", "strike": float(r["strike_price"]),
                        "open_interest": float(r["open_interest"]) if r.get("open_interest") else None,
                        "oi_date": r.get("open_interest_date"), "close_price": float(r["close_price"]) if r.get("close_price") else None,
                        "close_price_date": r.get("close_price_date"), "status": r["status"]} for r in rows if r.get("style", "american") == "american"])
    df = df[df["expiry"].isin(expiries)]
    if df.empty or close.empty:
        return df.iloc[0:0]
    keep = []
    for exp, g in df.groupby("expiry"):
        ref_day = pd.Timestamp(exp) - pd.Timedelta(days=45)
        px = close.loc[:ref_day]
        if px.empty:
            px = close.loc[:pd.Timestamp(exp)]
        if px.empty:
            continue
        s = float(px.iloc[-1])
        keep.append(g[(g["strike"] >= s * (1 - STRIKE_BAND)) & (g["strike"] <= s * (1 + STRIKE_BAND))])
    return pd.concat(keep) if keep else df.iloc[0:0]


def fetch_bars(symbols: list[str], start: str, end: str, key: str, secret: str) -> list[dict]:
    out, token = [], None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end, "limit": 10000}
        if token:
            p["page_token"] = token
        d = _get(BARS, p, key, secret)
        for sym, bars in (d.get("bars") or {}).items():
            for b in bars:
                out.append({"symbol": sym, "trade_date": pd.Timestamp(b["t"]).date(), "open": b["o"], "high": b["h"],
                            "low": b["l"], "close": b["c"], "volume": b["v"], "vwap": b.get("vw"), "n_trades": b.get("n")})
        token = d.get("next_page_token")
        if not token:
            return out


def backfill(start: dt.date, limit: int | None = None) -> dict:
    key, secret = keys_from_env("alpaca")
    with PanelStore(read_only=True) as store:
        names = [r[0] for r in store.con.execute("""SELECT DISTINCT ticker FROM membership WHERE eff_date >= ? ORDER BY 1""",
                                                 [start - dt.timedelta(days=45)]).fetchall()]
        closes = {t: s for t, s in store.bars_wide("close", start=(start - dt.timedelta(days=90)).isoformat()).items()}
    if limit:
        names = names[:limit]
    con = duckdb.connect(str(OPTIONS_DB))
    for s in DDL:
        con.execute(s)
    done = {r[0] for r in con.execute("SELECT underlying FROM opt_load_log").fetchall()}
    expiries = set(monthlies(start, dt.date.today()))
    stats = {"underlyings": 0, "contracts": 0, "bars": 0}
    for i, u in enumerate(names, 1):
        if u in done:
            continue
        sym = u.replace(".", "")                                  # BRK.B -> BRKB in OCC symbols
        try:
            rows = contracts_for(sym, start, key, secret)
        except urllib.error.HTTPError as e:                       # unknown / renamed underlying: skip, keep going
            print(f"  {u}: contracts {e.code}, skipped", flush=True)
            con.execute("INSERT OR REPLACE INTO opt_load_log VALUES (?, 0, 0, ?)", [u, pd.Timestamp.now()])
            continue
        close = closes.get(u, pd.Series(dtype=float)).dropna()
        sel = select_contracts(rows, close, expiries)
        n_bars = 0
        if not sel.empty:
            con.register("_c", sel.assign(oi_date=pd.to_datetime(sel["oi_date"]).dt.date,
                                          close_price_date=pd.to_datetime(sel["close_price_date"]).dt.date))
            con.execute("INSERT OR REPLACE INTO opt_contracts SELECT symbol, underlying, expiry, cp, strike, open_interest, oi_date, "
                        "close_price, close_price_date, status FROM _c")
            con.unregister("_c")
            for exp, g in sel.groupby("expiry"):
                syms = g["symbol"].tolist()
                for j in range(0, len(syms), BATCH):
                    try:
                        bars = fetch_bars(syms[j:j + BATCH], (exp - dt.timedelta(days=70)).isoformat(), exp.isoformat(), key, secret)
                    except urllib.error.HTTPError as e:
                        print(f"  {u} {exp}: bars {e.code}, batch skipped", flush=True)
                        continue
                    if bars:
                        con.register("_b", pd.DataFrame(bars))
                        con.execute("INSERT OR REPLACE INTO opt_bars SELECT * FROM _b")
                        con.unregister("_b")
                        n_bars += len(bars)
        con.execute("INSERT OR REPLACE INTO opt_load_log VALUES (?, ?, ?, ?)", [u, len(sel), n_bars, pd.Timestamp.now()])
        stats["underlyings"] += 1
        stats["contracts"] += len(sel)
        stats["bars"] += n_bars
        if i % 10 == 0:
            print(f"  [{i}/{len(names)}] {u}: {len(sel)} contracts, {n_bars} bars | total {stats}", flush=True)
    con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "status"])
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--limit-underlyings", type=int, default=None)
    args = ap.parse_args()
    if args.cmd == "backfill":
        print(backfill(dt.date.fromisoformat(args.start), args.limit_underlyings))
    else:
        con = duckdb.connect(str(OPTIONS_DB), read_only=True)
        print(con.execute("SELECT count(*) AS underlyings, sum(n_contracts) AS contracts, sum(n_bars) AS bars FROM opt_load_log").fetchall())
        print(con.execute("SELECT min(trade_date), max(trade_date), count(*) FROM opt_bars").fetchall())
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
