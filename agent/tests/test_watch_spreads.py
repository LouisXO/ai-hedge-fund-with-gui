"""A watched option spread: the day's record, open-interest reading and value levels (agent/watch.py)."""
from agent.watch import spread_alerts, spread_record

CFG = {"name": "RKLB 90/150", "legs": [["C90", 1], ["C150", -1]], "labels": ["90 看涨", "150 看涨"],
       "seen": {"qty": 1150, "price": 7.50, "oi": [1540, 3848]}, "below": [3.1], "above": [12.5, 30.0]}


def rec(bid90, ask90, oi90, bid150, ask150, oi150):
    snap = {"C90": {"bid": bid90, "ask": ask90, "last": None, "oi": oi90, "iv": 75.8, "volume": 0},
            "C150": {"bid": bid150, "ask": ask150, "last": None, "oi": oi150, "iv": 81.0, "volume": 0}}
    return spread_record(snap, CFG["legs"])


def test_the_spread_mid_is_long_minus_short():
    assert rec(7.9, 9.0, 1540, 2.0, 2.47, 3848)["mid"] == 6.215


def test_first_day_open_interest_up_by_the_print_reads_as_an_opening():
    alerts, line = spread_alerts(CFG, rec(7.9, 9.0, 2690, 2.0, 2.47, 4998), None)
    assert len(alerts) == 2 and all("像是新开仓" in a for a in alerts)
    assert "价差中间价 6.21(大单成本 7.50,-17%)" in line or "价差中间价 6.22(大单成本 7.50,-17%)" in line


def test_open_interest_down_reads_as_a_closing_and_small_moves_say_nothing():
    alerts, _ = spread_alerts(CFG, rec(7.9, 9.0, 390, 2.0, 2.47, 3900), None)
    assert alerts == ["RKLB 90/150:90 看涨 持仓量 1,540 → 390(-1,150,大单 1,150 张,像是平仓)"]


def test_value_levels_fire_on_the_crossing_day_only():
    before = rec(11.0, 12.0, 2690, 1.0, 1.2, 4998)                    # mid 10.4
    alerts, _ = spread_alerts(CFG, rec(15.0, 16.0, 2690, 2.0, 2.2, 4998), before)   # mid 13.4
    assert alerts == ["RKLB 90/150:价差突破 12.5 → 13.40"]
    assert spread_alerts(CFG, rec(15.0, 16.0, 2690, 2.0, 2.2, 4998), rec(15.0, 16.0, 2690, 2.0, 2.2, 4998))[0] == []
