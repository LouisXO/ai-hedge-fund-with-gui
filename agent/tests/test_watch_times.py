"""moomoo's unusual-options lines are stamped in Beijing time; the watchlist page shows US Pacific."""
import datetime as dt

from agent.watch import BEIJING, to_pacific

NOW = dt.datetime(2026, 9, 30, 8, 0, tzinfo=BEIJING)


def test_beijing_stamps_become_pacific_with_daylight_time():
    assert to_pacific("9.29 21:50，出现一笔买入看涨期权交易", NOW) == "9/29 06:50 PT，出现一笔买入看涨期权交易"
    assert to_pacific("9.30 01:21，x", NOW) == "9/29 10:21 PT，x"
    assert to_pacific("12.15 22:30，x", dt.datetime(2026, 12, 16, tzinfo=BEIJING)) == "12/15 06:30 PT，x"    # PST, UTC-8


def test_a_december_line_read_in_january_is_last_year_and_other_text_is_left_alone():
    assert to_pacific("12.31 23:00，x", dt.datetime(2027, 1, 2, tzinfo=BEIJING)) == "12/31 07:00 PT，x"
    assert to_pacific("没有时间的一行", NOW) == "没有时间的一行"
