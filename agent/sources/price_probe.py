"""Measure a price vendor's delisted coverage before paying for it.

Same test for every provider: take N names that were listed in a past
quarter but are NOT S&P 500 members (panel.issuer_seen), ask the vendor
for that year's daily bars, and report what share comes back. yfinance
scored 44% on the 2016 sample (the misses are the acquired and failed
names), which is why a small-cap study needs a different source.

Providers (key passed on the command line, never stored):
  alpaca  data.alpaca.markets  (free account; APCA key + secret)
  tiingo  api.tiingo.com       (free token)
  eodhd   eodhd.com/api        (paid ~$20/mo; demo token works for AAPL only)

Usage:
  python -m agent.sources.price_probe --provider alpaca --key ID --secret SECRET
  python -m agent.sources.price_probe --provider tiingo --key TOKEN
  python -m agent.sources.price_probe --provider eodhd  --key TOKEN [--year 2016] [--n 200]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hedge_fund.features.panel import PanelStore


ENV_KEYS = {"alpaca": ("ALPACA_KEY_ID", "ALPACA_SECRET"), "tiingo": ("TIINGO_TOKEN", None),
            "eodhd": ("EODHD_TOKEN", None)}


def keys_from_env(provider: str) -> tuple[str, str]:
    """Read credentials from ~/.hedge-fund/.env so they never reach a shell history."""
    path = Path.home() / ".hedge-fund" / ".env"
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    key_name, sec_name = ENV_KEYS[provider]
    if key_name not in env:
        raise SystemExit(f"put {key_name}=... " + (f"and {sec_name}=... " if sec_name else "") +
                         f"in {path} (chmod 600), or pass --key")
    return env[key_name], env.get(sec_name or "", "")


def sample(store: PanelStore, year: int, n: int) -> list[str]:
    return store.con.execute("""
        SELECT DISTINCT ticker FROM issuer_seen
        WHERE quarter = ? AND ticker NOT IN (SELECT DISTINCT ticker FROM membership)
        ORDER BY random() LIMIT ?""", [f"{year}q1", n]).df()["ticker"].tolist()


def _get(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "optradar-agent"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:                      # timeout, DNS, TLS
        return 0, str(e)[:200]


def bars_alpaca(t: str, year: int, key: str, secret: str) -> int:
    q = urllib.parse.urlencode({"symbols": t, "timeframe": "1Day", "start": f"{year}-01-01",
                                "end": f"{year}-12-31", "limit": 400, "feed": "sip"})
    code, body = _get(f"https://data.alpaca.markets/v2/stocks/bars?{q}",
                      {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    if code != 200:
        q2 = q.replace("feed=sip", "feed=iex")
        code, body = _get(f"https://data.alpaca.markets/v2/stocks/bars?{q2}",
                          {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    if code != 200:
        return 0
    return len((json.loads(body).get("bars") or {}).get(t) or [])


def bars_tiingo(t: str, year: int, key: str) -> int:
    q = urllib.parse.urlencode({"startDate": f"{year}-01-01", "endDate": f"{year}-12-31", "token": key})
    code, body = _get(f"https://api.tiingo.com/tiingo/daily/{t}/prices?{q}")
    return len(json.loads(body)) if code == 200 and body.startswith("[") else 0


def bars_eodhd(t: str, year: int, key: str) -> int:
    q = urllib.parse.urlencode({"from": f"{year}-01-01", "to": f"{year}-12-31", "api_token": key,
                                "fmt": "json"})
    code, body = _get(f"https://eodhd.com/api/eod/{t}.US?{q}")
    return len(json.loads(body)) if code == 200 and body.startswith("[") else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["alpaca", "tiingo", "eodhd"], required=True)
    ap.add_argument("--key", default=None, help="omit to read from ~/.hedge-fund/.env")
    ap.add_argument("--secret", default="")
    ap.add_argument("--year", type=int, default=2016)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--pause", type=float, default=0.3)
    args = ap.parse_args()
    if not args.key:
        args.key, args.secret = keys_from_env(args.provider)

    with PanelStore(read_only=True) as store:
        names = sample(store, args.year, args.n)

    hits, misses = 0, []
    for i, t in enumerate(names, 1):
        if args.provider == "alpaca":
            n = bars_alpaca(t, args.year, args.key, args.secret)
        elif args.provider == "tiingo":
            n = bars_tiingo(t, args.year, args.key)
        else:
            n = bars_eodhd(t, args.year, args.key)
        hits += n > 150
        if n == 0:
            misses.append(t)
        if i % 25 == 0:
            print(f"  {i}/{len(names)} covered {hits}", flush=True)
        time.sleep(args.pause)
    print(f"{args.provider}: {hits}/{len(names)} ({hits / len(names):.0%}) of non-S&P names listed in "
          f"{args.year}q1 have >150 bars that year   [yfinance baseline: 44%]")
    print("first misses:", misses[:15])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
