"""Trades from posts: a level is not a fill, a recap repeating the day's log is one trade, a re-open after a close is new,
and a rebuild starts from an empty table (made-up posts, a fake LLM, no network)."""
import datetime as dt
import json
import os

import duckdb

from agent import balder_log as bl


class FakeLLM:
    """Answers by the first key found in the post text."""

    def __init__(self, answers: dict):
        self.answers = answers

    def call(self, system, user):
        for key, rows in self.answers.items():
            if key in user:
                return {"result": json.dumps(rows, ensure_ascii=False)}
        return {"result": "[]"}


def _con():
    con = duckdb.connect()
    con.execute(bl.DDL)
    return con


def _rows(con):
    return con.execute("SELECT posted::VARCHAR, ticker, action, price FROM balder_trades ORDER BY posted, ticker, action").fetchall()


def test_a_target_or_stop_is_not_a_fill_price():
    llm = FakeLLM({"本月": [{"ticker": "XYZ", "action": "open", "price": 45}],
                   "平仓": [{"ticker": "ABC", "action": "close", "price": 19.8}]})
    assert bl.extract("本月推荐 $XYZ 初步45 目标52", llm)[0]["price"] is None
    assert bl.extract("平仓 $ABC 20.5→19.8 -3.4%", llm)[0]["price"] == 19.8
    assert bl.only_as_level("第一目标 123 ✅ 后续目标 150", 123)
    assert bl.only_as_level("Raises Price Target to $900", 900)
    assert not bl.only_as_level("$ABC 123 买入,目标 150", 123)              # stated as a fill: kept
    assert not bl.only_as_level("目标 150,现价 123 买入", 123)               # the target word belongs to 150


def test_a_post_is_dated_by_when_it_was_posted_not_when_it_was_saved():
    assert bl.posted_date("x", "2026-10-07 Balder X posts 帖子(2026-10-06T20:45:00Z):日志") == dt.date(2026, 10, 6)
    assert bl.posted_date("x", "2026-10-07 Balder X posts 帖子(2026-10-07T02:30:00.000Z):晚上") == dt.date(2026, 10, 6)   # 22:30 ET
    assert bl.posted_date("x", "2026-10-02 Balder X subs 帖子(2026-09-24T21:00):平仓") == dt.date(2026, 9, 24)
    assert bl.posted_date("x", "2026-10-01 平仓 $ABC 20.5→19.8") == dt.date(2026, 10, 1)                    # a file dropped by hand


def test_a_recap_repeating_the_days_log_is_one_trade_and_can_fill_a_missing_price(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "2026-10-06-x-1.txt").write_text("2026-10-06 日志:平仓 $ABC;新开 $DEF 🆕 31.25", encoding="utf-8")
    (drop / "2026-10-06-x-2.txt").write_text("2026-10-06 复盘:今日退出 $ABC 20.5→19.8;今日新增 $DEF 31.25", encoding="utf-8")
    llm = FakeLLM({"日志": [{"ticker": "ABC", "action": "close", "price": None}, {"ticker": "DEF", "action": "open", "price": 31.25}],
                   "复盘": [{"ticker": "ABC", "action": "close", "price": 19.8}, {"ticker": "DEF", "action": "open", "price": 31.25}]})
    con = _con()
    n = bl.ingest(con, drop=str(drop), seen_path=str(tmp_path / "seen.json"), transport=llm)
    assert n == 2
    assert _rows(con) == [("2026-10-06", "ABC", "close", 19.8), ("2026-10-06", "DEF", "open", 31.25)]
    assert sorted(json.load(open(tmp_path / "seen.json"))) == ["2026-10-06-x-1.txt", "2026-10-06-x-2.txt"]


def test_a_reopen_after_a_close_is_a_new_trade(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    for name, text in (("a.txt", "2026-10-01 开仓 $ABC 10.5"), ("b.txt", "2026-10-02 平仓 $ABC 11.0"), ("c.txt", "2026-10-03 再开仓 $ABC 10.8")):
        (drop / name).write_text(text, encoding="utf-8")
    llm = FakeLLM({"再开仓": [{"ticker": "ABC", "action": "open", "price": 10.8}],
                   "开仓": [{"ticker": "ABC", "action": "open", "price": 10.5}],
                   "平仓": [{"ticker": "ABC", "action": "close", "price": 11.0}]})
    con = _con()
    bl.ingest(con, drop=str(drop), seen_path=str(tmp_path / "seen.json"), transport=llm)
    assert [r[2] for r in _rows(con)] == ["open", "close", "open"]


def test_a_dry_run_rebuild_reads_every_post_again_and_saves_no_seen_file(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.txt").write_text("2026-10-01 开仓 $ABC 10.5", encoding="utf-8")
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps({"a.txt": "2026-10-01"}))
    con = _con()
    con.execute("INSERT INTO balder_trades (id, posted, ticker, action) VALUES ('junk', '2026-10-01', 'HPQ', 'open')")
    con.execute("DELETE FROM balder_trades")                                   # what --rebuild does before ingesting
    llm = FakeLLM({"开仓": [{"ticker": "ABC", "action": "open", "price": 10.5}]})
    assert bl.ingest(con, dry_run=True, rebuild=True, drop=str(drop), seen_path=str(seen), transport=llm) == 1
    assert _rows(con) == [("2026-10-01", "ABC", "open", 10.5)]
    assert json.load(open(seen)) == {"a.txt": "2026-10-01"}                    # untouched by a dry run


def test_a_supplement_adds_only_new_tickers_and_can_fill_a_missing_price(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "2026-10-27-x-5.txt").write_text("2026-10-27 Balder X subs 帖子(2026-10-01T20:00:00Z):截断 平仓 $ABC …", encoding="utf-8")
    (drop / "2026-10-27-x-5-full-153000.txt").write_text(
        "2026-10-27 Balder X subs 帖子(2026-10-01T20:00:00Z)(补全):全文 平仓 $ABC 10.00→10.50 平仓 DEF 20.00→19.00", encoding="utf-8")
    (drop / "2026-10-27-x-55.txt").write_text("2026-10-27 Balder X subs 帖子(2026-10-01T19:00:00Z):别的帖 平仓 $DEF", encoding="utf-8")
    llm = FakeLLM({"全文": [{"ticker": "ABC", "action": "close", "price": 10.5}, {"ticker": "DEF", "action": "close", "price": 19.0}],
                   "截断": [{"ticker": "ABC", "action": "close", "price": None}],
                   "别的帖": []})
    assert bl.drop_order(os.listdir(drop))[-1] == "2026-10-27-x-5-full-153000.txt"     # read after its post even on the same day
    con = _con()
    assert bl.ingest(con, drop=str(drop), seen_path=str(tmp_path / "seen.json"), transport=llm) == 2
    assert _rows(con) == [("2026-10-01", "ABC", "close", 10.5), ("2026-10-01", "DEF", "close", 19.0)]
    assert con.execute("SELECT count(*) FROM balder_trades WHERE ticker = 'ABC'").fetchone()[0] == 1
    assert bl.post_id("2026-10-27-x-5-full-153000.txt") == "5" and bl.post_id("2026-10-01-lmt-close.txt") is None


def test_a_quantity_in_words_is_dropped_not_a_crash():
    llm = FakeLLM({"买入": [{"ticker": "ABC", "action": "open", "price": None, "qty": "一手"},
                           {"ticker": "DEF", "action": "open", "price": None, "qty": "2,000"},
                           {"ticker": "GHI", "action": "open", "price": None, "qty": "30%"}]})
    assert [x["qty"] for x in bl.extract("买入 $ABC $DEF $GHI", llm)] == [None, 2000.0, 30.0]
