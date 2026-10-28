"""Review page (2026-10-27): the Balder scorecard, the earnings table of the paper holdings and the Balder line under
real deals. Made-up tickers, prices and dates (this repository is public); temp files and temp DuckDB only."""
import datetime as dt
import json
import math

import duckdb
import pandas as pd
import pytest

from agent import market_breadth as MB
from agent import review as R

D = dt.date


# ------------------------------------------------------------------ scorecard ----
def test_badge_counts_effective_n_not_rows():
    assert R.balder_badge(0, None) == "未计分" and R.balder_badge(0, 40) == "未计分"
    assert R.balder_badge(12, 12) == "只描述"
    assert R.balder_badge(45, None) == "只描述"                    # 45 rows, effective n unknown: not enough
    assert R.balder_badge(45, R.BALDER_MIN_N - 1) == "只描述"       # 45 rows on 29 posting days
    assert R.balder_badge(45, R.BALDER_MIN_N) == "可读"


def test_ci95_and_its_text():
    lo, hi = R.ci95(1.0, 2.0, 16)
    assert (lo, hi) == pytest.approx((1.0 - 1.96 * 2.0 / 4, 1.0 + 1.96 * 2.0 / 4))
    assert R.ci95(1.0, None, 16) is None and R.ci95(1.0, 2.0, 1) is None and R.ci95(None, 2.0, 16) is None
    assert R.ci95(1.0, math.nan, 16) is None
    assert R.ci_text((-0.5, 2.9)) == "[-0.50, +2.90]"
    assert R.ci_text(None) == "(无 sd,不给区间)"


def _trade(book, posted, ret5, spy5, action="open", ticker="ABC"):
    return {"book": book, "posted": posted, "ticker": ticker, "action": action, "ret5": ret5, "spy5": spy5}


def test_scorecard_rows_from_fields_rows_and_t():
    trades = {"generated_at": "2030-01-10T13:30:00", "n": 5,
              "by_book": {"short": {"h5": {"n": 40, "mean_abn": 0.8, "hit": 0.55, "n_days": 31, "sd_abn": 4.0}},   # summary fields win
                          "long": {"h5": {"n": 2, "mean_abn": 1.5, "hit": 1.0}}},                                    # no fields: from the rows
              "recent": [_trade("long", "2030-01-02", 3.0, 1.0), _trade("long", "2030-01-02", 2.0, 1.0),
                         _trade("long", "2030-01-03", 9.0, 0.0, action="close"),                                     # a close is not scored
                         _trade("short", "2030-01-03", 1.0, 0.0)]}
    views = {"generated_at": "2030-01-10T13:34:00", "n": 3,
             "by_stance": {"bull": {"h5": {"n": 3, "mean_abn_signed": -1.0, "right": 1 / 3}}},
             "recent": [{"stance": "bull", "posted": "2030-01-02", "ret5": 1.0, "spy5": 2.0},
                        {"stance": "bull", "posted": "2030-01-03", "ret5": 1.0, "spy5": 1.0},
                        {"stance": "bull", "posted": "2030-01-04", "ret5": 0.0, "spy5": 2.0}]}
    record = {"generated_at": "2030-01-10T16:18:00", "n": 50,
              "algo": {"n": 36, "mean_ex_qqq": 0.6, "t": 1.5, "beat_qqq": 0.5}, "long": {"n": 0}, "week_trades": []}
    rows = {(r["product"], r["group"]): r for r in R.balder_scorecard(trades, views, record)}
    assert len(rows) == 6
    s = rows[("X 帖子交易", "短线")]
    assert (s["n"], s["n_eff"], s["badge"], s["asof"]) == (40, 31, "可读", "2030-01-10")
    assert s["ci"] == pytest.approx((0.8 - 1.96 * 4.0 / math.sqrt(40), 0.8 + 1.96 * 4.0 / math.sqrt(40)))
    lg = rows[("X 帖子交易", "长线")]
    assert lg["n_eff"] == 1 and lg["badge"] == "只描述"                                 # two trades, one posting day
    assert lg["ci"] == pytest.approx((1.5 - 1.96 * math.sqrt(0.5) / math.sqrt(2), 1.5 + 1.96 * math.sqrt(0.5) / math.sqrt(2)))
    bull = rows[("X 帖子观点", "看多")]
    assert bull["n_eff"] == 3 and bull["hit"] == pytest.approx(1 / 3)
    signed = [-1.0, 0.0, -2.0]
    sd = pd.Series(signed).std(ddof=1)
    assert bull["ci"] == pytest.approx((-1.0 - 1.96 * sd / math.sqrt(3), -1.0 + 1.96 * sd / math.sqrt(3)))
    assert rows[("X 帖子观点", "看空")]["badge"] == "未计分"                             # no bear row at all
    algo = rows[("官网公开记录", "短线算法")]
    assert algo["n_eff"] is None and algo["badge"] == "只描述"                           # 36 closed, posting days unknown
    assert algo["ci"] == pytest.approx((0.6 - 1.96 * 0.6 / 1.5, 0.6 + 1.96 * 0.6 / 1.5))   # se = mean / t
    assert rows[("官网公开记录", "长线")]["badge"] == "未计分" and rows[("官网公开记录", "长线")]["mean"] is None


def test_scorecard_row_list_short_of_n_leaves_effective_n_unknown():
    trades = {"n": 30, "by_book": {"short": {"h5": {"n": 20, "mean_abn": 0.1, "hit": 0.5}}},
              "recent": [_trade("short", "2030-01-02", 1.0, 0.5)] * 15}                     # newest 15 of 20 scored
    s = [r for r in R.balder_scorecard(trades, {}, {}) if r["group"] == "短线"][0]
    assert s["n_eff"] is None and s["ci"] is None and s["badge"] == "只描述"
    assert "有效 n 未知" in R.balder_scorecard_html([s]) and "无 sd" in R.balder_scorecard_html([s])


@pytest.mark.parametrize("content", [None, "", "{}", "[]", "not json"])
def test_balder_block_renders_with_files_missing_or_empty(tmp_path, monkeypatch, content):
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    if content is not None:
        for f in ("balder_latest.json", "balder_views.json", "balder_record.json"):
            (tmp_path / f).write_text(content)
    html = R.balder_block()
    assert html.count("<span class='pill'>未计分</span>") == 6 and "永远不是信号" in html and "文件缺失" in html
    assert "官网公开记录:还没有数据" in html and "X 帖子观点:还没有记录" in html


def test_balder_block_with_partial_summaries(tmp_path, monkeypatch):
    """Rows with missing keys and None values (an older summary) still render."""
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    (tmp_path / "balder_latest.json").write_text(json.dumps({"n": 1, "by_book": {"short": {"h1": {"n": 1, "mean": 1.0}}},
                                                             "recent": [{"posted": "2030-01-02", "ticker": "ABC", "price": None}]}))
    (tmp_path / "balder_views.json").write_text(json.dumps({"n": 1, "by_stance": {"bear": {}}}))
    (tmp_path / "balder_record.json").write_text(json.dumps({"n": 2, "algo": {"n": 1, "mean_ex_qqq": 1.0},
                                                             "open": [{"symbol": "DEF", "ret_pct": None}]}))
    html = R.balder_block()
    assert "ABC" in html and "在场:DEF" in html and "看空" in html
    (tmp_path / "balder_latest.json").write_text(json.dumps({"n": 1, "by_book": [1, 2]}))          # malformed: the section says so
    html = R.balder_block()
    assert "Balder 摘要读不出来" in html and "永远不是信号" in html


# ------------------------------------------------------------------ earnings ----
CAL_CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
           "ABC,A,2030-01-04,2029-12-31,0.1,USD,pre-market\n"          # Fri: session 1
           "ABC,A,2030-01-08,2029-12-31,0.1,USD,pre-market\n"          # a later row: the first one wins
           "DEF,D,2030-01-10,2029-12-31,0.1,USD,post-market\n"         # Thu: session 5
           "GHI,G,2030-01-11,2029-12-31,0.1,USD,pre-market\n"          # Fri: session 6, out
           "JKL,J,2030-01-03,2029-12-31,0.1,USD,post-market\n"         # today after the close
           "MNO,M,2030-01-03,2029-12-31,0.1,USD,pre-market\n"          # today before the open: already traded
           "PQR,P,2030-01-02,2029-12-31,0.1,USD,post-market\n"         # yesterday
           "STU,S,2030-01-03,2029-12-31,0.1,USD,\n"                    # today, time not given: maybe after the close
           "KLM-B,K,2030-01-07,2029-12-31,0.1,USD,\n")                 # the calendar's spelling; no time given
DAY = D(2030, 1, 3)                                                    # a Thursday


def _cal():
    return MB.parse_calendar(CAL_CSV)


def test_earnings_window_is_five_sessions_and_skips_weekends():
    held = ["ABC", "DEF", "GHI", "JKL", "MNO", "PQR", "STU", "KLM.B", "XYZ"]
    got = R.earnings_soon(_cal(), held, DAY)
    assert sorted(got) == ["ABC", "DEF", "JKL", "KLM.B", "STU"]
    assert got["ABC"] == {"date": "2030-01-04", "time": "pre-market", "sessions": 1}
    assert got["DEF"]["sessions"] == 5 and got["JKL"]["sessions"] == 0 and got["KLM.B"]["sessions"] == 2   # Fri, Mon
    assert got["STU"] == {"date": "2030-01-03", "time": "", "sessions": 0}                               # shown as 时间未定
    assert R.earnings_soon(_cal(), held, DAY, n=4).keys() == {"ABC", "JKL", "KLM.B", "STU"}
    # from a Friday: Mon-Fri of the next week; a Saturday report before Monday's open counts, the next Saturday does not
    cal = MB.parse_calendar("symbol,reportDate,timeOfTheDay\nABC,2030-01-05,\nDEF,2030-01-11,post-market\nGHI,2030-01-12,\nJKL,2030-01-14,pre-market\n")
    assert sorted(R.earnings_soon(cal, ["ABC", "DEF", "GHI", "JKL"], D(2030, 1, 4))) == ["ABC", "DEF"]
    # the review run on a Sunday: the same five sessions
    assert sorted(R.earnings_soon(cal, ["ABC", "DEF", "GHI", "JKL"], D(2030, 1, 6))) == ["DEF"]
    assert R.nth_session_after(D(2030, 1, 6), 5) == D(2030, 1, 11)
    assert R.earnings_soon(pd.DataFrame(columns=["ticker", "date", "time"]), held, DAY) == {}


def test_paper_earnings_with_archived_iv(tmp_path, monkeypatch):
    (tmp_path / "earnings_calendar.csv").write_text(CAL_CSV)
    monkeypatch.setattr(MB, "CAL_PATH", tmp_path / "earnings_calendar.csv")
    adb = tmp_path / "archive.db"
    a = duckdb.connect(str(adb))
    a.execute("CREATE TABLE iv_hv (ticker VARCHAR, day DATE, iv DOUBLE, hv DOUBLE, px DOUBLE, fetched DATE)")
    a.execute("""INSERT INTO iv_hv VALUES ('ABC', '2030-01-01', 40, 20, 10, '2030-01-01'), ('ABC', '2030-01-02', 60, 30, 10, '2030-01-02'),
                 ('ABC', '2030-01-04', 99, 1, 10, '2030-01-04'), ('DEF', '2029-12-01', 50, 25, 10, '2029-12-01'),
                 ('STU', '2030-01-02', 'NaN', 30, 10, '2030-01-02')""")
    a.close()
    monkeypatch.setattr(R, "ARCHIVE_DB", adb)
    pos = [{"book": "long", "ticker": "ABC", "qty": 10, "since_entry": 2.0}, {"book": "insider", "ticker": "DEF", "qty": 5, "since_entry": None},
           {"book": "long", "ticker": "XYZ", "qty": 1, "since_entry": 0.0}, {"book": "long", "ticker": "STU", "qty": 2, "since_entry": 1.0}]
    rows, asof = R.paper_earnings(pos, DAY)
    assert asof is not None and [r["ticker"] for r in rows] == ["STU", "ABC", "DEF"]
    stu, abc, dfn = rows
    assert (abc["iv"], abc["hv"], abc["iv_hv"], abc["iv_day"]) == (60, 30, 2.0, "2030-01-02")      # newest on or before the day
    assert dfn["iv"] is None and dfn["sessions"] == 5                                              # archive row too old
    assert (stu["iv"], stu["hv"], stu["iv_hv"], stu["time"]) == (None, 30.0, None, "")              # NaN IV: None, not nan
    duckdb.connect(str(adb)).close()                                       # the read-only connection was closed
    duckdb.connect(str(adb)).execute("DROP TABLE iv_hv").close()
    assert R.iv_hv_latest(["ABC"], DAY) == {}                              # the query fails: still closed
    duckdb.connect(str(adb)).close()
    monkeypatch.setattr(R, "ARCHIVE_DB", tmp_path / "missing.db")
    rows, _ = R.paper_earnings(pos, DAY)
    assert [r["iv"] for r in rows] == [None, None, None]
    monkeypatch.setattr(MB, "CAL_PATH", tmp_path / "none.csv")
    assert R.paper_earnings(pos, DAY) == ([], None)
    (tmp_path / "bad.csv").write_text("Information: the API rate limit was reached")
    monkeypatch.setattr(MB, "CAL_PATH", tmp_path / "bad.csv")
    assert R.paper_earnings(pos, DAY) == ([], None)


# ------------------------------------------------------------------ real vs Balder ----
SESS = [d.date() for d in pd.bdate_range("2030-01-02", "2030-01-18")]


def _feed(tmp_path, rows):
    p = tmp_path / "balder_x_feed.jsonl"
    p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return str(p)


def test_feed_index_dates_and_ticker_words(tmp_path):
    p = _feed(tmp_path, [
        {"id": "1", "saved_at": "2030-01-09T10:00:00", "time": "2030-01-08T03:00:00Z", "text": "$AB 走强"},      # 22:00 ET on 01-07
        {"id": "2", "saved_at": "2030-01-09T10:00:00", "time": "2030-01-08T15:00:00.000Z", "text": "买入ABC,AB 不提"},
        {"id": "3", "saved_at": "2030-01-09T10:00:00", "time": "01-04 pre-open", "text": "ABCD 和 THE END"},
        {"id": "4", "saved_at": "2030-01-02T10:00:00", "time": "12-30", "text": "$DEF"},                          # the year before
        {"id": "4", "enrich": True, "saved_at": "2030-01-03T10:00:00", "time": "12-30", "text": "$DEF 补全"},       # same post
        {"id": "5", "saved_at": "2030-01-09T10:00:00", "time": "", "text": "$GHI,$ABC $ABCD"},   # no date: skipped; its cashtags still count
        "not json", "",
    ])
    idx, first = R.balder_feed_index(p)
    assert first == D(2029, 12, 30)
    assert idx["AB"] == [(D(2030, 1, 7), "1")]                                     # bare AB (2 letters) does not count
    assert idx["ABC"] == [(D(2030, 1, 8), "2")] and idx["ABCD"] == [(D(2030, 1, 4), "3")] and "BCD" not in idx
    assert idx["DEF"] == [(D(2029, 12, 30), "4")] and "GHI" not in idx
    assert "THE" not in idx and "END" not in idx                                   # never written as $THE / $END
    assert R.balder_feed_index(str(tmp_path / "missing.jsonl")) == ({}, None)


def test_bare_words_need_a_cashtag_somewhere_in_the_feed(tmp_path):
    """A made-up post full of capitalised words: only XYZW is a ticker, because another post writes $XYZW."""
    post = {"id": "7", "saved_at": "2030-01-09T10:00:00", "time": "2030-01-08T15:00:00Z",
            "text": "THE PLAN: WAIT FOR XYZW NOW, a LOW open. OPEN 12.50, stop 11.80"}
    idx, _ = R.balder_feed_index(_feed(tmp_path, [post]))
    assert idx == {}                                                               # no cashtag anywhere: no ticker
    idx, _ = R.balder_feed_index(_feed(tmp_path, [post, {"id": "8", "saved_at": "2030-01-10T10:00:00", "time": "2030-01-09T15:00:00Z",
                                                         "text": "$XYZW 继续拿着"}]))
    assert idx == {"XYZW": [(D(2030, 1, 8), "7"), (D(2030, 1, 9), "8")]}


def test_feed_index_skips_bad_lines(tmp_path):
    """An impossible date, a Feb 29 pushed into a non-leap year, bytes that are not UTF-8 and a non-object line are
    skipped; the good line still counts."""
    rows = [{"id": "1", "saved_at": "2030-01-09T10:00:00", "time": "2030-01-08T15:00:00Z", "text": "$ABC"},
            {"id": "2", "saved_at": "2030-03-01T10:00:00", "time": "2030-02-30T15:00:00Z", "text": "$DEF"},
            {"id": "3", "saved_at": "2028-02-28T10:00:00", "time": "02-29", "text": "$GHI"}]
    p = tmp_path / "balder_x_feed.jsonl"
    p.write_bytes(b"\n".join([json.dumps(r).encode() for r in rows] + [b'{"id": "4", "text": "\xff\xfe $JKL"}', b"[1, 2]", b"\x80"]) + b"\n")
    idx, first = R.balder_feed_index(str(p))
    assert idx == {"ABC": [(D(2030, 1, 8), "1")]} and first == D(2030, 1, 8)
    assert R._post_day({"time": "2030-02-30T15:00:00Z"}) is None
    (tmp_path / "dir.jsonl").mkdir()
    with pytest.raises(OSError):                                                   # not a missing file: the caller says so
        R.balder_feed_index(str(tmp_path / "dir.jsonl"))


def _state(tmp_path, rt):
    p = tmp_path / "balder_x_state.json"
    p.write_text(rt if isinstance(rt, str) else json.dumps({"read_through": rt, "todo": []}))
    return str(p)


def test_read_through_is_the_least_read_tab_in_et(tmp_path):
    tab, at = R.balder_read_through(_state(tmp_path, {"subs": "2030-01-14T15:00:00Z", "posts": "2030-01-10T03:30:00Z"}))
    assert tab == "posts" and at == dt.datetime(2030, 1, 9, 22, 30, tzinfo=R.NY)  # 03:30 UTC on 01-10 is 01-09 in New York
    assert R.balder_read_through(_state(tmp_path, {"subs": "2030-01-14T15:00:00.000Z", "posts": "2030-01-14T16:00:00"}))[0] == "subs"
    assert R.balder_read_through(_state(tmp_path, {"subs": "2030-01-14T15:00:00Z"})) == ("posts", None)      # a tab never set
    assert R.balder_read_through(_state(tmp_path, {"subs": "soon", "posts": "2030-01-14T15:00:00Z"})) == ("subs", None)
    for bad in ("not json", "[]", '{"todo": []}', '{"read_through": "2030-01-14"}'):
        assert R.balder_read_through(_state(tmp_path, bad)) == (None, None)
    assert R.balder_read_through(str(tmp_path / "missing.json")) == (None, None)


def test_lookback_is_three_sessions_before_the_deal():
    mon = D(2030, 1, 14)
    assert R.lookback_start(SESS, mon) == D(2030, 1, 9)                            # Wed, Thu, Fri before a Monday
    assert R.lookback_start(SESS, D(2030, 1, 10)) == D(2030, 1, 7)
    assert R.lookback_start(SESS, D(2030, 1, 12)) == D(2030, 1, 9)                 # a Saturday deal (a settlement)
    assert R.lookback_start([], mon) == D(2030, 1, 9) and R.lookback_start(SESS[:1], D(2030, 1, 5)) == D(2030, 1, 2)


READ_ALL = ("posts", dt.datetime(2030, 1, 20, tzinfo=R.NY))                      # both tabs read past every deal below


def test_balder_before_boundaries(tmp_path):
    """Deal on Monday 01-14: window [Wed 01-09, Mon 01-14): the weekend counts, the deal's own day does not."""
    p = _feed(tmp_path, [
        {"id": "a", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-08T15:00:00Z", "text": "$ABC"},   # Tue: before the window
        {"id": "b", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-09T15:00:00Z", "text": "$ABC"},   # Wed: first day, in
        {"id": "c", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-12T15:00:00Z", "text": "ABC"},    # Sat: in
        {"id": "d", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-14T15:00:00Z", "text": "$ABC"},   # the deal's day: out
        {"id": "e", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-10T15:00:00Z", "text": "$DEF"},
    ])
    feed, first = R.balder_feed_index(p)
    d = D(2030, 1, 14)
    start = R.lookback_start(SESS, d)
    views = {"n": 2, "recent": [{"ticker": "ABC", "stance": "bull", "posted": "2030-01-11"}, {"ticker": "ABC", "stance": "bear", "posted": "2030-01-14"}]}
    record = {"n": 3, "open": [{"book": "long", "symbol": "ABC", "opened": "2030-01-02", "closed": None},
                               {"book": "long", "symbol": "ABC", "opened": "2030-01-14", "closed": None}],        # opened on the deal's day
              "week_trades": [{"book": "algo", "symbol": "ABC", "opened": "2030-01-03", "closed": "2030-01-08"},   # closed before the window
                              {"book": "algo", "symbol": "ABC", "opened": "2030-01-07", "closed": "2030-01-09"}]}
    b = R.balder_before("ABC", d, start, feed, first, views, record, READ_ALL)
    assert b["posts"] == 2 and b["post_days"] == ["2030-01-09", "2030-01-12"]
    assert b["views"] == [{"stance": "bull", "posted": "2030-01-11"}]
    assert b["held"] == [{"book": "long", "opened": "2030-01-02", "closed": None}, {"book": "algo", "opened": "2030-01-07", "closed": "2030-01-09"}]
    line = R.balder_line(b)
    assert "01-09 起,不含当天" in line and "提到 2 次(01-09、01-12)" in line and "看多 01-11" in line and "长线 01-02 起" in line
    # nothing about another name; unknown parts say so
    none = R.balder_before("XYZ", d, start, feed, first, views, record, READ_ALL)
    assert (none["posts"], none["views"], none["held"]) == (0, [], [])
    assert "没有提到" in R.balder_line(none) and "公开记录不在场" in R.balder_line(none)
    early = R.balder_before("ABC", D(2030, 1, 9), R.lookback_start(SESS, D(2030, 1, 9)), feed, first, {"n": 40, "recent": views["recent"]}, {}, READ_ALL)
    assert early["posts"] is None and early["views"] is None and early["held"] is None    # feed starts 01-08; 2 of 40 views; no record
    assert "存档不够早" in R.balder_line(early) and "观点记录不够早" in R.balder_line(early)
    full = {"n": 40, "recent": [], "index": [{"ticker": "ABC", "stance": "bear", "posted": "2030-01-07"}]       # every view listed
            + [{"ticker": "DEF", "stance": "bull", "posted": "2029-12-01"}] * 39}
    assert R.balder_before("ABC", D(2030, 1, 9), D(2030, 1, 4), feed, first, full, {}, READ_ALL)["views"] == [{"stance": "bear", "posted": "2030-01-07"}]
    # a partial list (2 of 40) whose oldest view is on the window's first day may have lost others of that day; one a day older may not
    part = lambda day: {"n": 40, "recent": [{"ticker": "ABC", "stance": "bull", "posted": "2030-01-11"}, {"ticker": "DEF", "stance": "bear", "posted": day}]}
    assert R.balder_before("ABC", d, start, feed, first, part("2030-01-09"), {}, READ_ALL)["views"] is None
    assert R.balder_before("ABC", d, start, feed, first, part("2030-01-08"), {}, READ_ALL)["views"] == [{"stance": "bull", "posted": "2030-01-11"}]


def test_balder_line_says_when_the_archive_has_a_gap(tmp_path):
    """The reader saves with gaps; only read_through is gap-free. Not read through to the deal day's 00:00 ET: a mention
    found is shown as it is, none found is not 'did not mention'."""
    feed, first = R.balder_feed_index(_feed(tmp_path, [
        {"id": "a", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-02T15:00:00Z", "text": "$DEF"},
        {"id": "b", "saved_at": "2030-01-15T10:00:00", "time": "2030-01-10T15:00:00Z", "text": "$ABC"}]))
    d, start = D(2030, 1, 14), D(2030, 1, 9)
    views = {"n": 1, "recent": [{"ticker": "ABC", "stance": "bull", "posted": "2030-01-10"}]}
    at = lambda iso: R.balder_read_through(_state(tmp_path, {"subs": "2030-01-20T00:00:00Z", "posts": iso}))
    # 05:00 UTC on Monday 01-14 is 00:00 in New York: the window [01-09, 01-14) is covered; a minute earlier it is not
    ok = R.balder_before("XYZ", d, start, feed, first, views, {}, at("2030-01-14T05:00:00Z"))
    assert ok["gap"] is None and "X 帖子没有提到 · 没有记录的观点" in R.balder_line(ok)
    gap = R.balder_before("XYZ", d, start, feed, first, views, {}, at("2030-01-14T04:59:00Z"))
    assert gap["gap"] == {"tab": "posts", "through": "2030-01-13"} and gap["posts"] == 0 and gap["views"] == []
    line = R.balder_line(gap)
    assert "X 帖子存档有缺口(主页读到 01-13),已存的没有提到" in line and "观点记录有缺口(主页读到 01-13),已存的没有观点" in line
    hit = R.balder_line(R.balder_before("ABC", d, start, feed, first, views, {}, at("2030-01-11T15:00:00Z")))
    assert "X 帖子提到 1 次(01-10)" in hit and "观点 看多 01-10" in hit and "缺口" not in hit             # a mention is real
    assert "X 帖子存档有缺口(没有读帖状态),已存的没有提到" in R.balder_line(R.balder_before("XYZ", d, start, feed, first, views, {}, (None, None)))
    sub = R.balder_before("XYZ", d, start, feed, first, views, {}, R.balder_read_through(_state(tmp_path, {"posts": "2030-01-20T00:00:00Z"})))
    assert "订阅帖读到哪天不知道" in R.balder_line(sub)
    late = R.balder_before("XYZ", D(2030, 1, 3), D(2029, 12, 31), feed, first, {}, {}, (None, None))      # feed starts 01-02
    assert "X 帖子存档不够早" in R.balder_line(late) and "观点记录不够早或缺失" in R.balder_line(late)


def test_render_with_balder_line_and_earnings(tmp_path, monkeypatch):
    """The whole page with no Balder file at all, a deal carrying its Balder line and one earnings row."""
    monkeypatch.setattr(R, "OUT_DIR", str(tmp_path))
    deal = {"ts": "2030-01-14 10:00:00", "ticker": "ABC", "name": "", "side": "BUY", "qty": 1.0, "price": 10.0, "is_option": False,
            "u_ret": 1.0, "u_ret5": 2.0, "after_pct": 0.5, "range_pos": 50.0, "system": [], "tags": [], "balder": "Balder,交易前 3 个交易日:X 帖子没有提到"}
    rep = {"date": "2030-01-14", "generated_at": "2030-01-14T16:30:00", "summary": ["x"], "lessons_30d": {},
           "market": {"spy": {"ret": 0.1, "close": 100.0}, "iwm": {"ret": 0.2}, "vix": {"close": 15.0, "ret": 1.0}},
           "paper": {"books": [], "orders": [], "positions": [], "closed": [], "flags": [], "earnings_cal": "2030-01-13",
                     "earnings": [{"book": "long", "ticker": "DEF", "qty": 5, "date": "2030-01-15", "time": "post-market", "sessions": 1,
                                   "since_entry": -1.0, "iv": 60.0, "hv": 30.0, "iv_hv": 2.0, "iv_day": "2030-01-13"},
                                  {"book": "long", "ticker": "STU", "qty": 2, "date": "2030-01-14", "time": "", "sessions": 0,
                                   "since_entry": 1.0, "iv": math.nan, "hv": 30.0, "iv_hv": math.nan, "iv_day": "2030-01-13"}]},
           "real": {"nav": {}, "deals": [deal], "round_trips": [], "positions": [], "flags": [], "options_30d": [], "window": {}}}
    html = R.render_html(rep)
    assert "<tr class='ref'><td></td><td colspan='7'>Balder,交易前 3 个交易日:X 帖子没有提到</td></tr>" in html
    assert "60% / 30% · 2.00x" in html and "2030-01-15 盘后" in html and "Balder 记分卡" in html
    assert "<td>2030-01-14 时间未定</td><td>0</td><td><span class='pos'>+1.00%</span></td><td>—</td>" in html and "nan" not in html.lower()
    rep["paper"]["earnings_cal"], rep["paper"]["earnings"] = None, []
    assert "还没有财报日历缓存" in R.render_html(rep)
