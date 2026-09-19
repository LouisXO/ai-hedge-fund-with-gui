"""Agent ledger + gate + pick selection. Temp DuckDB, no network, no moomoo."""
import pandas as pd
import pytest

from agent import brief, ledger
from agent.daily import gate, pick


@pytest.fixture
def con(tmp_path):
    c = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(c)
    yield c
    c.close()


def test_gate_keeps_cheap_half_with_vol_reverting():
    iv = pd.Series({"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0})
    rv20 = pd.Series({"A": 9.0, "B": 18.0, "C": 26.0, "D": 35.0})
    rv60 = pd.Series({"A": 12.0, "B": 15.0, "C": 30.0, "D": 40.0})   # B's vol is rising
    g = gate(iv, rv20, rv60)
    assert list(g[g["passed"]].index) == ["A"]          # B fails vol, C/D fail the IV median
    assert g.loc["B", "reason"] == "rv20_above_rv60"
    assert g.loc["D", "reason"] == "iv_above_median"


def test_pick_takes_top_k_per_side_only_from_gated_names():
    names = list("ABCDEFG")
    frames = {"sig": pd.DataFrame({"value": [0.9, 0.7, 0.5, 0.1, -0.5, -0.8, -0.9],
                                   "rv20": 20.0, "rv60": 25.0, "iv": 23.0}, index=names)}
    iv = pd.Series(23.0, index=names)
    g = pd.DataFrame({"passed": [True] * 6 + [False], "reason": ""}, index=names)
    picks = pick(frames, g, iv)
    calls = [p["ticker"] for p in picks if p["side"] == "C"]
    puts = [p["ticker"] for p in picks if p["side"] == "P"]
    assert calls == ["A", "B", "C", "D", "E"]
    assert puts == ["F", "E", "D", "C", "B"]            # G is gated out
    assert all(p["breakeven_pct"] > 0 for p in picks)
    assert {p["status"] for p in picks} == {"shadow"}


def test_insert_is_by_column_name_not_dict_order(con):
    rows = [{"ticker": "AAA", "side": "C", "rank": 1, "value": 0.5, "as_of": "2026-09-18",
             "signal_name": "s", "iv": 20.0, "rv20": 18.0, "rv60": 22.0, "breakeven_pct": 1.2,
             "gate_passed": True, "gate_reason": "", "status": "shadow", "ledger_id": None, "run_id": "r1"}]
    assert ledger.write_picks(con, rows) == 1
    got = con.execute("SELECT ticker, side, gate_passed, status FROM agent_picks").fetchone()
    assert got == ("AAA", "C", True, "shadow")


def test_fill_returns_uses_next_open_as_entry(con):
    idx = pd.bdate_range("2026-09-14", periods=12)
    close = pd.DataFrame({"AAA": [100 + i for i in range(12)]}, index=idx, dtype=float)
    opn = close.shift(1).fillna(100.0)
    spy = pd.Series(range(400, 412), index=idx, dtype=float)
    ledger.write_signals(con, idx[0].date(), "r1", {"s": pd.DataFrame({"value": [0.1]}, index=["AAA"])}, set())
    assert ledger.fill_returns(con, close, opn, spy) == 1
    row = con.execute("SELECT entry_px, ret1, ret5 FROM agent_returns").fetchone()
    assert row[0] == pytest.approx(100.0)                 # open of the day after the signal
    assert row[1] == pytest.approx((close.iat[1, 0] / 100 - 1) * 100)
    assert row[2] == pytest.approx((close.iat[5, 0] / 100 - 1) * 100)


def test_live_summary_counts_direction_hits(con):
    idx = pd.bdate_range("2026-09-14", periods=12)
    close = pd.DataFrame({"AAA": [100 + i for i in range(12)]}, index=idx, dtype=float)
    opn = close.shift(1).fillna(100.0)
    ledger.write_signals(con, idx[0].date(), "r1", {"s": pd.DataFrame({"value": [0.1]}, index=["AAA"])}, set())
    ledger.fill_returns(con, close, opn, pd.Series(1.0, index=idx))
    ledger.write_picks(con, [{"as_of": idx[0].date(), "ticker": "AAA", "signal_name": "s", "side": "C",
                              "rank": 1, "value": 0.1, "iv": 20.0, "rv20": 1, "rv60": 1,
                              "breakeven_pct": 1.0, "gate_passed": True, "gate_reason": "",
                              "status": "shadow", "ledger_id": None, "run_id": "r1"}])
    s = ledger.live_summary(con)
    assert s["by_side"][0]["dir_hit"] == 1.0              # price rose and the pick was a call
    assert s["n_days"] == 1


def test_brief_one_line_mentions_shadow_and_gate():
    d = {"mode": "shadow", "gate": {"passed": 236, "of": 503}, "picks": [{}] * 30, "validated_signals": []}
    line = brief.one_line(d, {"by_side": [], "horizon": "ret10"})
    assert "shadow" in line and "236/503" in line and "30" in line
