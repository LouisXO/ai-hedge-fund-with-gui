"""Views from posts: the quote must be in the post, index levels are not views, a repeat within three days is one view,
and a bear view is right when the stock trails SPY (made-up posts, a fake LLM, no network)."""
import datetime as dt
import json

import duckdb
import pandas as pd

from agent import balder_views as bv


class FakeLLM:
    def __init__(self, rows):
        self.rows = rows

    def call(self, system, user):
        return {"result": json.dumps(self.rows, ensure_ascii=False)}


def _con():
    con = duckdb.connect()
    con.execute(bv.DDL)
    return con


def test_a_view_needs_its_words_in_the_post_and_an_index_is_not_a_view():
    post = "ABC 的订单很硬,这点回调不用慌。XYZ 这波该跑了。大盘 SPX 今天区间 100-110。"
    llm = FakeLLM([{"ticker": "$abc", "stance": "bull", "horizon": "short", "claim": "订单硬", "quote": "ABC 的订单很硬，这点回调不用慌"},
                   {"ticker": "XYZ", "stance": "bear", "claim": "该跑", "quote": "XYZ 这波该卖了"},           # not the post's words
                   {"ticker": "SPX", "stance": "bull", "claim": "区间", "quote": "大盘 SPX 今天区间"},
                   {"ticker": "DEF", "stance": "maybe", "claim": "?", "quote": "ABC 的订单很硬"}])
    got = bv.extract(post, llm)
    assert [(v["ticker"], v["stance"], v["horizon"]) for v in got] == [("ABC", "bull", "short")]


def test_the_same_view_within_three_days_is_one_and_the_opposite_stance_is_new():
    d = dt.date(2026, 10, 5)
    v = lambda day, stance, f: {"posted": day, "ticker": "ABC", "stance": stance, "horizon": "unknown", "claim": "", "quote": "", "source_file": f}
    con = _con()
    n = bv.record(con, [v(d, "bull", "a.txt"), v(d + dt.timedelta(days=2), "bull", "b.txt"),
                        v(d + dt.timedelta(days=2), "bear", "c.txt"), v(d + dt.timedelta(days=9), "bull", "d.txt")])
    assert n == 3
    assert con.execute("SELECT stance, source_file FROM balder_views ORDER BY posted, stance").fetchall() == [
        ("bull", "a.txt"), ("bear", "c.txt"), ("bull", "d.txt")]


def test_scoring_starts_at_the_next_open_and_a_bear_is_right_when_it_trails_spy():
    sess = [dt.date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8)]
    bars = pd.DataFrame({"ticker": "ABC", "trade_date": sess, "open": [10, 20, 21, 22, 23, 24],
                         "close": [10, 20, 21, 22, 23, 18], "adj_close": [10, 20, 21, 22, 23, 18]})
    spy_open = {s: 100.0 for s in sess}
    spy_close = {s: 101.0 for s in sess}
    got = bv.forward([("v1", dt.date(2026, 10, 1), "ABC")], sess, bars, spy_open, spy_close)
    assert round(got["v1"]["ret5"], 6) == round((18 / 20 - 1) * 100, 6)        # entry 10/2 open 20, fifth session 10/8 close 18
    assert round(got["v1"]["spy5"], 6) == 1.0 and "ret20" not in got["v1"]
    con = _con()
    con.execute("INSERT INTO balder_views VALUES ('v1', '2026-10-01', 'ABC', 'bear', 'short', '', '', 'a.txt', -10, NULL, 1, NULL, '2026-10-08')")
    s = bv.summary(con)["by_stance"]["bear"]["h5"]
    assert s["n"] == 1 and s["right"] == 1.0 and s["mean_abn_signed"] == 11.0


def test_collect_reads_unseen_posts_only_and_takes_the_posting_day(tmp_path):
    (tmp_path / "2026-10-07-x-1.txt").write_text("2026-10-07 Balder X posts 帖子(2026-10-07T02:30:00Z):ABC 回调不用慌", encoding="utf-8")
    (tmp_path / "old.txt").write_text("ABC 回调不用慌", encoding="utf-8")
    llm = FakeLLM([{"ticker": "ABC", "stance": "bull", "claim": "", "quote": "ABC 回调不用慌"}])
    views, seen = bv.collect(drop=str(tmp_path), seen={"old.txt": "2026-10-01"}, transport=llm)
    assert [(v["source_file"], v["posted"]) for v in views] == [("2026-10-07-x-1.txt", dt.date(2026, 10, 6))]   # 22:30 ET
    assert set(seen) == {"old.txt", "2026-10-07-x-1.txt"}
