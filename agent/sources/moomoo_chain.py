"""Archive ATM option chains into panel.chain_daily — the point-in-time IV record.

S2/S3 had to model IV as k x realized vol because there is no free option
history (DoltHub's SQL API is gone, Alpha Vantage's is premium). The only
way to get a real IV-vs-RV series is to record it from now on, which is
what this does: one snapshot per trading day of the ATM +- WIDTH strikes
on the 25-50 DTE monthly expiry, per ticker.

Run it BEFORE the close (15:45 ET): moomoo's after-hours bid/ask are stale
and anything derived from them inherits that. Open interest is published
overnight, so treat oi as belonging to the previous session.

Universe is deliberately small (optradar's watchlist + positions + the
agent's shadow picks, ~40 names): a full 500-name sweep costs 500 chain
calls plus 500 snapshots against moomoo's per-30s limits.

Usage: python -m agent.sources.moomoo_chain [--tickers A,B] [--width 8]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from hedge_fund.features.panel import PanelStore

sys.path.insert(0, "/Users/louis/optradar")
import yaml  # noqa: E402

from sources.moomoo_src import Moomoo, g  # noqa: E402

OPTRADAR = "/Users/louis/optradar"
AGENT_OUT = os.path.join(OPTRADAR, "out", "agent")


def universe(limit: int | None = None) -> list[str]:
    cfg = yaml.safe_load(open(os.path.join(OPTRADAR, "config.yaml")))
    wl = cfg["watchlist"]
    names = [t.split(".")[-1] for t in wl["core"] + wl["theme"] + wl.get("watch_only", [])]
    today = sorted(f for f in os.listdir(AGENT_OUT) if f.endswith(".json")) if os.path.isdir(AGENT_OUT) else []
    if today:
        import json
        for p in json.load(open(os.path.join(AGENT_OUT, today[-1])))["picks"]:
            names.append(p["ticker"])
    out = sorted(dict.fromkeys(names))
    return out[:limit] if limit else out


def archive(store: PanelStore, tickers: list[str], width: int = 8, dte_min: int = 25,
            dte_max: int = 50) -> dict:
    src = Moomoo()
    try:
        return _archive(src, store, tickers, width, dte_min, dte_max)
    finally:
        src.close()   # moomoo contexts keep non-daemon threads alive; without this the process hangs


def _archive(src, store: PanelStore, tickers: list[str], width: int, dte_min: int, dte_max: int) -> dict:
    health = src.health()
    snap_date = dt.date.today()
    rows, failed = [], {}
    snaps = src.snapshot([f"US.{t}" for t in tickers])
    for t in tickers:
        code = f"US.{t}"
        try:
            spot = g(snaps.get(code, {}), "last_price")
            if not spot:
                failed[t] = "no spot"
                continue
            expiry, dte = src.pick_expiry(code, dte_min, dte_max)
            if not expiry:
                failed[t] = "no expiry (throttled?)"
                continue
            strip = src.atm_quotes(code, spot, expiry, width=width)
            if not strip:
                failed[t] = "empty chain (throttled?)"
                continue
            for strike, cp in strip.items():
                for right, r in cp.items():
                    rows.append({"snap_date": snap_date, "ticker": t, "expiry": expiry, "strike": float(strike),
                                 "right": right, "bid": g(r, "bid_price"), "ask": g(r, "ask_price"),
                                 "last": g(r, "last_price"), "volume": g(r, "volume"),
                                 "oi": g(r, "option_open_interest"), "iv": g(r, "option_implied_volatility"),
                                 "delta": g(r, "option_delta"), "gamma": g(r, "option_gamma"),
                                 "theta": g(r, "option_theta"), "vega": g(r, "option_vega"),
                                 "spot": float(spot), "dte": int(dte), "snap_ts": pd.Timestamp.now(),
                                 "source": "moomoo"})
        except Exception as exc:                      # one bad ticker must not sink the sweep
            failed[t] = str(exc)[:80]
    n = store.insert("chain_daily", pd.DataFrame(rows))
    return {"tickers": len(tickers), "rows": n, "failed": failed, "market": health.get("market_us")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--width", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else universe(args.limit)
    with PanelStore() as store:
        print(archive(store, tickers, args.width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
