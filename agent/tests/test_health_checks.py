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
                                    "  [10/10] rows 5 symbols with data 5 failed 0", "0 failed",
                                    "{'allowed': 20, 'queue': 2959, 'ok': 20, 'failed': 0, 'rows': 673}"]) == ("ok", "")
    assert health.section_failures(["{'ok': 18, 'failed': 2}"])[0] == "warn"
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


# ---- who wrote the long book's lists ----

def add_lists(db, as_of, run_id, signals, n=3):
    from agent import ledger
    c = ledger.connect(db)
    try:
        ledger.ensure_schema(c)
        c.execute("DELETE FROM agent_picks WHERE as_of = ? AND signal_name IN (SELECT unnest(?))", [as_of, list(signals)])
        ledger.write_picks(c, [{"as_of": as_of, "ticker": f"T{i}", "signal_name": s, "side": "L", "rank": i + 1, "run_id": run_id}
                               for s in signals for i in range(n)])
    finally:
        c.close()


def drop_lists(db, as_of):
    from agent import ledger
    c = ledger.connect(db)
    try:
        c.execute("DELETE FROM agent_picks WHERE as_of = ?", [as_of])
    finally:
        c.close()


def weekdays(a, b):
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5]


def test_list_writers(tmp_path):
    db = str(tmp_path / "optradar.db")
    v1, v2, lc, clm, clv = "composite_long", "composite_long_v2", "largecap_qlv", "long_v2_clM", "long_v2_clV"
    for d in weekdays(dt.date(2026, 10, 1), dt.date(2026, 10, 15)):
        add_lists(db, d, "exec", [v1, v2, lc])
    r = health.check_list_writers(dt.datetime(2026, 10, 15, 17, 0), db)
    assert (r["level"], r["detail"]) == ("ok", "最近 10 个交易日的 10 天名单都由 16:10 下单任务写入")
    add_lists(db, dt.date(2026, 10, 5), "2026-10-05-bd50ba", [v1, v2])          # older than 10 sessions on 10-19: not read
    add_lists(db, dt.date(2026, 10, 7), "2026-10-08-75f33e", [v1, v2])          # in LIST_RUNS_CHECKED: a warning
    add_lists(db, dt.date(2026, 10, 16), "2026-10-19-c47b53", [v1, v2])
    for sig in (clm, clv):
        add_lists(db, dt.date(2026, 10, 16), "shadow_v2", [sig])                  # the v2 cluster lists are not read
    add_lists(db, dt.date(2026, 10, 19), "exec", [v1, v2, lc])
    r = health.check_list_writers(dt.datetime(2026, 10, 19, 17, 0), db)
    assert (r["level"], r["detail"]) == ("bad",
        "不是 16:10 下单任务写的名单:2026-10-16 composite_long、composite_long_v2 由 2026-10-19-c47b53 写入。"
        "偏离检查、评估和归因都按名单重放,先核对当天日线;"
        "2026-10-07 composite_long、composite_long_v2 由 2026-10-08-75f33e 写入,10-19 核对过,和重算一致")
    assert "2026-10-05" not in r["detail"] and "clM" not in r["detail"]
    drop_lists(db, dt.date(2026, 10, 12))                                          # a day the bars guard skipped
    r = health.check_list_writers(dt.datetime(2026, 10, 19, 17, 0), db)
    assert r["level"] == "bad" and r["detail"].endswith(";没有长线名单:2026-10-12(16:10 任务没写;早报遇到日线不全也不写)")
    r = health.check_list_writers(dt.datetime(2026, 10, 20, 15, 0), db)          # 10-20's list is not due yet: not missing
    assert "2026-10-20" not in r["detail"]
    for d in weekdays(dt.date(2026, 10, 20), dt.date(2026, 10, 30)):
        add_lists(db, d, "exec", [v1, v2, lc])
    r = health.check_list_writers(dt.datetime(2026, 10, 21, 17, 0), db)          # 10-07 has left the window
    assert r["level"] == "bad" and "2026-10-07" not in r["detail"] and "2026-10-16" in r["detail"]
    add_lists(db, dt.date(2026, 10, 16), "repair-2026-10-19", [v1, v2, lc])       # rewritten after the fact
    r = health.check_list_writers(dt.datetime(2026, 10, 21, 17, 0), db)
    assert (r["level"], r["detail"]) == ("warn", "2026-10-16 composite_long、composite_long_v2、largecap_qlv 是事后修复"
                                                 "(repair-2026-10-19),不是当天的数据;"
                                                 "没有长线名单:2026-10-12(16:10 任务没写;早报遇到日线不全也不写)")
    r = health.check_list_writers(dt.datetime(2026, 10, 30, 17, 0), db)          # 10-12 and 10-16 left it too
    assert (r["level"], r["detail"]) == ("ok", "最近 10 个交易日的 10 天名单都由 16:10 下单任务写入")
    r = health.check_list_writers(dt.datetime(2026, 12, 1, 17, 0), db)
    assert (r["level"], r["detail"]) == ("warn", "最近 10 个交易日没有长线名单")


def _shadow(tmp_path, **kw):
    rep = {"generated_at": "2026-10-22T13:31:00", "first_event_day": "2026-10-21", "n_events": 14, "n_with_E1": 9,
           "sprt": {"n": 9, "mean_pct": 0.31, "lower_pct": -22.56, "upper_pct": 40.53, "decision": "none", "decided": None},
           "exit_readings": {"due": "2027-06-30", "status": "hidden until 2027-06-30"}, "error": None, **kw}
    p = tmp_path / "insider_shadow.json"
    p.write_text(json.dumps(rep))
    return str(p)


def test_insider_shadow_report(tmp_path):
    thu_eve = dt.datetime(2026, 10, 22, 17, 0)
    r = health.check_insider_shadow(thu_eve, str(tmp_path / "missing.json"))
    assert r["level"] == "warn" and "还没有" in r["detail"]
    r = health.check_insider_shadow(dt.datetime(2026, 10, 21, 16, 30), str(tmp_path / "missing.json"))   # deployed, not run yet
    assert r["level"] == "ok" and "第一次运行在 10-22 13:25" in r["detail"]
    r = health.check_insider_shadow(thu_eve, _shadow(tmp_path))
    assert r["level"] == "ok" and "14 个事件(有 E1 的 9)" in r["detail"] and "SPRT 未越线" in r["detail"]
    assert "2027-06-30 之前不看" in r["detail"]
    r = health.check_insider_shadow(dt.datetime(2026, 10, 23, 17, 0), _shadow(tmp_path))      # Friday: Thursday's report
    assert r["level"] == "warn" and "10-23 的没有写" in r["detail"]
    r = health.check_insider_shadow(thu_eve, _shadow(tmp_path, error="RuntimeError: panel locked", error_at="2026-10-22T13:40"))
    assert r["level"] == "warn" and "上次运行失败" in r["detail"] and "panel locked" in r["detail"]
    dec = {"decision": "H0", "n": 830, "entry_day": "2027-09-14", "truncated": False}
    r = health.check_insider_shadow(thu_eve, _shadow(tmp_path, sprt={"n": 830, "mean_pct": -0.11, "lower_pct": -0.11,
                                                                      "upper_pct": 0.57, "decision": "H0", "decided": dec}))
    assert r["level"] == "ok" and '判"无"(第 830 个事件,2027-09-14)' in r["detail"]


def test_the_index_freshness_check_reads_the_oldest_of_spy_iwm_and_qqq(tmp_path):
    # 2026-10-21 review: only SPY was read, so a QQQ (the pages' benchmark) or an IWM (the long alarm) basis_guard kept
    # back showed nowhere; ^VIX is not read, as before
    import pandas as pd
    name, _, sql, warn, bad = next(x for x in health.DATA if x[0].startswith("指数"))
    assert name == "指数 SPY/IWM/QQQ" and (warn, bad) == (1, 3)
    last = {"SPY": dt.date(2026, 10, 21), "IWM": dt.date(2026, 10, 21), "QQQ": dt.date(2026, 10, 20), "^VIX": dt.date(2026, 10, 19)}
    with PanelStore(tmp_path / "p.db") as s:
        s.upsert_index(pd.DataFrame([{"symbol": k, "trade_date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adj_close": 1.0,
                                      "source": "t", "fetched_at": pd.Timestamp("2026-10-21")} for k, v in last.items()
                                     for d in (v - dt.timedelta(days=1), v)]))
        assert s.con.execute(sql).fetchone()[0] == dt.date(2026, 10, 20)
        s.con.execute("DELETE FROM index_daily WHERE symbol = 'QQQ' AND trade_date = '2026-10-20'")
        s.con.execute("INSERT INTO index_daily VALUES ('QQQ', '2026-10-21', 1, 1, 1, 1, 1, 't', now())")
        assert s.con.execute(sql).fetchone()[0] == dt.date(2026, 10, 21)        # all three current: SPY's day, as before
