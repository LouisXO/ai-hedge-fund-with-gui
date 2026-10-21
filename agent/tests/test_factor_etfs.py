"""S48 (audit long_strategy #6): factor ETFs go to index_daily through their own function, off the order path."""
from __future__ import annotations

import os

import pandas as pd
import pytest

from agent.backfill import FACTOR_ETFS, INDEX_SYMBOLS, backfill_factor_etfs
from hedge_fund.features.panel import PanelStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_download(present):
    days = pd.bdate_range("2024-01-02", periods=5, name="Date")
    cols = pd.MultiIndex.from_product([present, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    df = pd.DataFrame(10.0, index=days, columns=cols)
    calls = []

    def dl(symbols, start, end):
        calls.append((tuple(symbols), start, end))
        return df
    return dl, calls


@pytest.fixture
def store(tmp_path):
    s = PanelStore(tmp_path / "panel.db")
    yield s
    s.close()


def test_writes_every_etf_to_index_daily_and_reports_missing(store):
    present = [s for s in FACTOR_ETFS if s != "AVUV"]
    dl, calls = _fake_download(present)
    out = backfill_factor_etfs(store, download=dl, end="2024-01-10")
    assert calls == [(FACTOR_ETFS, "2012-01-01", "2024-01-10")]           # one request, ETFs only
    assert out["missing"] == ["AVUV"] and out["rows"] == 5 * len(present)
    got = dict(store.con.execute("SELECT symbol, count(*) FROM index_daily GROUP BY 1").fetchall())
    assert got == {s: 5 for s in present}


def test_the_research_etfs_stay_out_of_the_production_index_refresh():
    assert INDEX_SYMBOLS == ("SPY", "^VIX", "IWM", "QQQ")
    assert not set(FACTOR_ETFS) & set(INDEX_SYMBOLS)
    for f in ("agent/execute.py", "agent/daily.py"):
        src = open(os.path.join(ROOT, f)).read()
        assert "backfill_factor_etfs" not in src and "FACTOR_ETFS" not in src, f


def _et_today():
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York")).date()


@pytest.mark.parametrize("closed, days", [(False, 0), (True, 1)])
def test_default_end_leaves_out_an_open_session(store, monkeypatch, closed, days):
    # yf's end is exclusive: during the session the default must stop at yesterday (no in-progress bar).
    import datetime as dt

    import agent.backfill as bf
    monkeypatch.setattr(bf, "session_closed", lambda now=None: closed)
    dl, calls = _fake_download(list(FACTOR_ETFS))
    backfill_factor_etfs(store, download=dl)
    assert calls[0][2] == (_et_today() + dt.timedelta(days=days)).isoformat()            # New York's date (download_end)


def test_nothing_downloaded_writes_nothing(store):
    for dl in (_fake_download([])[0], lambda symbols, start, end: pd.DataFrame()):   # yfinance can return an empty frame
        assert backfill_factor_etfs(store, download=dl, end="2024-01-10") == {"rows": 0, "symbols": {}, "missing": list(FACTOR_ETFS),
                                                                                 "refused": {}, "rescaled": {}}
    assert store.con.execute("SELECT count(*) FROM index_daily").fetchone()[0] == 0


def test_postclose_updates_the_factor_etfs_every_day_before_drift():
    # S54: the comment said "the weekly job", no job ever called it, and index_daily stopped at 2026-09-28
    lines = [l for l in open(os.path.join(ROOT, "agent/bin/postclose.sh")).read().splitlines() if l.startswith("$PY")]
    i = next(k for k, l in enumerate(lines) if "-m agent.backfill factors" in l)
    j = next(k for k, l in enumerate(lines) if "-m agent.drift" in l)
    assert i < j and "|| echo" in lines[i] and "non-fatal" in lines[i]


def test_the_factors_command_downloads_then_opens_the_panel(monkeypatch):
    import sys

    import agent.backfill as bf
    calls = []
    monkeypatch.setattr(bf, "backfill_factor_etfs", lambda: calls.append("etfs") or {"rows": 5, "symbols": {}, "missing": []})
    monkeypatch.setattr(bf, "PanelStore", lambda *a, **k: calls.append("panel"))     # the command opens nothing itself
    monkeypatch.setattr(sys, "argv", ["agent.backfill", "factors"])
    assert bf.main() == 0 and calls == ["etfs"]
    monkeypatch.setattr(bf, "backfill_factor_etfs", lambda: {"rows": 0, "symbols": {}, "missing": list(FACTOR_ETFS)})
    assert bf.main() == 1                                                           # nothing downloaded: the step says so


# ---------------------------------------------------------------- 2026-10-21: index cut, adjustment-basis guard ----
def _raw(spec: dict) -> pd.DataFrame:
    """A yf.download-shaped frame: spec = {symbol: [(day, close, adj_close)]}; a None close is a row yfinance left NaN."""
    days = sorted({pd.Timestamp(d) for rows in spec.values() for d, _, _ in rows})
    cols = pd.MultiIndex.from_product([list(spec), ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    df = pd.DataFrame(float("nan"), index=pd.DatetimeIndex(days, name="Date"), columns=cols)
    for s, rows in spec.items():
        for d, c, a in rows:
            df.loc[pd.Timestamp(d), [(s, "Open"), (s, "High"), (s, "Low"), (s, "Close"), (s, "Adj Close"), (s, "Volume")]] = \
                [c, c, c, c, a, 1.0]
    return df


DAYS = [str(d.date()) for d in pd.bdate_range("2026-10-12", periods=6)]


def _seed(store, sym="MTUM", f=0.99):
    store.upsert_index(pd.DataFrame([{"symbol": sym, "trade_date": pd.Timestamp(d).date(), "open": 100.0, "high": 100.0, "low": 100.0,
                                      "close": 100.0 + i, "adj_close": (100.0 + i) * f, "source": "yfinance", "fetched_at": pd.Timestamp("2026-10-16")}
                                     for i, d in enumerate(DAYS[:5])]))


def _stored(store, sym="MTUM"):
    return store.con.execute("SELECT trade_date, adj_close / close FROM index_daily WHERE symbol = ? ORDER BY 1", [sym]).fetchall()


@pytest.mark.parametrize("days, f, written", [
    (DAYS, 0.98, True),               # the whole history on a new basis (a distribution): every stored day rewritten
    (DAYS[1:], 0.99, True),           # starts a day late, same basis: the days it has are written
    (DAYS[1:], 0.98, False),          # starts a day late on a new basis: day 1 would stay on the old one
    (DAYS[:3], 0.98, False),          # ends before the stored last day on a new basis
    (DAYS[5:], 0.98, False),          # only a new day: nothing to compare the basis with
])
def test_a_partial_response_on_a_new_adjustment_basis_is_not_written(store, days, f, written):
    _seed(store)
    before = _stored(store)
    rows = [(d, 100.0 + DAYS.index(d), (100.0 + DAYS.index(d)) * f) for d in days]
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": rows, "QUAL": rows}), end="2026-10-20")
    assert ("MTUM" in out["refused"]) is (not written) and "QUAL" not in out["refused"]     # QUAL had nothing stored
    assert out["missing"] == [s for s in FACTOR_ETFS if s not in ("MTUM", "QUAL")]
    if written:
        assert [round(r, 6) for _, r in _stored(store)][-len(days):] == [f] * len(days)
    else:
        assert _stored(store) == before and "stored rows kept" in out["refused"]["MTUM"]


def test_a_day_dropped_inside_the_response_counts_as_left_out(store):
    _seed(store)
    before = _stored(store)
    rows = [(d, 100.0 + i, (100.0 + i) * 0.98) for i, d in enumerate(DAYS)]
    lead = [(d, None if d in DAYS[:1] else c, a) for d, c, a in rows]                   # NaN close: _frame drops it
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": lead}), end="2026-10-20")
    assert "1 stored day(s) (2026-10-12)" in out["refused"]["MTUM"] and _stored(store) == before
    assert "docs/RUNBOOK.md" in out["refused"]["MTUM"]                                 # says where the way out is written
    same = [(d, c, None if c is None else c * 0.99) for d, c, _ in lead]                 # the same gap on the stored basis
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": same}), end="2026-10-20")
    assert out["refused"] == {} and out["rescaled"] == {} and len(_stored(store)) == 6   # same basis: written, 10-12 kept


def test_a_stored_day_left_out_between_two_returned_days_is_rescaled_to_the_new_basis(store):
    # 2026-10-21 review: a day Yahoo stops returning must not refuse the symbol at every download after its next
    # distribution. Between two returned days on the same basis change, the left-out day takes that change.
    _seed(store)
    old = dict(store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'MTUM'").fetchall())
    rows = [(d, None if d == DAYS[2] else 100.0 + i, (100.0 + i) * 0.98) for i, d in enumerate(DAYS)]
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": rows}), end="2026-10-20")
    assert out["refused"] == {} and "1 stored day(s) the response left out (2026-10-14)" in out["rescaled"]["MTUM"]
    got = dict(store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'MTUM'").fetchall())
    assert [round(r, 9) for _, r in _stored(store)] == [0.98] * 6                       # one basis, the new day included
    d = [pd.Timestamp(x).date() for x in DAYS]
    assert got[d[2]] / got[d[1]] == pytest.approx(old[d[2]] / old[d[1]], rel=1e-12)     # no junction: the stored returns
    assert got[d[3]] / got[d[2]] == pytest.approx(old[d[3]] / old[d[2]], rel=1e-12)
    assert store.con.execute("SELECT close, fetched_at FROM index_daily WHERE symbol = 'MTUM' AND trade_date = ?",
                             [d[2]]).fetchone() == (102.0, pd.Timestamp("2026-10-16"))  # its close and fetch time as stored
    again = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": [(x, c, None if c is None else c * 0.97)
                                                                                for x, c, _ in rows]}), end="2026-10-20")
    assert again["refused"] == {} and "MTUM" in again["rescaled"]                      # the next distribution: no lock-in


def test_a_left_out_day_next_to_a_new_ex_date_is_still_refused(store):
    _seed(store)
    before = _stored(store)
    # the new distribution's ex-date is 10-15: days before it move by 0.98 / 0.99, from it on they do not
    rows = [(d, None if d == DAYS[2] else 100.0 + i, (100.0 + i) * (0.98 if i < 3 else 0.99)) for i, d in enumerate(DAYS)]
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": rows}), end="2026-10-20")
    assert "MTUM" in out["refused"] and out["rescaled"] == {} and _stored(store) == before
    rows = [(d, None if d in (DAYS[0], DAYS[2]) else 100.0 + i, (100.0 + i) * 0.98) for i, d in enumerate(DAYS)]
    out = backfill_factor_etfs(store, download=lambda s, a, b: _raw({"MTUM": rows}), end="2026-10-20")
    assert "2 stored day(s) (2026-10-12, 2026-10-14)" in out["refused"]["MTUM"] and _stored(store) == before   # one can't be


def test_the_factors_command_fails_the_step_when_an_etf_was_refused(monkeypatch, capsys):
    import sys

    import agent.backfill as bf
    from agent import health
    monkeypatch.setattr(bf, "backfill_factor_etfs", lambda: {"rows": 5, "symbols": {}, "missing": [], "refused": {"MTUM": "x"}})
    monkeypatch.setattr(sys, "argv", ["agent.backfill", "factors"])
    assert bf.main() == 2                                                       # written but one refused: postclose.sh says "partly"
    line = next(l for l in capsys.readouterr().out.splitlines() if "MTUM" in l and "not written" in l)
    assert health.section_failures([line])[0] == "warn"                         # the job check sees it


@pytest.mark.parametrize("closed, days", [(False, 0), (True, 1)])
def test_the_index_refresh_leaves_out_an_open_session_too(store, monkeypatch, closed, days):
    import datetime as dt

    import agent.backfill as bf
    monkeypatch.setattr(bf, "session_closed", lambda now=None: closed)
    calls = []
    bf.backfill_index(store, download=lambda s, a, b: calls.append((tuple(s), a, b)) or pd.DataFrame())
    assert calls == [(INDEX_SYMBOLS, "2012-01-01", (_et_today() + dt.timedelta(days=days)).isoformat())]


def test_every_scheduled_index_refresh_runs_after_the_cut():
    import datetime as dt
    from zoneinfo import ZoneInfo

    import agent.backfill as bf
    et = ZoneInfo("America/New_York")
    for pt in ((13, 25), (16, 10)):                       # postclose and execute: PT is ET - 3 all year, they get today's bar
        assert bf.session_closed(dt.datetime(2026, 10, 21, pt[0] + 3, pt[1], tzinfo=et))
        assert bf.session_closed(dt.datetime(2026, 12, 21, pt[0] + 3, pt[1], tzinfo=et))
    assert not bf.session_closed(dt.datetime(2026, 10, 21, 11, 41, tzinfo=et))          # the 08:41 brief: yesterday's bar only
    assert not bf.session_closed(dt.datetime(2026, 10, 21, 16, 10, tzinfo=et))


def test_download_end_takes_new_yorks_date_with_its_clock():
    # 2026-10-21 review: from 21:00 PT the PT date + 0 left out the session that had just closed
    import datetime as dt
    from zoneinfo import ZoneInfo

    import agent.backfill as bf
    pt, et = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")
    at = lambda *a: bf.download_end(dt.datetime(*a, tzinfo=pt))                       # noqa: E731
    assert at(2026, 10, 21, 8, 41) == "2026-10-21"              # the brief, session open: through 10-20
    assert at(2026, 10, 21, 13, 25) == at(2026, 10, 21, 16, 10) == "2026-10-22"       # postclose, execute: 10-21 included
    assert at(2026, 10, 21, 21, 30) == "2026-10-22"             # a late re-run (00:30 ET on the 22nd): 10-21 still included
    assert at(2026, 10, 22, 0, 30) == "2026-10-22"              # after midnight PT: through 10-21, the 22nd not open yet
    assert at(2026, 10, 23, 21, 30) == "2026-10-25"             # Friday evening (Saturday in New York): Friday included
    assert at(2026, 12, 21, 21, 30) == "2026-12-22"             # PST: the same
    assert bf.download_end(dt.datetime(2026, 10, 22, 0, 30)) == "2026-10-22"          # a naive time is New York's
    assert bf.download_end(dt.datetime(2026, 10, 22, 0, 30, tzinfo=et)) == at(2026, 10, 21, 21, 30)


def test_the_index_refresh_prints_a_refusal_and_writes_the_rest_without_raising(store, capsys):
    import agent.backfill as bf
    _seed(store, "SPY")
    rows = [(d, 100.0 + DAYS.index(d), (100.0 + DAYS.index(d)) * 0.98) for d in DAYS[2:]]
    n = bf.backfill_index(store, download=lambda s, a, b: _raw({"SPY": rows, "IWM": rows}))
    line = next(l for l in capsys.readouterr().out.splitlines() if "index SPY" in l)
    assert n == len(rows) and "index SPY: update failed, not written" in line
    from agent import health
    assert health.section_failures([line])[0] == "warn"                                # execute / postclose job checks warn
    assert [round(r, 6) for _, r in _stored(store, "SPY")] == [0.99] * 5 and len(_stored(store, "IWM")) == len(rows)
    assert bf.backfill_index(store, download=lambda s, a, b: pd.DataFrame()) == 0     # nothing returned: no concat error
