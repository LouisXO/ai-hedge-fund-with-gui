"""agent.health: failure lines in the latest run of each job, late starts, the evening's exec record,
and the Form 4 fetch runs. Temp files and a temp DuckDB only; launchctl is faked."""
import datetime as dt
import json
import types

import pytest

from agent import health
from agent.sources.sec_daily_form4 import record_run
from hedge_fund.features.panel import PanelStore

TUE_EVE = dt.datetime(2026, 9, 29, 17, 0)          # Tuesday, after the 16:10 job
EXEC_LOG = """=== postclose Tue Sep 29 13:25:02 PDT 2026 ===
[SYNC] bar 2026-09-29 ...
daily archive failed (non-fatal)
Traceback (most recent call last):
=== execute Tue Sep 29 16:10:03 PDT 2026 AGENT_EXEC=on ===
=== form4 realtime Tue Sep 29 16:10:04 PDT 2026 ===
{'days': 1, 'filings': 0, 'rows': 0, 'status': 'failed'}
form4 realtime refresh FAILED (exit 1): the insider book plans on what the panel already holds
bars: {'rows': 10, 'failed_symbols': 0, 'n_batches_failed': 0}
  [100/100] rows 10 symbols with data 10 failed 0
[SUBMITTED] bar 2026-09-29 ...
"""


@pytest.fixture
def logs(tmp_path, monkeypatch):
    ex, col = tmp_path / "execute.log", tmp_path / "collect.log"
    ex.write_text(EXEC_LOG)
    col.write_text("=== weekly fundamentals Sun Sep 27 03:00:01 PDT 2026 ===\nlisting refresh FAILED (exit 1)\n"
                   "=== done Sun Sep 27 03:40:00 PDT 2026 ===\n"
                   "=== form4 daily Mon Sep 28 06:00:02 PDT 2026 ===\n{'status': 'ok'}\n"
                   "=== news Mon Sep 28 13:15:01 PDT 2026 ===\nnews fetch failed: RuntimeError rate limit\n"
                   "=== form4 daily Tue Sep 29 06:47:00 PDT 2026 ===\n{'status': 'ok'}\n")
    jobs = [("com.louis.agent.postclose", "13:25 收盘后", str(ex), r"^=== postclose (.+) ===", range(0, 5), 14),
            ("com.louis.agent.execute", "16:10 下单", str(ex), r"^=== execute (.+?) AGENT_EXEC", range(0, 5), 17),
            ("com.louis.agent.form4", "06:00 Form 4", str(col), r"^=== form4 daily (.+) ===", range(0, 5), 7),
            ("com.louis.agent.news", "13:15 新闻情绪", str(col), r"^=== news (.+) ===", range(0, 5), 14),
            ("com.louis.agent.fundamentals", "周日 基本面", str(col), r"^=== weekly fundamentals (.+) ===", [6], 4)]
    monkeypatch.setattr(health, "JOBS", jobs)
    return {j[0]: j for j in jobs}


def findings(jobs, label):
    _, _, log, _, days, _ = jobs[label]
    return health.run_findings(label, log, days, {})


def test_each_job_reads_only_its_own_latest_run(logs):
    ex = findings(logs, "com.louis.agent.execute")
    assert len(ex) == 1 and ex[0][0] == "bad"
    assert ex[0][1].startswith("最近一次运行(09-29 16:10)关键步骤失败:form4 realtime refresh FAILED (exit 1)")
    pc = findings(logs, "com.louis.agent.postclose")
    assert pc == [("warn", "最近一次运行(09-29 13:25)1 个非关键步骤失败:daily archive failed (non-fatal)")]
    assert findings(logs, "com.louis.agent.fundamentals")[0][0] == "bad"     # its section ends at the next form4 run
    assert findings(logs, "com.louis.agent.news")[0][0] == "warn"
    f4 = findings(logs, "com.louis.agent.form4")
    assert f4 == [("warn", "09-29 06:47 才启动,比计划的 06:00 晚 47 分钟(电脑在睡眠或没插电?)")]


def test_counters_and_field_names_are_not_failures():
    assert health.section_failures(["bars: {'failed_symbols': 0, 'n_batches_failed': 0}",
                                    "  [10/10] rows 5 symbols with data 5 failed 0", "0 failed"]) == ("ok", "")
    assert health.section_failures(["  XYZ failed: HTTP 500"])[0] == "warn"
    assert health.section_failures(["Traceback (most recent call last):", "  File x"]) == \
        ("warn", "日志里有 1 处 Traceback(程序出错)")


def test_health_does_not_read_its_own_report_back(tmp_path):
    # 09-28's execute really failed; health printed that into the log after it and after the next day's runs,
    # which themselves went fine. Neither later run may inherit the FAILED line quoted in health's report.
    log = tmp_path / "execute.log"
    log.write_text("=== execute Mon Sep 28 16:10:02 PDT 2026 AGENT_EXEC=on ===\nexecute FAILED (exit 1)\n"
                   "[bad ] 16:10 下单: 最近一次运行(09-28 16:10)关键步骤失败:execute FAILED (exit 1)\n"
                   "health: bad (1 bad, 0 warn, 40 checks)\n"
                   "=== postclose Tue Sep 29 13:25:03 PDT 2026 ===\n[SYNC] bar 2026-09-29 ok\n"
                   "[bad ] 16:10 下单: 最近一次运行(09-28 16:10)关键步骤失败:execute FAILED (exit 1)\n"
                   "[warn] 13:25 收盘后: 1 个非关键步骤失败:daily archive failed (non-fatal)\n"
                   "health: bad (1 bad, 1 warn, 40 checks)\n"
                   "=== execute Tue Sep 29 16:10:02 PDT 2026 AGENT_EXEC=on ===\n[SUBMITTED] bar 2026-09-29 ok\n"
                   "[bad ] 13:25 收盘后: 最近一次运行(09-29 13:25)关键步骤失败:[bad ] 16:10 下单: execute FAILED\n")
    for label in ("com.louis.agent.postclose", "com.louis.agent.execute"):
        assert health.run_findings(label, str(log), range(0, 5), {}) == [], label
    assert health.section_failures(["[warn] x: Traceback in failed step", "health: warn (0 bad, 1 warn)"]) == ("ok", "")


def test_yfinance_delisted_noise_is_not_a_failure():
    brief = ["50 Failed downloads:", "1 Failed download:",
             "Failed to get ticker 'PX' reason: Failed to perform, curl: (27) . See https://curl.se/libcurl/c/libcurl-errors.html",
             "Cookie/crumb fetch failed (RequestException), continuing without crumb",
             "['RDC', 'AET']: possibly delisted; no timezone found",
             "['BEN', 'ATI']: OperationalError('unable to open database file')",
             "$VRSN: possibly delisted; no price data found  (1d 2026-09-19 -> 2026-09-29)",
             "agent: panel refresh failed, scoring on the existing panel: No objects to concatenate"]
    assert health.section_failures(brief) == ("ok", "")
    # the brief's own steps still count
    assert health.section_failures(brief + ["site publish failed (non-fatal)"]) == \
        ("warn", "1 个非关键步骤失败:site publish failed (non-fatal)")
    assert health.section_failures(brief + ["agent: compute failed: IO Error: Could not set lock"])[0] == "warn"


def test_late_start_across_midnight_belongs_to_the_evening_before():
    t = dt.datetime(2026, 9, 30, 0, 30)                                      # Wednesday 00:30
    assert health.scheduled_before(t, 16, 10, range(0, 5)) == dt.datetime(2026, 9, 29, 16, 10)
    assert health.scheduled_before(dt.datetime(2026, 9, 26, 7, 0), 6, 0, range(0, 5)) == dt.datetime(2026, 9, 25, 6, 0)


def test_late_start_findings(tmp_path):
    log = tmp_path / "execute.log"
    log.write_text("=== execute Wed Sep 30 00:30:00 PDT 2026 AGENT_EXEC=on ===\n[SUBMITTED] ok\n")
    assert health.run_findings("com.louis.agent.execute", str(log), range(0, 5), {}) == \
        [("warn", "09-30 00:30 才启动,比计划的 09-29 16:10 晚 500 分钟(电脑在睡眠或没插电?)")]
    log.write_text("=== postclose Wed Sep 30 10:00:00 PDT 2026 ===\n")      # a manual run in the morning: not late
    assert health.run_findings("com.louis.agent.postclose", str(log), range(0, 5), {}) == []


def test_check_jobs_folds_the_log_into_the_job_row(logs, monkeypatch):
    listing = "\n".join(f"-\t0\t{label}" for label in logs)
    monkeypatch.setattr(health.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=listing))
    rows = {r["label"]: r for r in health.check_jobs(TUE_EVE)}
    assert rows["com.louis.agent.execute"]["level"] == "bad"
    assert "上次 09-29 16:10" in rows["com.louis.agent.execute"]["detail"]
    assert "关键步骤失败" in rows["com.louis.agent.execute"]["detail"]
    assert rows["com.louis.agent.postclose"]["level"] == "warn"


# ---- the evening's exec record ----

def write_exec(d, day, **kw):
    j = {"as_of": str(day), "last_session": str(day), "mode": "submit", "skipped_reason": None, "reconcile": [],
         "orders": [{"ticker": "ABC", "status": "accepted"}], "generated_at": f"{day}T16:14:07"}
    j.update(kw)
    (d / f"exec_{day}.json").write_text(json.dumps(j))


def test_exec_record(tmp_path):
    day = TUE_EVE.date()
    r = health.check_exec(TUE_EVE, str(tmp_path))
    assert r["level"] == "bad" and "没有留下执行记录 exec_2026-09-29.json" in r["detail"]
    write_exec(tmp_path, day)
    r = health.check_exec(TUE_EVE, str(tmp_path))
    assert (r["level"], r["detail"]) == ("ok", "2026-09-29 已提交 1 张订单,对账一致,16:14 写入")
    write_exec(tmp_path, day, mode="dry_run", reconcile=["ABC: lots 10 vs broker 0"],
               orders=[{"ticker": "XYZ", "status": "error: insufficient buying power"}])
    r = health.check_exec(TUE_EVE, str(tmp_path))
    assert r["level"] == "bad"
    for part in ("模式是 dry_run", "1 张订单被券商拒绝:XYZ", "账本和券商持仓对不上:ABC"):
        assert part in r["detail"]
    for cc, level in ((True, "ok"), ({"ok": False, "detail": "books 12,000 > broker 9,000"}, "bad"),
                      ({"status": "ok"}, "ok"), ([], "ok"), (["short 100"], "bad"), (42, "warn")):
        write_exec(tmp_path, day, cash_check=cc)
        assert health.check_exec(TUE_EVE, str(tmp_path))["level"] == level, cc
    # agent.execute's format (S48 package A): ok None = not checked; its own order states for a failed POST
    for cc, level, part in (
            ({"books_cash": 100.0, "broker_cash": 100.0, "diff": 0.0, "orders_in_flight": 0, "ok": True}, "ok", "现金核对通过"),
            ({"books_cash": 100.0, "broker_cash": 90.0, "diff": -10.0, "orders_in_flight": 0, "ok": False}, "bad", "现金核对不通过"),
            ({"books_cash": 100.0, "broker_cash": 90.0, "diff": None, "orders_in_flight": 2, "ok": None}, "warn",
             "现金核对没做:有 2 张订单在途"),
            ({"books_cash": 100.0, "broker_cash": None, "diff": None, "orders_in_flight": 0, "ok": None}, "warn",
             "现金核对没做:拿不到券商的现金数")):
        write_exec(tmp_path, day, cash_check=cc)
        r = health.check_exec(TUE_EVE, str(tmp_path))
        assert r["level"] == level and part in r["detail"], (cc, r)
    write_exec(tmp_path, day, orders=[{"ticker": "AAA", "status": "submit_unknown"}, {"ticker": "BBB", "status": "not_sent"},
                                      {"ticker": "CCC", "status": "rejected"}, {"ticker": "DDD", "status": "filled"}],
               not_attempted=["long|2026-09-29|EEE|buy"])
    r = health.check_exec(TUE_EVE, str(tmp_path))
    assert r["level"] == "bad"
    for part in ("1 张订单被券商拒绝:CCC", "1 张订单没发出去", "1 张订单不知道有没有到券商", "1 张订单没有尝试发送"):
        assert part in r["detail"], part
    assert "DDD" not in r["detail"]
    write_exec(tmp_path, day, started_at="2026-09-29T16:55:00")
    r = health.check_exec(TUE_EVE, str(tmp_path))
    assert r["level"] == "warn" and "比 16:10 晚 45 分钟" in r["detail"]


def test_exec_record_before_the_job_checks_the_previous_session_and_knows_holidays(tmp_path):
    write_exec(tmp_path, dt.date(2026, 9, 25))                                       # Friday
    assert health.check_exec(dt.datetime(2026, 9, 28, 9, 0), str(tmp_path))["level"] == "ok"      # Monday morning
    assert health.check_exec(dt.datetime(2026, 9, 28, 16, 20), str(tmp_path))["level"] == "ok"     # job still running
    assert health.check_exec(dt.datetime(2026, 9, 28, 16, 45), str(tmp_path))["level"] == "bad"
    # Monday a holiday: the evening run rewrote Friday's file, and the calendar in it says Friday was the last session
    write_exec(tmp_path, dt.date(2026, 9, 25), generated_at="2026-09-28T16:12:00")
    r = health.check_exec(dt.datetime(2026, 9, 28, 17, 0), str(tmp_path))
    assert (r["level"], r["detail"]) == ("ok", "2026-09-28 休市(券商日历),当晚没有订单要下")
    # stale bars: the run knew Monday was a session but planned from Friday's bar
    write_exec(tmp_path, dt.date(2026, 9, 25), generated_at="2026-09-28T16:12:00", last_session="2026-09-28",
               skipped_reason="bar date != last session")
    r = health.check_exec(dt.datetime(2026, 9, 28, 17, 0), str(tmp_path))
    assert r["level"] == "bad" and "bar date != last session" in r["detail"]


# ---- Form 4 fetch runs ----

def add_run(db, source, at, status, n_items=0, n_rows=0, reason=None):
    with PanelStore(db) as s:
        record_run(s.con, {"source": source, "started_at": at, "finished_at": at, "status": status, "n_requests": 1,
                           "n_failed": int(status != "ok"), "n_items": n_items, "n_rows": n_rows, "reason": reason})


def test_fetch_runs(tmp_path):
    db = str(tmp_path / "panel.db")
    PanelStore(db).close()
    rows = health.check_fetch_runs(TUE_EVE, db)
    assert [r["level"] for r in rows] == ["warn", "warn"] and "还没有抓取记录" in rows[0]["detail"]
    add_run(db, "form4_daily", dt.datetime(2026, 9, 29, 6, 5), "ok", 900, 40)
    add_run(db, "form4_realtime", dt.datetime(2026, 9, 28, 16, 11), "ok", 0, 0)
    rt, daily = health.check_fetch_runs(TUE_EVE, db)
    assert (rt["level"], daily["level"]) == ("bad", "ok")
    assert rt["detail"].startswith("2026-09-29 的抓取记录还没有写入(记录在运行结束时写")
    assert daily["detail"] == "2026-09-29 06:05 正常,900 份申报,新增 40 行"
    monday = health.check_fetch_runs(dt.datetime(2026, 9, 29, 9, 0), db)[0]          # next morning: last night's run
    assert (monday["level"], monday["detail"]) == ("ok", "2026-09-28 16:11 正常,当天没有新申报")
    add_run(db, "form4_realtime", dt.datetime(2026, 9, 29, 16, 11), "failed", reason="could not list: efts search")
    rt = health.check_fetch_runs(dt.datetime(2026, 9, 29, 16, 20), db)[0]              # job just ran
    assert rt["level"] == "bad" and "抓取失败,不能当作没有申报:could not list: efts search" in rt["detail"]
    add_run(db, "form4_realtime", dt.datetime(2026, 9, 29, 16, 30), "partial", reason="2 of 50 filings not fetched")
    rt = health.check_fetch_runs(TUE_EVE, db)[0]
    assert rt["level"] == "warn" and "2 of 50" in rt["detail"]
