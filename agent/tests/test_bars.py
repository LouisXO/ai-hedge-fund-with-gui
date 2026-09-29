"""Alpaca bars: failed requests are reported, class shares are spelled the vendor's way, one basis per symbol.

No network and no credentials: `Vendor` stands in for the bars endpoint through the module's `http`
argument, the panel is a temp DuckDB (S47 items 4 and 5, audit 2026-09-28).
"""
import datetime as dt
import json
import urllib.parse

import pandas as pd
import pytest

from agent.sources import alpaca_bars as ab
from hedge_fund.features.panel import PanelStore

TODAY = dt.date.today()
DAYS = [(TODAY - dt.timedelta(days=n)).isoformat() for n in range(12, 0, -1)]     # 12 days back .. yesterday
WINDOW = [d for d in DAYS if d >= (TODAY - dt.timedelta(days=7)).isoformat()]      # what update(days=7) asks for
OLD = pd.Timestamp("2026-09-20 12:00:00")
KEYS = ("k", "s")


class Vendor:
    """The bars endpoint as the module sees it: (url, headers) -> (status, body). Closes by vendor symbol
    and ISO date; a hyphen in any symbol refuses the whole request, as Alpaca does."""

    def __init__(self, raw: dict, adj: dict | None = None, page: int = 10000):
        self.raw, self.adj, self.page = raw, adj or raw, page
        self.calls: list[tuple] = []                 # (symbols, adjustment, start, page_token)
        self.script: list[int] = []                  # statuses answered first, one per call
        self.fail = lambda symbols, adjustment, start, token: None      # -> a status, or None to answer

    def __call__(self, url, headers):
        assert headers["APCA-API-KEY-ID"] == "k"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        syms = q["symbols"].split(",")
        self.calls.append((syms, q["adjustment"], q["start"], q.get("page_token")))
        if self.script:
            return self.script.pop(0), "scripted"
        if any("-" in s for s in syms):
            return 400, '{"message": "invalid symbol"}'
        code = self.fail(syms, q["adjustment"], q["start"], q.get("page_token"))
        if code is not None:
            return code, "failed"
        src = self.adj if q["adjustment"] == "all" else self.raw
        rows = [(s, d, c) for s in syms for d, c in sorted(src.get(s, {}).items()) if d >= q["start"]]
        at = int(q.get("page_token") or 0)
        bars: dict = {}
        for s, d, c in rows[at:at + self.page]:
            bars.setdefault(s, []).append({"t": f"{d}T04:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": 1000})
        nxt = str(at + self.page) if at + self.page < len(rows) else None
        return 200, json.dumps({"bars": bars, "next_page_token": nxt})


class Naps(list):
    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


def _list(store, symbols):
    store.insert("listing_status", pd.DataFrame({"symbol": symbols, "name": symbols, "exchange": "NYSE",
                                                 "asset_type": "Stock", "status": "Active"}))
    store.insert("issuer_seen", pd.DataFrame({"ticker": symbols, "cik": "1", "quarter": "2026q2"}))


def _stored(store, sym, rows, fetched_at=OLD, source="alpaca"):
    """rows: {date: (close, adj_close)}"""
    store.upsert_bars(pd.DataFrame([{"ticker": sym, "trade_date": pd.Timestamp(d).date(), "open": c, "high": c,
                                     "low": c, "close": c, "adj_close": a, "volume": 1000.0, "source": source,
                                     "fetched_at": fetched_at} for d, (c, a) in rows.items()]))


def _bars(store, sym):
    return store.con.execute("SELECT trade_date, close, adj_close, fetched_at FROM bars WHERE ticker = ? ORDER BY 1",
                             [sym]).df()


def _flat(px=10.0, days=DAYS):
    return {d: px for d in days}


# ---------------------------------------------------------------- requests -------------
def test_class_share_is_asked_in_the_vendor_spelling_and_stored_in_the_panel_spelling(store):
    v = Vendor({"BRK.A": _flat(700_000.0), "BRO": _flat(90.0)})
    st = ab.backfill(store, DAYS[0], DAYS[-1], symbols=["BRK-A", "BRO"], quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert v.calls[0][0] == ["BRK.A", "BRO"] and len(v.calls) == 2           # one raw, one adjusted; no 400
    assert st["with_data"] == 2 and st["failed_symbols"] == [] and st["n_batches_failed"] == 0
    assert len(_bars(store, "BRK-A")) == len(DAYS) and _bars(store, "BRK.A").empty
    assert ab.vendor_symbol("BRK.B") == "BRK.B" and ab.vendor_symbol("ABR-P-F") == "ABR-P-F"   # only class shares


def test_timeout_5xx_and_429_are_retried_with_backoff():
    v, naps = Vendor({"AAA": _flat()}), Naps()
    v.script = [0, 503, 429]
    bars, failed = ab.fetch_batch(["AAA"], DAYS[0], DAYS[-1], "k", "s", "raw", v, naps)
    assert len(bars["AAA"]) == len(DAYS) and failed == []
    assert len(v.calls) == 4 and naps == [2.0, 6.0, 60.0]                   # third retry, and a 429 waits longer


def test_a_refused_batch_is_halved_until_the_bad_symbol_stands_alone():
    syms = ["AAA", "ABR-P-F", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]      # a preferred share: no mapping, 400
    v, naps = Vendor({s: _flat() for s in syms}), Naps()
    bars, failed = ab.fetch_batch(syms, DAYS[0], DAYS[-1], "k", "s", "raw", v, naps)
    assert failed == ["ABR-P-F"] and sorted(bars) == sorted(s for s in syms if s != "ABR-P-F")
    assert len(v.calls) == 7 and naps == []                                  # 1 + 2 per halving; a 400 is not retried


def test_a_vendor_that_does_not_answer_costs_three_requests_not_two_hundred():
    syms = [f"S{i:03d}" for i in range(100)]
    v, naps = Vendor({s: _flat() for s in syms}), Naps()
    v.fail = lambda *a: 0
    bars, failed = ab.fetch_batch(syms, DAYS[0], DAYS[-1], "k", "s", "raw", v, naps)
    assert bars == {} and failed == syms
    assert len(v.calls) == 3 * (1 + ab.RETRIES)                              # the batch and its two halves, retried

    v = Vendor({s: _flat() for s in syms})
    v.fail = lambda *a: 403                                                  # the key or the plan: no halving
    assert ab.fetch_batch(syms, DAYS[0], DAYS[-1], "k", "s", "raw", v, Naps()) == ({}, syms) and len(v.calls) == 1


def test_a_5xx_caused_by_one_symbol_is_isolated():
    syms = ["AAA", "BBB", "CCC", "DDD"]
    v = Vendor({s: _flat() for s in syms})
    v.fail = lambda symbols, *a: 500 if "CCC" in symbols else None
    bars, failed = ab.fetch_batch(syms, DAYS[0], DAYS[-1], "k", "s", "raw", v, Naps())
    assert failed == ["CCC"] and sorted(bars) == ["AAA", "BBB", "DDD"]


def test_a_page_that_fails_fails_the_request_no_half_history(store):
    v = Vendor({"AAA": _flat(), "BBB": _flat()}, page=len(DAYS) // 2)        # every symbol needs a second page
    v.fail = lambda symbols, adjustment, start, token: 500 if token else None
    st = ab.backfill(store, DAYS[0], DAYS[-1], symbols=["AAA", "BBB"], quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert st["failed_symbols"] == ["AAA", "BBB"] and st["rows"] == 0        # the first pages alone are not stored
    assert store.con.execute("SELECT count(*) FROM bars").fetchone()[0] == 0


# ---------------------------------------------------------------- backfill -------------
def test_raw_close_is_never_stored_as_adj_close_when_the_adjusted_request_fails(store):
    v = Vendor({"AAA": _flat(), "BBB": _flat()})
    v.fail = lambda symbols, adjustment, start, token: 500 if adjustment == "all" else None
    st = ab.backfill(store, DAYS[0], DAYS[-1], symbols=["AAA", "BBB"], quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert st["rows"] == 0 and st["with_data"] == 0
    assert st["failed_symbols"] == ["AAA", "BBB"] and st["n_batches_failed"] == 1
    assert store.con.execute("SELECT count(*) FROM bars").fetchone()[0] == 0

    v = Vendor({"AAA": _flat(), "BBB": _flat()}, adj={"AAA": _flat(9.7)})    # adjusted answer without BBB
    st = ab.backfill(store, DAYS[0], DAYS[-1], symbols=["AAA", "BBB"], quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert st["failed_symbols"] == ["BBB"] and st["with_data"] == 1 and _bars(store, "BBB").empty
    assert _bars(store, "AAA")["adj_close"].tolist() == [9.7] * len(DAYS)


def test_an_outage_stops_the_run_and_every_symbol_is_reported(store, monkeypatch):
    monkeypatch.setattr(ab, "BATCH", 2)
    syms = [f"S{i}" for i in range(10)]                                      # 5 batches
    v = Vendor({s: _flat() for s in syms})
    v.fail = lambda *a: 0
    st = ab.backfill(store, DAYS[0], DAYS[-1], symbols=syms, quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert st["failed_symbols"] == syms and st["n_batches_failed"] == 5 and "aborted" in st
    assert len({tuple(c[0]) for c in v.calls if len(c[0]) == 2}) == ab.ABORT_AFTER   # batches 4 and 5 were not asked


def test_missing_symbols_are_listed_and_backfilled_from_2015(store):
    _list(store, ["AAA", "BRK-A", "BSX", "ZZZ"])
    _stored(store, "AAA", {d: (10.0, 10.0) for d in DAYS})
    _stored(store, "BSX", {d: (90.0, 90.0) for d in DAYS}, source="yfinance")   # an S&P name: yfinance rows only
    assert ab.missing_symbols(store) == ["BRK-A", "BSX", "ZZZ"]
    v = Vendor({"BRK.A": _flat(700_000.0), "BSX": _flat(91.0)})              # ZZZ: Alpaca does not serve it
    st = ab.backfill_missing(store, quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert v.calls[0][:3] == (["BRK.A", "BSX", "ZZZ"], "raw", "2015-01-01") and len(v.calls) == 2
    assert st["with_data"] == 2 and st["failed_symbols"] == [] and st["still_missing"] == 1
    assert set(store.con.execute("SELECT source FROM bars WHERE ticker = 'BSX'").df()["source"]) == {"alpaca"}


# ---------------------------------------------------------------- adjustment basis -----
def test_update_refetches_in_full_the_symbol_whose_factor_moved(store):
    """AAA went ex-dividend (3%) yesterday: the vendor now adjusts every earlier close. BBB did not."""
    _list(store, ["AAA", "BBB"])
    for s in ("AAA", "BBB"):
        _stored(store, s, {d: (10.0, 10.0) for d in DAYS[:-1]})               # through the day before yesterday
    v = Vendor({"AAA": _flat(), "BBB": _flat()},
               adj={"AAA": {**_flat(9.7), DAYS[-1]: 10.0}, "BBB": _flat()})
    st = ab.update(store, 7, http=v, sleep=Naps(), keys=KEYS)
    assert st["refetched"] == ["AAA"] and st["failed_symbols"] == []
    full = [c for c in v.calls if c[2] == "2015-01-01"]
    assert [c[:2] for c in full] == [(["AAA"], "raw"), (["AAA"], "all")]     # BBB is not asked again
    a = _bars(store, "AAA")
    assert a["adj_close"].tolist() == [9.7] * (len(DAYS) - 1) + [10.0]       # one basis; the ex-date rise is the vendor's
    assert a["fetched_at"].nunique() == 1 and a["fetched_at"].iloc[0] > OLD
    b = _bars(store, "BBB")
    assert len(b) == len(DAYS) and b["adj_close"].tolist() == [10.0] * len(DAYS)
    assert (b["fetched_at"] == OLD).sum() == len(DAYS) - len(WINDOW)         # older rows untouched
    assert ab.adjustment_breaks(store.con, DAYS[0]).empty


def test_update_writes_nothing_of_a_symbol_whose_full_refetch_fails(store):
    _list(store, ["AAA", "BBB"])
    for s in ("AAA", "BBB"):
        _stored(store, s, {d: (10.0, 10.0) for d in DAYS[:-1]})
    v = Vendor({"AAA": _flat(), "BBB": _flat()}, adj={"AAA": {**_flat(9.7), DAYS[-1]: 10.0}, "BBB": _flat()})
    v.fail = lambda symbols, adjustment, start, token: 500 if start == "2015-01-01" else None
    st = ab.update(store, 7, http=v, sleep=Naps(), keys=KEYS)
    assert st["refetched"] == [] and st["failed_symbols"] == ["AAA"] and st["n_batches_failed"] == 1
    a = _bars(store, "AAA")
    assert len(a) == len(DAYS) - 1 and (a["fetched_at"] == OLD).all() and (a["adj_close"] == 10.0).all()
    assert len(_bars(store, "BBB")) == len(DAYS)                             # the rest of the batch is stored
    v.fail = lambda *a: None                                                 # next evening: same difference, found again
    assert ab.update(store, 7, http=v, sleep=Naps(), keys=KEYS)["refetched"] == ["AAA"]


def test_update_gives_a_symbol_without_stored_rows_its_whole_history(store):
    _list(store, ["AAA", "NEW"])
    _stored(store, "AAA", {d: (10.0, 10.0) for d in DAYS[:-1]})
    v = Vendor({"AAA": _flat(), "NEW": _flat(5.0)})
    st = ab.update(store, 7, extra=["BRK.B"], http=v, sleep=Naps(), keys=KEYS)
    assert st["symbols"] == 3 and st["refetched"] == ["NEW"] and st["last_bar"] == pd.Timestamp(DAYS[-1]).date()
    assert len(_bars(store, "NEW")) == len(DAYS)                             # not the 7-day window alone


def test_breaks_are_told_from_the_vendors_own_ex_dates(store):
    t = [pd.Timestamp(f"2026-09-{d} 16:10:00") for d in (21, 22, 23)]
    # DIV: the row of 09-14 was fetched before the ex-date, the later ones after it -> false -3.4%
    _stored(store, "DIV", {"2026-09-14": (9.02, 9.02)}, t[0])
    _stored(store, "DIV", {"2026-09-15": (8.955, 8.654), "2026-09-16": (8.90, 8.601)}, t[1])
    # EXD: an ex-dividend date between two fetches: the factor rises, as it should
    _stored(store, "EXD", {"2026-09-14": (50.0, 49.5)}, t[0])
    _stored(store, "EXD", {"2026-09-15": (49.6, 49.6)}, t[1])
    # REV: a 1:25 reverse split on its ex-date: the factor falls 25 -> 1 and the raw close rises 22x
    _stored(store, "REV", {"2026-09-14": (0.3405, 8.5125)}, t[0])
    _stored(store, "REV", {"2026-09-15": (7.635, 7.635)}, t[1])
    # FWD: a 2:1 forward split on its ex-date: factor 0.5 -> 1, raw close halves
    _stored(store, "FWD", {"2026-09-14": (100.0, 50.0)}, t[0])
    _stored(store, "FWD", {"2026-09-15": (51.0, 51.0)}, t[1])
    # JMP: the older row was fetched before a 1:12 reverse split -> adj_close 0.529 -> 6.285, raw flat
    _stored(store, "JMP", {"2026-09-15": (0.529, 0.529)}, t[1])
    _stored(store, "JMP", {"2026-09-16": (0.498, 6.285)}, t[2])
    # HLF: the older row was fetched before a 2:1 forward split -> adj_close halves, raw flat
    _stored(store, "HLF", {"2026-09-15": (100.0, 100.0)}, t[1])
    _stored(store, "HLF", {"2026-09-16": (101.0, 50.5)}, t[2])
    # ONE: the same factor change inside one fetch is not a break between fetches
    _stored(store, "ONE", {"2026-09-15": (10.0, 10.0), "2026-09-16": (10.0, 9.7)}, t[2])
    # TOL: 0.04% is inside the tolerance
    _stored(store, "TOL", {"2026-09-15": (100.0, 100.0)}, t[1])
    _stored(store, "TOL", {"2026-09-16": (100.0, 99.96)}, t[2])
    br = ab.adjustment_breaks(store.con, "2026-09-14")
    assert [(r.ticker, str(r.trade_date)[:10], r.kind) for r in br.itertuples()] == [
        ("DIV", "2026-09-15", "drop"), ("HLF", "2026-09-16", "drop"), ("JMP", "2026-09-16", "jump")]
    assert br["change_pct"].round(2).tolist() == [-3.36, -50.0, 1162.05]
    assert ab.adjustment_breaks(store.con, "2026-09-16")["ticker"].tolist() == ["HLF", "JMP"]


def test_repair_refetches_every_symbol_with_a_break_in_one_request(store):
    early, late = pd.Timestamp("2026-09-21 16:10:00"), pd.Timestamp("2026-09-22 16:15:00")
    days = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16"]
    for s in ("DIV", "JMP", "FINE"):
        _stored(store, s, {d: (10.0, 10.0) for d in days[:3]}, early)
    _stored(store, "DIV", {d: (10.0, 9.7) for d in days[3:]}, late)
    _stored(store, "JMP", {d: (10.0, 120.0) for d in days[3:]}, late)
    _stored(store, "FINE", {d: (10.0, 10.0) for d in days[3:]}, late)
    _stored(store, "DIV", {"2026-09-12": (10.0, 10.0)}, early)               # a row the vendor no longer returns
    v = Vendor({s: _flat(10.0, days) for s in ("DIV", "JMP", "FINE")},
               adj={"DIV": _flat(9.7, days), "JMP": _flat(120.0, days), "FINE": _flat(10.0, days)})
    st = ab.repair_adjustments(store, "2026-09-14", end="2026-09-16", quiet=True, http=v, sleep=Naps(), keys=KEYS)
    assert [c[:3] for c in v.calls] == [(["DIV", "JMP"], "raw", "2015-01-01"), (["DIV", "JMP"], "all", "2015-01-01")]
    assert st["breaks_before"] == 2 and st["breaks_after"] == 0 and st["with_data"] == 2
    d = _bars(store, "DIV")
    assert d["adj_close"].tolist() == [9.7] * 5 and d["fetched_at"].nunique() == 1
    assert _bars(store, "JMP")["adj_close"].tolist() == [120.0] * 5
    assert (_bars(store, "FINE")["fetched_at"] <= late).all()                # not touched


# ---------------------------------------------------------------- audit ----------------
def test_audit_fails_on_a_break_and_passes_without_one(store):
    from agent.audit import Report, audit_prices
    check = "adjustment basis breaks between fetches"
    early, late = pd.Timestamp("2026-09-21 16:10:00"), pd.Timestamp("2026-09-22 16:15:00")
    _stored(store, "EXD", {"2026-09-14": (50.0, 49.5)}, early)                # the vendor's own ex-date
    _stored(store, "EXD", {"2026-09-15": (49.6, 49.6)}, late)
    rep = Report()
    audit_prices(store, rep)
    assert [r[2] for r in rep.rows if r[1] == check] == ["PASS"]
    _stored(store, "DIV", {"2026-09-14": (9.02, 9.02)}, early)
    _stored(store, "DIV", {"2026-09-15": (8.955, 8.654)}, late)
    rep = Report()
    audit_prices(store, rep)
    row = [r for r in rep.rows if r[1] == check][0]
    assert row[2] == "FAIL" and "DIV" in row[3] and "1 rows" in row[3]
