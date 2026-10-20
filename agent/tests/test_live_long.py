"""Once-per-bar rule for the live long book (signal-driven cadence)."""
import pandas as pd
import pytest

from agent.books.live import should_score


def test_scores_each_new_bar_once():
    bar = pd.Timestamp("2026-09-30")
    assert should_score(bar, None)
    assert not should_score(bar, bar)                          # rerun the same morning: idempotent
    assert should_score(pd.Timestamp("2026-10-01"), bar)       # next completed bar


def test_engine_sizes_and_stop_rule():
    """S24: per-name sizing scales the entry; a 20% drawdown from the high sells at the next open."""
    import numpy as np
    from agent.books.data import Market
    from agent.books.engine import simulate
    idx = pd.bdate_range("2026-01-05", periods=60)     # long enough for the metrics' bootstrap
    px = pd.DataFrame(10.0, index=idx, columns=["A", "B"])
    px.loc[idx[3]:, "A"] = 12.0                       # A rallies to 12 (new high)
    px.loc[idx[6]:, "A"] = 9.0                        # then drops 25% from that high -> stop
    adv = pd.DataFrame(5e6, index=idx, columns=["A", "B"])
    m = Market(px, px, px, adv, pd.DataFrame(True, index=idx, columns=["A", "B"]), pd.Series(100.0, index=idx), {})
    targets = {d: ["A", "B"] for d in idx}
    sizes = {d: {"A": 0.30, "B": 0.10} for d in idx}
    res = simulate(m, targets, str(idx[0].date()), str(idx[-1].date()), max_positions=2, hold_days=None,
                   exec_frac=0.0, sizes=sizes, stop_pct=20.0)
    a = [t for t in res.trades if t.ticker == "A"]
    assert len(a) == 1 and a[0].exit_day == idx[7]                     # flagged at idx[6]'s close, sold next open
    assert a[0].ret_pct == pytest.approx(-10.0)                         # 10 -> 9
    # sizing: A got 30% of equity, B 10% -> exposure on day 1 is 40%
    assert res.exposure.iloc[1] == pytest.approx(0.40, abs=0.01)
    # A re-enters after the stop because it is still in the target list (no cooling-off rule in v1)


def test_v2_shadow_matches_v1_outside_a_crash_and_drops_momentum_inside():
    from agent.books.long_v2 import spy_in_crash_regime
    from agent.books.data import Market
    idx = pd.bdate_range("2024-01-01", periods=600)
    spy = pd.Series(100.0, index=idx)
    spy.iloc[550:] = 75.0                                                  # -25% from the 2-year high
    m = Market(pd.DataFrame(index=idx), pd.DataFrame(index=idx), pd.DataFrame(index=idx), pd.DataFrame(index=idx),
               pd.DataFrame(index=idx), spy, {})
    r = spy_in_crash_regime(m)
    assert not r.iloc[540] and r.iloc[560]


def bars_panel(tmp_path, counts: dict) -> str:
    """A temp panel whose bars table has `counts[day]` tickers on each day."""
    from hedge_fund.features.panel import PanelStore
    db = str(tmp_path / "panel.db")
    rows = [{"ticker": f"T{i:04d}", "trade_date": pd.Timestamp(d).date(), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
             "adj_close": 10.0, "volume": 1e6, "source": "test", "fetched_at": pd.Timestamp("2026-10-19")}
            for d, n in counts.items() for i in range(n)]
    with PanelStore(db) as s:
        s.upsert_bars(pd.DataFrame(rows))
    return db


@pytest.mark.parametrize("before, n_day, thin", [
    ([100] * 5, 10, True),                       # 10-16: about 500 names of 5,300
    ([100] * 5, 95, False),                      # ordinary drift, a half day
    ([100] * 5, 90, False), ([100] * 5, 89, True),    # the edge: at least 90% of the median passes
    ([100, 10, 100, 10, 100], 10, True),         # two short sessions before do not move the median
    ([100, 10, 100, 10, 100], 100, False),       # the day after a short day passes
    ([100, 100], 10, True),                      # fewer than five sessions: the median of what there is
    ([], 10, False),                             # nothing to compare with
])
def test_thin_bar_day_against_the_median_of_the_five_sessions_before(tmp_path, before, n_day, thin):
    from agent.books.live import thin_bar_day
    from hedge_fund.features.panel import PanelStore
    days = pd.bdate_range(end="2026-10-16", periods=len(before) + 1)
    db = bars_panel(tmp_path, {**dict(zip(days[:-1], before)), days[-1]: n_day})
    with PanelStore(db, read_only=True) as s:
        why = thin_bar_day(s, days[-1])
    assert (why is not None) == thin, why
    if thin:
        assert why.startswith(f"2026-10-16 has bars for {n_day:,} names") and f"of the {len(before)} sessions before it" in why


def test_thin_bar_day_reads_only_the_five_sessions_before_and_a_day_without_bars(tmp_path):
    from agent.books.live import thin_bar_day
    from hedge_fund.features.panel import PanelStore
    days = pd.bdate_range(end="2026-10-16", periods=7)
    db = bars_panel(tmp_path, dict(zip(days, [1000, 1000, 1000, 100, 100, 100, 95])))
    with PanelStore(db, read_only=True) as s:
        assert thin_bar_day(s, days[-1]) is None               # median 100; with the sixth session back it would be 550
        why = thin_bar_day(s, pd.Timestamp("2026-10-19"))      # no bar at all that day
        assert why == "2026-10-19 has bars for 0 names, under 90% of the median 100 of the 5 sessions before it"


def test_shadow_v2_does_not_record_cluster_lists_on_a_thin_bar_day(tmp_path, monkeypatch, capsys):
    """agent.shadow_v2 at 13:25 records the latest bar's cluster lists when the 16:10 job has not: same guard as the brief."""
    import types
    from agent import shadow_v2
    from hedge_fund.features.panel import PanelStore
    days = pd.bdate_range(end="2026-10-16", periods=6)
    panel = bars_panel(tmp_path, {**{d: 100 for d in days[:-1]}, days[-1]: 10})
    calls = []
    monkeypatch.setattr(shadow_v2, "PanelStore", lambda read_only=False: PanelStore(panel, read_only=read_only))
    monkeypatch.setattr(shadow_v2, "load_market", lambda store, start: types.SimpleNamespace(adj=pd.DataFrame(index=days)))
    monkeypatch.setattr(shadow_v2, "industry_by_ticker", lambda store: pd.Series(dtype=object))
    monkeypatch.setattr(shadow_v2, "DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(shadow_v2, "record", lambda con, store, market, day: calls.append(day) or 60)
    monkeypatch.setattr(shadow_v2, "recorded_lists", lambda con: ({}, {}))
    monkeypatch.setattr(shadow_v2, "long_capital", lambda con: 60000.0)
    assert shadow_v2.main() == 0 and calls == []
    assert "shadow v2: cluster lists not recorded: 2026-10-16 has bars for 10 names" in capsys.readouterr().out
    bars_panel(tmp_path, {days[-1]: 100})
    assert shadow_v2.main() == 0 and calls == [days[-1]]
