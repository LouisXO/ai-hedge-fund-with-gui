"""S54 insider all-event shadow, on synthetic data only: the SPRT's boundaries, sigma re-estimate, truncation and
decisions; the visibility rule; one event per ticker per 10 sessions; the persistent store; readings kept hidden."""
import copy
import datetime as dt
import json
import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from agent import insider_shadow as sh, ledger
from hedge_fund.features.panel import PanelStore

PT, ET = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")
CFG = sh.config()
SP = CFG["sprt"]
SESSIONS = [d.date() for d in pd.bdate_range("2026-09-01", "2026-11-30")]
TICKERS = ["AAA", "BBB", "CCC", "EEE", "FFF", "GGG", "WWW", "SMALL", "CLU", "QQQQ"]
D = dt.date


def pt(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=PT)


# ------------------------------------------------------------------ SPRT ----
def test_boundaries_match_the_registration():
    lo, up = sh.boundaries(1, 5.95, SP)
    assert up == pytest.approx(0.135 + 363.5, abs=0.1) and lo == pytest.approx(0.135 - 204.3, abs=0.1)
    lo, up = sh.boundaries(1200, 5.95, SP)
    assert round(up, 2) == 0.44 and round(lo, 3) == -0.035                     # the registration's n = 1,200 example
    k = 5.95 ** 2 / 0.27
    assert k * math.log(16) == pytest.approx(363.5, abs=0.2) and k * math.log(1 / 0.2105) == pytest.approx(204.3, abs=0.2)


def test_h1_and_h0_decide_at_the_first_crossing_with_the_prior_sigma():
    r = sh.sprt(np.full(300, 2.0), SP)                    # 2.0 >= 0.135 + 363.5/n from n = 195, before the re-estimate
    assert r["decision"]["decision"] == "H1" and r["decision"]["n"] == 195 and not r["decision"]["truncated"]
    assert r["decision"]["sigma_pct"] == 5.95
    assert r["current"]["n"] == 195 and r["current"]["n_after_stop"] == 105      # stopped: later events do not move it
    r = sh.sprt(np.full(300, -1.5), SP)                   # -1.5 <= 0.135 - 204.3/n from n = 125
    assert r["decision"]["decision"] == "H0" and r["decision"]["n"] == 125
    assert sh.sprt(np.full(50, 0.5), SP)["decision"] is None


def test_sigma_is_the_sample_sd_of_the_first_200_from_event_200_and_then_frozen():
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 1.0, 400)                         # sd 1: far below the prior's 5.95, boundaries shrink at 200
    r = sh.sprt(x[:199], SP)
    assert r["current"]["sigma_pct"] == 5.95 and r["sigma200"] is None and r["current"]["sigma_source"] == "prior"
    r = sh.sprt(x, SP)
    s200 = float(np.std(x[:200], ddof=1))
    assert r["sigma200"] == pytest.approx(s200)
    if r["decision"] is not None:                         # a crossing from 200 on uses the re-estimate
        assert r["decision"]["n"] >= 200 and r["decision"]["sigma_pct"] == pytest.approx(s200)
    frozen = sh.sprt(x, SP, {"sigma200": 9.9})            # the recorded value wins over a recomputation
    assert frozen["sigma200"] == 9.9
    dec = {"decision": "H0", "n": 7, "mean_pct": -1, "lower_pct": -2, "upper_pct": 3, "sigma_pct": 5.95, "truncated": False}
    assert sh.sprt(x, SP, {"decision": dec})["decision"] == dec


def test_truncation_decides_on_the_mean_against_the_midpoint():
    sp = {**SP, "truncate_n": 50, "sigma_reestimate_n": 1000}       # prior sigma throughout: no boundary is reached by 50
    up = np.tile([1.2, -0.8], 40)                                   # mean 0.2 >= 0.135
    r = sh.sprt(up, sp)
    assert r["decision"]["truncated"] and r["decision"]["decision"] == "H1" and r["decision"]["n"] == 50
    r = sh.sprt(np.tile([1.1, -0.9], 40), sp)                       # mean 0.1 < 0.135
    assert r["decision"]["truncated"] and r["decision"]["decision"] == "H0"
    assert sh.sprt(np.tile([1.2, -0.8], 20), sp)["decision"] is None      # 40 events: not yet at the truncation


# ------------------------------------------------------------------ visibility / dedupe ----
def test_cutoffs_for_a_monday_open():
    rt, other = sh.cutoffs(D(2026, 10, 26))
    assert rt == dt.datetime(2026, 10, 26, 9, 25, tzinfo=ET)                     # orders go out until 09:25 ET
    assert other == dt.datetime(2026, 10, 23, 19, 15, tzinfo=PT)                 # the Friday 19:15 late pass
    sql, params = sh.visible_filter(D(2026, 10, 26), D(2026, 10, 21), has_stored_at=False)
    assert "stored_at" not in sql and "fetched_at" in sql and params[0] == D(2026, 10, 21)


def test_one_event_per_ticker_per_ten_sessions():
    pos = {d: i for i, d in enumerate(SESSIONS)}
    cands = pd.DataFrame([{"ticker": "AAA", "trigger_filing_day": D(2026, 10, 28), "lag_sessions": 0, "kind": "big_usd",
                           "n_buyers": 1, "buy_usd": 3e5, "adv20": 1e7}])
    last = {"AAA": D(2026, 10, 21)}
    rows, rep = sh.new_events(cands, D(2026, 10, 28), D(2026, 10, 29), last, pos, 10, dt.datetime.now())
    assert rows == [] and rep == 1 and last["AAA"] == D(2026, 10, 21)            # 5 sessions after its event: a repeat
    rows, rep = sh.new_events(cands, D(2026, 11, 4), D(2026, 11, 5), last, pos, 10, dt.datetime.now())
    assert len(rows) == 1 and rep == 0 and last["AAA"] == D(2026, 11, 4)         # 10 sessions after: a new event


# ------------------------------------------------------------------ end to end on a synthetic panel ----
def _bars(t: str, d: dt.date, o: float, c: float, factor: float = 1.0, vol: float = 1e6) -> dict:
    return {"ticker": t, "trade_date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
            "adj_close": c * factor, "volume": vol, "source": "test", "fetched_at": pd.Timestamp.now()}


def _tx(t: str, filed: str, usd: float, stored, source: str = "edgar_realtime", owner: str = "o", price: float = 10.0) -> dict:
    return {"accession": f"{t}-{filed}-{owner}-{usd}", "ticker": t, "issuer_cik": "1", "filing_date": pd.Timestamp(filed).date(),
            "trans_date": pd.Timestamp(filed).date(), "owner_name": owner, "relationship": "isOfficer", "officer_title": "CEO",
            "trans_code": "P", "acq_disp": "A", "shares": usd / price, "price": price, "value_usd": float(usd),
            "shares_after": None, "source": source, "fetched_at": stored.tz_convert(PT).tz_localize(None), "stored_at": stored}


TXS = [
    _tx("AAA", "2026-10-21", 300_000, pt("2026-10-21 16:12")),                   # on time: event 10/21, entry 10/22
    _tx("CCC", "2026-10-21", 300_000, pt("2026-10-22 05:00")),                   # the late pass ran past midnight: still 10/21
    _tx("EEE", "2026-10-21", 300_000, pt("2026-10-22 07:00")),                   # stored after the 10/22 open: event 10/22 (late)
    _tx("BBB", "2026-10-21", 300_000, pt("2026-10-22 06:00"), "edgar_daily"),   # 06:00 index: seen at 16:10 -> event 10/22
    _tx("FFF", "2026-10-21", 300_000, pt("2026-10-26 06:00"), "edgar_daily"),   # first seen on 10/26: outside the window, never
    _tx("GGG", "2026-10-20", 300_000, pt("2026-10-20 16:15")),                   # filed before the first event day: not read
    _tx("WWW", "2026-10-23", 300_000, pt("2026-10-24 10:00"), "edgar_daily"),   # stored Saturday: Monday 16:10 -> event 10/26
    _tx("SMALL", "2026-10-21", 100_000, pt("2026-10-21 16:12")),                 # one buyer, $100k: does not qualify
    _tx("CLU", "2026-10-22", 60_000, pt("2026-10-22 16:12"), owner="x"),         # two buyers that day: a cluster -> event 10/22
    _tx("CLU", "2026-10-22", 60_000, pt("2026-10-22 16:12"), owner="y"),
    _tx("AAA", "2026-10-28", 300_000, pt("2026-10-28 16:12")),                   # 5 sessions after AAA's event: a repeat
    _tx("AAA", "2026-11-04", 300_000, pt("2026-11-04 16:12")),                   # 10 sessions after: a new event
]
EXPECTED = {("AAA", D(2026, 10, 21)), ("CCC", D(2026, 10, 21)), ("EEE", D(2026, 10, 22)), ("BBB", D(2026, 10, 22)),
            ("CLU", D(2026, 10, 22)), ("WWW", D(2026, 10, 26)), ("AAA", D(2026, 11, 4))}


@pytest.fixture
def world(tmp_path):
    panel = tmp_path / "panel.db"
    with PanelStore(panel) as s:
        rows = []
        for d in SESSIONS:
            for t in TICKERS:
                o = c = 10.0
                k = 1.0
                if t == "AAA" and d == D(2026, 10, 22):
                    c = 10.5                                                     # E1 of AAA's first event: +5%
                if t == "AAA" and d == D(2026, 10, 28):
                    c = 11.0                                                     # E5c: close 10/28 = 11 -> +10%
                if t == "AAA" and d == D(2026, 10, 29):
                    o, c = 12.0, 12.0                                            # E6o: open 10/29 = 12 -> +20%
                if t == "CCC":                                                   # 2-for-1 split on 10/27, adjusted prices flat
                    o = c = 10.0 if d < D(2026, 10, 27) else 5.0
                    k = 0.5 if d < D(2026, 10, 27) else 1.0
                rows.append(_bars(t, d, o, c, k))
        s.upsert_bars(pd.DataFrame(rows))
        idx = []
        for d in SESSIONS:
            for sym, o, c in (("SPY", 200.0, 201.0 if d == D(2026, 10, 22) else 200.0),
                              ("IWM", 100.0, 101.0 if d == D(2026, 10, 22) else 100.0)):
                idx.append({"symbol": sym, "trade_date": d, "open": o, "high": c, "low": o, "close": c, "adj_close": c,
                            "source": "test", "fetched_at": pd.Timestamp.now()})
        s.upsert_index(pd.DataFrame(idx))
        s.insert("listing_status", pd.DataFrame([{"symbol": t, "name": t, "exchange": "NYSE", "asset_type": "Stock",
                                                  "ipo_date": D(2010, 1, 4), "delisting_date": None, "status": "Active",
                                                  "fetched_at": pd.Timestamp.now()} for t in TICKERS]))
        s.con.execute("CREATE TABLE spread_grid (bucket VARCHAR, year INT, median_spread_pct DOUBLE)")
        s.insert("insider_tx", pd.DataFrame(TXS))
    db = tmp_path / "ledger.db"
    con = ledger.connect(str(db))
    ledger.ensure_schema(con)
    con.close()
    return {"panel": str(panel), "db": str(db), "out": str(tmp_path / "shadow.json")}


def _events(db: str) -> pd.DataFrame:
    con = ledger.connect(db, read_only=True)
    try:
        df = con.execute("SELECT * FROM agent_insider_shadow ORDER BY event_day, ticker").df()
    finally:
        con.close()
    df["event_day"] = pd.to_datetime(df["event_day"]).dt.date
    return df


def test_events_follow_the_visibility_rule_and_the_window(world):
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    ev = _events(world["db"])
    assert set(zip(ev["ticker"], ev["event_day"])) == EXPECTED
    got = ev.set_index(["ticker", "event_day"])
    assert got.loc[("EEE", D(2026, 10, 22)), "lag_sessions"] == 1 and got.loc[("BBB", D(2026, 10, 22)), "lag_sessions"] == 1
    assert got.loc[("CLU", D(2026, 10, 22)), "kind"] == "cluster"
    assert pd.Timestamp(got.loc[("WWW", D(2026, 10, 26)), "entry_day"]).date() == D(2026, 10, 27)
    assert rep["n_events"] == 7 and rep["evenings_processed"] == len([d for d in SESSIONS if d >= D(2026, 10, 21)]) - 1


def test_returns_are_on_the_stocks_window_against_iwm_and_splits_are_adjusted(world):
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    a = _events(world["db"]).set_index(["ticker", "event_day"])
    first = a.loc[("AAA", D(2026, 10, 21))]
    assert first["e1_ret_pct"] == pytest.approx(5.0) and first["e1_iwm_pct"] == pytest.approx(1.0)
    assert first["e1_spy_pct"] == pytest.approx(0.5)
    assert first["e1_ew_pct"] == pytest.approx(5.0 / len(TICKERS))              # the band's equal weight, same window
    assert first["e5c_ret_pct"] == pytest.approx(10.0) and first["e6o_ret_pct"] == pytest.approx(20.0)
    assert first["e5c_iwm_pct"] == pytest.approx(0.0)                            # IWM open 10/22 -> close 10/28: flat
    c = a.loc[("CCC", D(2026, 10, 21))]
    assert c["e5c_ret_pct"] == pytest.approx(0.0) and c["e10_ret_pct"] == pytest.approx(0.0)   # the split is not a loss
    assert pd.Timestamp(first["e10_day"]).date() == D(2026, 11, 4)


def test_the_store_is_idempotent_and_frozen_against_later_revisions(world):
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    before = _events(world["db"])
    with PanelStore(world["panel"]) as s:
        s.upsert_bars(pd.DataFrame([_bars("AAA", D(2026, 10, 22), 10.0, 20.0)]))          # a bar revised after E1 was written
        s.insert("insider_tx", pd.DataFrame([_tx("QQQQ", "2026-10-21", 400_000, pt("2026-10-21 16:30"))]))   # a late backfill
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    after = _events(world["db"])
    cols = [c for c in before.columns if not c.endswith("_at")]
    pd.testing.assert_frame_equal(before[cols], after[cols])
    con = ledger.connect(world["db"], read_only=True)
    try:
        n_days = con.execute("SELECT count(*), count(DISTINCT event_day) FROM agent_insider_shadow_days").fetchone()
    finally:
        con.close()
    assert n_days[0] == n_days[1]


def test_readings_stay_hidden_until_due_and_then_are_read_once(world, tmp_path):
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    text = open(world["out"]).read()
    assert rep["exit_readings"]["status"] == "hidden until 2027-06-30"
    for word in ("E5c_minus", "E10_minus", "a_v3", "b_v3", "c_v3"):
        assert word not in text
    assert set(rep["exit_readings"]["counts"]) == {"E1", "E5c", "E6o", "E10"}
    assert "E5c" not in sh.line(rep).replace("exit readings", "")
    cfg = copy.deepcopy(CFG)
    cfg["insider_shadow"]["reading_date"] = D(2026, 11, 2)            # events entered by 11/02, all ten sessions in by 11/30
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), cfg=cfg, panel=world["panel"])
    rr = rep["exit_readings"]
    assert rr["status"] == "read" and rr["n_events"] == 6 and "a_E5c_minus_E6o" in rr
    again = sh.run(world["db"], world["out"], today=D(2026, 12, 1), cfg=cfg, panel=world["panel"])
    assert again["exit_readings"]["read_on"] == rr["read_on"]           # read once, then frozen


def test_a_due_reading_waits_for_the_exits_of_its_batch(world):
    cfg = copy.deepcopy(CFG)
    cfg["insider_shadow"]["reading_date"] = D(2026, 11, 20)             # AAA's 11/05 entry has E10 on 11/18, settled 11/19
    with PanelStore(world["panel"]) as s:
        s.con.execute("DELETE FROM bars WHERE trade_date > '2026-11-17'")
        s.con.execute("DELETE FROM index_daily WHERE trade_date > '2026-11-17'")
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 20), cfg=cfg, panel=world["panel"])
    assert rep["exit_readings"]["status"] == "due, waiting for exits"
    assert "a_E5c_minus_E6o" not in open(world["out"]).read()


def test_a_failed_run_keeps_the_last_report_and_says_so(world, monkeypatch, capsys):
    assert sh.main(["--db", world["db"], "--out", world["out"], "--panel", world["panel"]]) == 0
    monkeypatch.setattr(sh, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("panel locked")))
    assert sh.main(["--db", world["db"], "--out", world["out"], "--panel", world["panel"]]) == 1
    rep = json.load(open(world["out"]))
    assert "panel locked" in rep["error"] and "sprt" in rep
    assert "FAILED" not in capsys.readouterr().out                      # a record, not a book-feeding step


# ------------------------------------------------------------------ registration / first run ----
def test_the_config_holds_the_registered_numbers():
    s = CFG["insider_shadow"]
    assert str(s["first_event_day"]) == "2026-10-21" and s["dedupe_sessions"] == 10 and s["window_sessions"] == 2
    assert {k: tuple(v) for k, v in s["exits"].items()} == {"E1": (1, "close"), "E5c": (5, "close"), "E6o": (6, "open"),
                                                             "E10": (10, "close")}
    assert s["benchmark"] == "IWM" and str(s["reading_date"]) == "2027-06-30"
    assert (SP["h0_pct"], SP["h1_pct"], SP["alpha"], SP["beta"], SP["sigma_prior_pct"], SP["sigma_reestimate_n"],
            SP["truncate_n"]) == (0.0, 0.27, 0.05, 0.20, 5.95, 200, 2400)
    rd = CFG["exit_readings"]
    assert (rd["a_abs_max_pp"], rd["b_max_pp"], rd["c_min_pp"], rd["c_min_t"]) == (0.10, 0.0, 0.30, 2.0)
    from agent.signals_insider import candidates                                     # the live rule's own window
    import inspect
    from agent import execute
    assert inspect.signature(execute.insider_targets).parameters["window"].default == s["window_sessions"]
    assert candidates is sh.signals_insider.candidates


def test_a_first_run_with_nothing_to_read_writes_an_empty_report(tmp_path):
    panel = tmp_path / "panel.db"
    with PanelStore(panel) as s:
        s.upsert_index(pd.DataFrame([{"symbol": "SPY", "trade_date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                      "adj_close": 1.0, "source": "t", "fetched_at": pd.Timestamp.now()}
                                     for d in SESSIONS if d <= D(2026, 10, 20)]))      # nothing after the first event day yet
    db = str(tmp_path / "ledger.db")
    con = ledger.connect(db)
    ledger.ensure_schema(con)
    con.close()
    rep = sh.run(db, str(tmp_path / "s.json"), today=D(2026, 10, 21), panel=str(panel))
    assert rep["n_events"] == 0 and rep["evenings_processed"] == 0 and rep["last_evening"] is None
    assert rep["sprt"]["decision"] == "none" and rep["sprt"]["n"] == 0 and rep["sprt"]["boundary_crossed"] is False
    assert rep["exit_readings"]["status"] == "hidden until 2027-06-30"
    assert "no E1 yet" in sh.line(rep) and json.load(open(tmp_path / "s.json"))["error"] is None


def test_a_crossing_is_reported_with_its_event_and_day_and_then_frozen(world):
    cfg = copy.deepcopy(CFG)
    cfg["sprt"]["sigma_prior_pct"] = 0.5                         # upper boundary at n: 0.135 + 0.926 ln 16 / n = 0.135 + 2.567 / n
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), cfg=cfg, panel=world["panel"])
    s = rep["sprt"]
    assert s["decision"] == "H1" and s["boundary_crossed"] is True                # the 10/22 entries: AAA +4%, CCC -1%:
    assert s["decided"]["n"] == 2 and s["decided"]["entry_day"] == "2026-10-22"   # mean 1.5% >= 1.42% at the day's end
    assert s["decided"]["last_event"] == ["CCC", "2026-10-21"] and s["n_after_stop"] == rep["n_with_E1"] - 2
    assert s["clustered_by_date"]["n"] == rep["n_with_E1"]                        # the date-clustered t covers every E1
    rep = sh.run(world["db"], world["out"], today=D(2026, 12, 1), panel=world["panel"])   # the registered sigma: no crossing
    assert rep["sprt"]["decision"] == "H1" and rep["sprt"]["decided"]["entry_day"] == "2026-10-22"   # frozen when first made


def test_an_event_older_than_the_revisit_window_gets_no_late_exit(world, monkeypatch):
    with PanelStore(world["panel"]) as s:                       # AAA's 10/22 bar is missing when the runs happen
        s.con.execute("DELETE FROM bars WHERE ticker = 'AAA' AND trade_date = '2026-10-22'")
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    a = _events(world["db"]).set_index(["ticker", "event_day"]).loc[("AAA", D(2026, 10, 21))]
    assert pd.isna(a["e1_ret_pct"])
    with PanelStore(world["panel"]) as s:                       # the bar arrives later
        s.upsert_bars(pd.DataFrame([_bars("AAA", D(2026, 10, 22), 10.0, 10.5)]))
    monkeypatch.setattr(sh, "REVISIT_SESSIONS", 20)             # 10/22 is 27 sessions back by 11/30: no longer looked for
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    a = _events(world["db"]).set_index(["ticker", "event_day"]).loc[("AAA", D(2026, 10, 21))]
    assert pd.isna(a["e1_ret_pct"])
    monkeypatch.setattr(sh, "REVISIT_SESSIONS", 30)             # within the window (a run missed for days): it catches up
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    a = _events(world["db"]).set_index(["ticker", "event_day"]).loc[("AAA", D(2026, 10, 21))]
    assert a["e1_ret_pct"] == pytest.approx(5.0)


def test_rows_stored_before_the_column_existed_fall_back_to_fetched_at(tmp_path):
    from agent.books.data import insider_flows
    with PanelStore(tmp_path / "p.db") as s:
        for acc, fetched in (("on-time", "2026-10-21 16:12:00"), ("late", "2026-10-22 07:00:00")):   # PT, naive
            s.con.execute("INSERT INTO insider_tx (accession, ticker, filing_date, trans_date, owner_name, trans_code, acq_disp, "
                          "shares, price, value_usd, source, fetched_at) VALUES (?, ?, '2026-10-21', '2026-10-21', 'o', 'P', 'A', "
                          "100, 10, 1000, 'edgar_realtime', ?)", [acc, acc.upper(), fetched])
        assert s.con.execute("SELECT count(*) FROM insider_tx WHERE stored_at IS NULL").fetchone()[0] == 2
        for col in (True, False):
            seen = insider_flows(s, "2026-10-21", sh.visible_filter(D(2026, 10, 22), D(2026, 10, 21), col))
            assert sorted(seen["ticker"]) == ["ON-TIME"]                        # 07:00 PT is after the 10/22 09:25 ET cutoff
            seen = insider_flows(s, "2026-10-21", sh.visible_filter(D(2026, 10, 23), D(2026, 10, 21), col))
            assert sorted(seen["ticker"]) == ["LATE", "ON-TIME"]


# ------------------------------------------------------------------ review fixes (2026-10-21) ----
def test_a_row_from_another_source_counts_from_the_1915_planning_run(tmp_path):
    """The 19:15 late pass plans from every row the panel holds: a daily-index row stored at 17:00 PT (the 06:00 job run
    late after a sleep) is seen for the next open; one stored after 19:15 is not, while a realtime one still is."""
    from agent.books.data import insider_flows
    with PanelStore(tmp_path / "p.db") as s:
        s.insert("insider_tx", pd.DataFrame([_tx("DLY", "2026-10-21", 1000, pt("2026-10-21 17:00"), "edgar_daily"),
                                             _tx("LATE", "2026-10-21", 1000, pt("2026-10-21 19:30"), "edgar_daily"),
                                             _tx("RT", "2026-10-21", 1000, pt("2026-10-21 19:30"))]))
        for x, want in ((D(2026, 10, 22), ["DLY", "RT"]), (D(2026, 10, 23), ["DLY", "LATE", "RT"])):
            seen = insider_flows(s, "2026-10-21", sh.visible_filter(x, D(2026, 10, 21)))
            assert sorted(seen["ticker"]) == want


def test_looks_are_at_the_end_of_each_entry_day():
    assert sh.looks_at([D(2026, 10, 22)] * 2 + [D(2026, 10, 23)] * 3 + [D(2026, 10, 26)]) == [2, 5, 6]
    sp = {**SP, "sigma_prior_pct": 0.5}                    # upper 0.135 + 2.567/n, lower 0.135 - 1.443/n
    day = [10.0] * 3 + [-10.0] * 5                         # one entry day: three winners first in ticker order, then losers
    assert sh.sprt(np.array(day), sp)["decision"]["decision"] == "H1"          # a look after every event: H1 at n = 1
    r = sh.sprt(np.array(day), sp, looks=[8])                                   # a look when the day is in
    assert r["decision"]["decision"] == "H0" and r["decision"]["n"] == 8


def test_the_truncation_look_is_at_exactly_truncate_n_whatever_the_day_batches():
    sp = {**SP, "truncate_n": 50, "sigma_reestimate_n": 1000}
    r = sh.sprt(np.tile([1.2, -0.8], 40), sp, looks=[49, 80])                  # a day's batch runs from event 50 to 80
    assert r["decision"]["truncated"] and r["decision"]["n"] == 50 and r["decision"]["mean_pct"] == pytest.approx(0.2)


def _seq(db: str) -> dict:
    return {(t, d): q for t, d, q in zip(*[_events(db)[c] for c in ("ticker", "event_day", "e1_seq")])}


def test_a_late_e1_is_appended_to_the_sprt_order_not_inserted(world):
    with PanelStore(world["panel"]) as s:                       # AAA's entry-day bar is missing at the first run
        s.con.execute("DELETE FROM bars WHERE ticker = 'AAA' AND trade_date = '2026-10-22'")
    sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    first = _seq(world["db"])
    assert pd.isna(first[("AAA", D(2026, 10, 21))])
    assert sorted(v for v in first.values() if pd.notna(v)) == list(range(1, len(first)))   # 1..n, nothing skipped
    with PanelStore(world["panel"]) as s:                       # the refetch brings it later
        s.upsert_bars(pd.DataFrame([_bars("AAA", D(2026, 10, 22), 10.0, 10.5)]))
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), panel=world["panel"])
    after = _seq(world["db"])
    assert after[("AAA", D(2026, 10, 21))] == len(first)                         # last, after every earlier E1
    assert {k: v for k, v in after.items() if k != ("AAA", D(2026, 10, 21))} == \
        {k: v for k, v in first.items() if k != ("AAA", D(2026, 10, 21))}       # the walked path is unchanged
    assert rep["recent_events"] and rep["sprt"]["n"] == len(first)


def test_a_due_reading_waits_while_a_missing_exit_can_still_come(world, monkeypatch):
    cfg = copy.deepcopy(CFG)
    cfg["insider_shadow"]["reading_date"] = D(2026, 11, 2)
    with PanelStore(world["panel"]) as s:                       # WWW (entry 10/27) has no bar after 10/28 at first
        s.con.execute("DELETE FROM bars WHERE ticker = 'WWW' AND trade_date > '2026-10-28'")
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), cfg=cfg, panel=world["panel"])
    assert rep["exit_readings"]["status"] == "due, waiting for exits"          # 10/27 is still looked for: no reading
    assert "a_E5c_minus_E6o" not in open(world["out"]).read()
    monkeypatch.setattr(sh, "REVISIT_SESSIONS", 20)             # 10/27 now older than the revisit window: missing for good
    rep = sh.run(world["db"], world["out"], today=D(2026, 11, 30), cfg=cfg, panel=world["panel"])
    assert rep["exit_readings"]["status"] == "read" and rep["exit_readings"]["n_events"] == 5
