"""MoomooDataClient — satisfies aihf's hedge_fund.data.protocol.DataClient using
a local moomoo OpenD (read-only market data). No inheritance: duck-typed.

P1 scope (方案 A): built on the FAST, reliable endpoints — history kline,
market snapshot, news search. The slow/flaky fundamentals endpoints
(get_financials_statements / get_company_profile) are deliberately NOT used;
deep FinancialMetrics ratios are left None (agents' snapshot.render tolerates
None). Deepening fundamentals is P2.

Contract (from protocol.py): infrastructure failures RAISE; empty list / None
means the data genuinely doesn't exist. get_prices honors this (raises on a
non-OK return). Point-in-time note: snapshot-derived metrics reflect *today*,
so this client is for LIVE signals, not historical backtests — a backtest
needs point-in-time fundamentals (revisit before P4).
"""
from __future__ import annotations

import datetime as dt
import signal
import threading
import time

import moomoo as mm
from moomoo import RET_OK, OpenQuoteContext, KLType, AuType

from hedge_fund.data.models import (
    CompanyFacts, CompanyNews, Earnings, EarningsRecord,
    FinancialMetrics, InsiderTrade, Price,
)


def _alarm(sec):
    """SIGALRM watchdog. No-op off the main thread — signal handlers can only be
    installed there, and the pipeline runs these calls in a thread pool."""
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(sec)


def _disarm():
    if threading.current_thread() is threading.main_thread():
        signal.alarm(0)


# ---------------------------------------------------------------------------
# Process-wide shared cache + rate limiter.
#
# OpenD enforces undocumented rate limits: financials 30/30s, snapshot 60/30s.
# Five personas analysing the same ticker need the SAME fundamentals, so a
# per-(code, kind) cache collapses 5 identical fetches into 1 — which both
# removes the rate-limit pressure and makes the run much faster. The limiter
# stays as a backstop for cold caches / large universes.
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
_CACHE_TTL = 900.0  # 15 min: fundamentals don't move intraday
_CACHE_LOCK = threading.Lock()


class _RateLimiter:
    """Sliding-window limiter: at most `n` calls per `window` seconds."""

    def __init__(self, n: int, window: float = 30.0):
        self._n, self._w = n, window
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._hits = [t for t in self._hits if now - t < self._w]
                if len(self._hits) < self._n:
                    self._hits.append(now)
                    return
                sleep_for = self._w - (now - self._hits[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


# headroom under the documented ceilings
_LIMITS = {"financials": _RateLimiter(12), "snapshot": _RateLimiter(30),
           "kline": _RateLimiter(24), "news": _RateLimiter(8)}


_RATE_MSG = "频率太高"  # OpenD's rate-limit rejection (server-side, cross-process)


def _cached(key: tuple[str, str], producer, retries: int = 4):
    """Fetch once per (code, kind) within TTL; concurrent callers share it.

    OpenD counts rate limits server-side across ALL clients, so a local limiter
    alone can still be rejected (e.g. the daily radar querying in parallel).
    On a rate-limit rejection back off and retry — the limit is a 30s window,
    so waiting is always the right move.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    last: Exception | None = None
    for attempt in range(retries):
        _LIMITS[key[1]].acquire()
        try:
            val = producer()
        except Exception as exc:
            last = exc
            if _RATE_MSG not in str(exc):
                raise
            time.sleep(min(8.0 * (attempt + 1), 32.0))  # 30s window -> back off hard
            continue
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), val)
        return val
    raise last  # type: ignore[misc]


class MoomooError(RuntimeError):
    """Infrastructure failure talking to OpenD — must propagate, never swallowed."""


class MoomooDataClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 11111, call_timeout: int = 25):
        self._q = OpenQuoteContext(host=host, port=port)
        self._t = call_timeout

    def close(self):
        try:
            self._q.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ prices
    def get_prices(self, ticker: str, start_date: str, end_date: str, **kw) -> list[Price]:
        code = _norm(ticker)
        def fetch():
            try:
                _alarm(self._t)
                ret, df, _page = self._q.request_history_kline(
                    code, start=start_date, end=end_date,
                    ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
            except TimeoutError:
                raise MoomooError(f"get_prices timeout for {code}")
            finally:
                _disarm()
            if ret != RET_OK:
                raise MoomooError(f"request_history_kline failed for {code}: {df}")
            return df.to_dict("records") if df is not None and len(df) else []
        records = _cached((f"{code}|{start_date}|{end_date}", "kline"), fetch)
        out = []
        for r in records:
            out.append(Price(open=r["open"], close=r["close"], high=r["high"],
                             low=r["low"], volume=int(r["volume"]),
                             time=str(r["time_key"])[:10]))
        return out

    # -------------------------------------------------------------- fundamentals
    def _snapshot(self, code: str) -> dict:
        def fetch():
            try:
                _alarm(self._t)
                ret, df = self._q.get_market_snapshot([code])
            except TimeoutError:
                raise MoomooError(f"snapshot timeout for {code}")
            finally:
                _disarm()
            if ret != RET_OK:
                raise MoomooError(f"snapshot failed for {code}: {df}")
            return df.iloc[0].to_dict() if len(df) else {}
        return _cached((code, "snapshot"), fetch)

    # income-statement field_ids (stable across periods)
    _FID = {"revenue": 8001, "gross_profit": 8004,
            "operating_income": 8017, "net_income": 8037}

    def _financials(self, code: str) -> list[dict]:
        def fetch():
            try:
                _alarm(self._t)
                ret, d = self._q.get_financials_statements(code)  # income stmt, ~10 periods
            except TimeoutError:
                raise MoomooError(f"financials timeout for {code}")
            finally:
                _disarm()
            if ret != RET_OK:
                raise MoomooError(f"financials failed for {code}: {d}")
            return d.get("report_list", []) if isinstance(d, dict) else []
        return _cached((code, "financials"), fetch)

    def get_financial_metrics(self, ticker: str, end_date: str,
                              period: str = "ttm", limit: int = 10) -> list[FinancialMetrics]:
        code = _norm(ticker)
        reports = self._financials(code)
        if not reports:
            return []
        s = self._snapshot(code)  # valuation ratios only exist "now" -> latest period

        def num(v):
            try:
                f = float(v)
                return f if f == f else None
            except (TypeError, ValueError):
                return None

        out = []
        for i, rpt in enumerate(reports[:limit]):
            items = {it.get("field_id"): it for it in rpt.get("item_list", [])}
            def val(key):
                return num((items.get(self._FID[key]) or {}).get("data"))
            rev, gp, oi, ni = val("revenue"), val("gross_profit"), val("operating_income"), val("net_income")
            rev_yoy = num((items.get(self._FID["revenue"]) or {}).get("yoy"))
            m = FinancialMetrics(
                ticker=ticker,
                report_period=str(rpt.get("period_text") or rpt.get("date_time_str") or end_date),
                period=("annual" if rpt.get("financial_type") == "ANNUAL" else "quarterly"),
                gross_margin=(gp / rev) if (gp is not None and rev) else None,
                operating_margin=(oi / rev) if (oi is not None and rev) else None,
                net_margin=(ni / rev) if (ni is not None and rev) else None,
                revenue_growth=(rev_yoy / 100.0) if rev_yoy is not None else None,
                # valuation snapshot attaches to the most recent period only
                market_cap=num(s.get("total_market_val")) if i == 0 else None,
                price_to_earnings_ratio=(num(s.get("pe_ttm_ratio")) or num(s.get("pe_ratio"))) if i == 0 else None,
                price_to_book_ratio=num(s.get("pb_ratio")) if i == 0 else None,
                price_to_sales_ratio=(num(s.get("total_market_val")) / (rev * 4) if (i == 0 and rev) else None),
            )
            out.append(m)
        return out

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        s = self._snapshot(_norm(ticker))
        v = s.get("total_market_val")
        try:
            return float(v) if v and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        # name from snapshot (fast); sector/industry left None in P1 (profile endpoint is slow)
        s = self._snapshot(_norm(ticker))
        if not s:
            return None
        return CompanyFacts(ticker=ticker, is_active=True,
                            name=s.get("name") or None,
                            exchange=(ticker.split(".")[0] if "." in ticker else None))

    # --------------------------------------------------------------------- news
    def get_news(self, ticker: str, end_date: str, start_date: str | None = None,
                 limit: int = 1000) -> list[CompanyNews]:
        code = _norm(ticker)
        if not hasattr(self._q, "get_search_news"):
            return []
        _LIMITS["news"].acquire()
        try:
            _alarm(self._t)
            ret, df = self._q.get_search_news(code)
        except TimeoutError:
            raise MoomooError(f"news timeout for {code}")
        finally:
            _disarm()
        if ret != RET_OK:
            return []  # news absence is non-critical; treat as genuinely empty
        out = []
        for r in (df.to_dict("records") if df is not None and len(df) else [])[:limit]:
            d = str(r.get("news_time") or r.get("time") or "")[:10] or None
            if end_date and d and d > end_date:
                continue  # point-in-time guard
            out.append(CompanyNews(ticker=ticker, title=str(r.get("title") or ""),
                                   source=str(r.get("source") or "moomoo"),
                                   date=d, url=r.get("url") or None))
        return out

    # ---------------------------------------------- not wired in P1 (genuinely empty)
    def get_insider_trades(self, ticker, end_date, start_date=None, limit=1000) -> list[InsiderTrade]:
        return []

    def get_earnings(self, ticker: str) -> Earnings | None:
        return None

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        return []


def _norm(ticker: str) -> str:
    """AAPL -> US.AAPL; passthrough if already market-qualified."""
    t = ticker.strip().upper()
    return t if "." in t else f"US.{t}"


def prefetch(client: "MoomooDataClient", tickers: list[str], end_date: str) -> dict:
    """Warm the shared cache serially before a concurrent persona run.

    Turns N_personas x N_tickers fundamentals fetches into N_tickers — the
    single biggest reason runs hit OpenD's rate limits.
    """
    ok, failed = 0, {}
    for t in tickers:
        try:
            client.get_financial_metrics(t, end_date)
            client.get_company_facts(t)
            ok += 1
        except Exception as exc:
            failed[t] = str(exc)[:120]
    return {"warmed": ok, "failed": failed}
