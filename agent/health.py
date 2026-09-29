"""System health: every scheduled job, every dataset's freshness, logins, power, disk, backup.

Why: in the week of 2026-09-21 four failures were found only by accident — a job that was never
loaded, an expired Claude login (the weekly report failed silently), the Mac sleeping through a
load, an exhausted API quota. This runs at the end of every scheduled job, writes
out/health.json, is rendered on the private front page, and notifies when something is red.

Levels: ok / warn (degraded, the system still trades and reports) / bad (something the books or
the record depend on is broken). Read-only everywhere; the Claude login probe is one tiny call.

S48 (2026-09-29): a job counted as healthy when launchd started it, whatever its steps did. Now each job's
latest run is read from its log (a FAILED line from a book-feeding step is bad, other failed lines and
Tracebacks warn, a start more than 30 minutes after the schedule warns); the evening's exec_<date>.json
must exist in submit mode with no rejected order, an empty reconcile and a passing cash check; and the
Form 4 fetches must have logged an ok run in panel.fetch_runs (so "no filings" and "fetch failed" differ).
health's own report lines and yfinance's delisted-ticker noise in the same logs are not read as failures; an
order the broker may not have (not_sent, submit_unknown, not attempted) is bad like a rejected one; a cash
check that was not run (ok None: orders in flight or no broker cash) warns.

Usage: python -m agent.health [--no-notify] [--no-llm-probe]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess

import duckdb

from hedge_fund.paths import AGENT_DIR

OUT = "/Users/louis/optradar/out"
HEALTH = os.path.join(OUT, "health.json")
A = str(AGENT_DIR)
JOBS = [  # label, what, log, marker regex, weekdays it runs (0 = Mon), hour it should have run by
    ("com.louis.optradar", "08:41 早报", f"{OUT}/cron.log", r"^=== (?!weekly)(.+?) (?:start|done) ===", range(0, 5), 9),
    ("com.louis.agent.postclose", "13:25 收盘后(复盘、快照、备份)", f"{OUT}/agent/execute.log", r"^=== postclose (.+) ===", range(0, 5), 14),
    ("com.louis.agent.execute", "16:10 下单", f"{OUT}/agent/execute.log", r"^=== execute (.+?) AGENT_EXEC", range(0, 5), 17),
    ("com.louis.agent.form4", "06:00 Form 4", f"{OUT}/agent/collect.log", r"^=== form4 daily (.+) ===", range(0, 5), 7),
    ("com.louis.agent.news", "13:15 新闻情绪", f"{OUT}/agent/collect.log", r"^=== news (.+) ===", range(0, 5), 14),
    ("com.louis.optradar.weekly", "周六 09:30 周报", None, None, [5], 10),
    ("com.louis.agent.fundamentals", "周日 03:00 基本面 + 审计 + 周备份", f"{OUT}/agent/collect.log", r"^=== weekly fundamentals (.+) ===", [6], 4),
    ("com.louis.optradar.private", "私有站 HTTP 服务", None, None, None, None),
    ("com.louis.power", "电源策略", None, None, None, None),
]
# The line that opens a run in the job's log (group 1 = `date` output) and the scheduled start (PT). A run's
# section ends where the next run of any job writing to the same log begins.
RUN_START = {
    "com.louis.optradar": (r"^=== (?!weekly)(.+?) start ===", (8, 41)),
    "com.louis.agent.postclose": (r"^=== postclose (.+) ===", (13, 25)),
    "com.louis.agent.execute": (r"^=== execute (.+?) AGENT_EXEC", (16, 10)),
    "com.louis.agent.form4": (r"^=== form4 daily (.+) ===", (6, 0)),
    "com.louis.agent.news": (r"^=== news (.+) ===", (13, 15)),
    "com.louis.agent.fundamentals": (r"^=== weekly fundamentals (.+) ===", (3, 0)),
}
OTHER_RUN_STARTS = {f"{OUT}/cron.log": [r"^=== weekly"]}      # jobs not in RUN_START that write to a shared log
LATE_MIN = 30
LATE_MAX_MIN = 12 * 60        # a start more than 12 hours after the schedule is taken as a manual run, not a late one
FAILED_RE = re.compile(r"\bFAILED\b")                        # a book-feeding step (agent/bin/*.sh) failed
SOFT_FAIL_RE = re.compile(r"\b[Ff]ailed\b")                  # any other step
ZERO_FAIL_RE = re.compile(r"\bfailed:?\s+0\b|\b0 failed\b")   # progress counters ("... failed 0")
# Lines never read as a failure of the run they sit in. health's own report ('[bad ] name: detail', 'health: ...')
# lands in the same logs and quotes the failure lines it found: read back, one FAILED would stay red forever
# and spread to every job sharing the log. yfinance prints the next ones for long-delisted names on every brief;
# the brief's yfinance bar top-up is best effort by design (agent/daily.py: the after-close Alpaca update holds
# the bar, and a stale panel shows in the data checks below).
IGNORE_RE = re.compile(r"^(?:\[(?:bad |warn|ok  )\] |health: "
                       r"|\d+ Failed downloads?:|Failed to get ticker\b|Cookie/crumb fetch failed\b"
                       r"|\[['\"][^\]]*\]: |\$\S+: possibly delisted"
                       r"|agent: panel refresh failed, scoring on the existing panel)")
EXEC_DUE, EXEC_GRACE_MIN = (16, 10), 30
ORDER_PROBLEMS = [  # exec_<date>.json order status (error*/rejected* folded into 'rejected') -> what it means
    ("rejected", "被券商拒绝"),
    ("not_sent", "没发出去(券商那边没有这张单,重跑会再发)"),
    ("submit_unknown", "不知道有没有到券商(发送和查询都失败,下次同步再查)"),
]
FETCH_CHECKS = [  # fetch_runs.source, name, scheduled start, minutes before a missing run counts, level if missing/failed
    ("form4_realtime", "Form 4 实时抓取(16:10,内部人书当晚的信号)", (16, 10), 30, "bad"),
    ("form4_daily", "Form 4 每日索引(06:00)", (6, 0), 60, "warn"),
]
RANK = {"ok": 0, "warn": 1, "bad": 2}

DATA = [  # name, db, sql for the latest date, max age in sessions before warn / bad
    ("价格日线 bars", f"{A}/panel.db", "SELECT max(trade_date) FROM bars", 1, 3),
    ("指数 SPY/VIX", f"{A}/panel.db", "SELECT max(trade_date) FROM index_daily WHERE symbol = 'SPY'", 1, 3),
    ("内部人 Form 4", f"{A}/panel.db", "SELECT max(filing_date) FROM insider_tx", 2, 4),
    ("基本面 XBRL", f"{A}/panel.db", "SELECT max(filed) FROM fundamentals_pit", 8, 15),
    # the OLDEST tenth of the companies, not the newest fetch: one re-fetched company must not make the table look fresh
    ("基本面刷新(最旧的一成公司)", f"{A}/panel.db", "SELECT CAST(quantile_cont(fetched_at, 0.1) AS DATE) FROM xbrl_load_log WHERE status = 'ok'", 8, 12),
    ("新闻 Alpaca", f"{A}/news.db", "SELECT CAST(max(created_at) AS DATE) FROM news_items", 2, 4),
    ("期权日线", f"{A}/options.db", "SELECT max(trade_date) FROM opt_bars", 6, 12),
    ("空头持仓 FINRA", f"{A}/short.db", "SELECT max(public_date) FROM short_interest", 15, 25),
    ("SEC 增发/144", f"{A}/filings.db", "SELECT max(filed) FROM sec_forms", 2, 4),
    ("开盘竞价", f"{A}/auctions.db", "SELECT max(day) FROM auctions", 2, 5),
    ("盘中 30 分钟线", f"{A}/intraday.db", "SELECT CAST(max(ts) AS DATE) FROM bars30", 2, 5),
    ("盘中 5 分钟线", f"{A}/intraday.db", "SELECT CAST(max(ts) AS DATE) FROM bars5", 2, 5),
    ("IV/HV 存档", f"{A}/archive.db", "SELECT max(day) FROM iv_hv", 2, 5),
    ("分析师共识存档", f"{A}/archive.db", "SELECT max(day) FROM consensus", 2, 5),
    ("难借券存档", f"{A}/archive.db", "SELECT max(day) FROM borrow", 2, 5),
    ("盈利预测 AV", f"{A}/estimates.db", "SELECT CAST(max(fetched_at) AS DATE) FROM av_estimates_log", 3, 7),
    ("模拟盘净值", "/Users/louis/optradar/optradar.db", "SELECT max(as_of) FROM agent_book_nav", 1, 2),
    ("竞价口径净值", "/Users/louis/optradar/optradar.db", "SELECT max(as_of) FROM agent_auction_nav", 1, 3),
    ("实盘快照 moomoo", "/Users/louis/optradar/optradar.db", "SELECT max(date) FROM acct_nav", 1, 3),
]


def sessions_back(d: dt.date, today: dt.date) -> int:
    """Weekdays strictly after d up to today (holidays count as sessions: a warn on a holiday is harmless)."""
    n, cur = 0, d
    while cur < today:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def last_session(today: dt.date, now: dt.datetime) -> dt.date:
    d = today if (today.weekday() < 5 and now.hour >= 14) else today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def prev_weekday(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def due_day(now: dt.datetime, hhmm: tuple[int, int], grace_min: int, have_today: bool) -> dt.date:
    """The latest weekday whose run should be finished: today once the grace period has passed (or as soon as
    today's record exists), else the weekday before. Holidays count as weekdays; the checks handle them."""
    sched = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    if now.weekday() < 5 and (now >= sched + dt.timedelta(minutes=grace_min) or (now >= sched and have_today)):
        return now.date()
    return prev_weekday(now.date())


def parse_stamp(text: str) -> dt.datetime | None:
    """`date` output ('Tue Sep 29 16:10:02 PDT 2026') -> naive local time."""
    try:
        return dt.datetime.strptime(re.sub(r" [A-Z]{3,4} ", " ", text.strip()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


def parse_iso(v) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(v)).replace(tzinfo=None) if v else None
    except ValueError:
        return None


def read_lines(path: str, cache: dict) -> list[str]:
    if path not in cache:
        try:
            with open(path, errors="ignore") as f:
                cache[path] = f.read().splitlines()
        except OSError:
            cache[path] = []
    return cache[path]


def last_run_section(lines: list[str], start_re: str, stop_res: list[str]) -> tuple[str | None, list[str]]:
    """(stamp on the start line, the run's lines): from the last line matching start_re up to the next line
    that opens a run of any job writing to the same log."""
    starts = [i for i, line in enumerate(lines) if re.match(start_re, line.strip())]
    if not starts:
        return None, []
    i = starts[-1]
    stamp = re.match(start_re, lines[i].strip()).group(1)
    out = []
    for line in lines[i + 1:]:
        if any(re.match(r, line.strip()) for r in stop_res):
            break
        out.append(line)
    return stamp, out


def section_failures(lines: list[str]) -> tuple[str, str]:
    """(level, detail): a FAILED line (a book-feeding step in agent/bin/*.sh) is bad; any other 'failed' line
    or a Traceback is a warning. Progress counters such as 'failed 0', health's own report lines and yfinance's
    delisted-ticker noise are not failures."""
    lines = [line.strip() for line in lines if not IGNORE_RE.match(line.strip())]
    hard = [line for line in lines if FAILED_RE.search(line)]
    soft = [line for line in lines if not FAILED_RE.search(line) and SOFT_FAIL_RE.search(ZERO_FAIL_RE.sub("", line))]
    tracebacks = sum(1 for line in lines if line.lstrip().startswith("Traceback"))
    if hard:
        return "bad", f"关键步骤失败:{hard[0][:90]}" + (f" 等 {len(hard)} 行" if len(hard) > 1 else "")
    if soft:
        return "warn", f"{len(soft)} 个非关键步骤失败:{soft[0][:80]}" + (" 等" if len(soft) > 1 else "")
    if tracebacks:
        return "warn", f"日志里有 {tracebacks} 处 Traceback(程序出错)"
    return "ok", ""


def scheduled_before(t: dt.datetime, hh: int, mm: int, days) -> dt.datetime | None:
    """The latest scheduled start at or before t: a 16:10 run that only started at 00:30 belongs to the evening
    before (a start the next morning is then 8 hours late, not 16 hours early)."""
    s = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if s > t:
        s -= dt.timedelta(days=1)
    for _ in range(7):
        if s.weekday() in days:
            return s
        s -= dt.timedelta(days=1)
    return None


def run_findings(label: str, log: str | None, days, cache: dict) -> list[tuple[str, str]]:
    """(level, detail) for the latest run of a job read from its log: failure lines, and a late start."""
    if label not in RUN_START or not log or not os.path.exists(log):
        return []
    start_re, (hh, mm) = RUN_START[label]
    stops = [RUN_START[j[0]][0] for j in JOBS if j[2] == log and j[0] in RUN_START] + OTHER_RUN_STARTS.get(log, [])
    stamp, lines = last_run_section(read_lines(log, cache), start_re, stops)
    if stamp is None:
        return []
    t = parse_stamp(stamp)
    when = t.strftime("%m-%d %H:%M") if t else stamp
    out = []
    level, text = section_failures(lines)
    if level != "ok":
        out.append((level, f"最近一次运行({when}){text}"))
    sched = scheduled_before(t, hh, mm, days) if t and days is not None else None
    if sched is not None:
        late = (t - sched).total_seconds() / 60
        if LATE_MIN < late <= LATE_MAX_MIN:
            planned = f"{hh:02d}:{mm:02d}" if sched.date() == t.date() else f"{sched:%m-%d %H:%M}"
            out.append(("warn", f"{when} 才启动,比计划的 {planned} 晚 {late:.0f} 分钟(电脑在睡眠或没插电?)"))
    return out


def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def cash_check_ok(cc) -> tuple[bool | None, str]:
    """(passed?, note) for exec_<date>.json's cash_check, whatever shape it has; None = shape not understood."""
    if isinstance(cc, bool):
        return cc, ""
    if isinstance(cc, str):
        return cc.strip().lower() in ("ok", "pass", "passed"), cc
    if isinstance(cc, list):
        return not cc, "; ".join(map(str, cc))
    if isinstance(cc, dict):
        note = str(cc.get("detail") or cc.get("note") or cc.get("message") or "")
        for k in ("ok", "passed"):
            if k in cc:
                return bool(cc[k]), note or str(cc)
        for k in ("status", "level", "result"):
            if k in cc:
                return str(cc[k]).lower() in ("ok", "pass", "passed"), note or str(cc)
    return None, str(cc)


def check_exec(now: dt.datetime, out_dir: str = f"{OUT}/agent") -> dict:
    """The evening order job's record: exec_<day>.json exists, submit mode, nothing rejected, reconcile empty,
    cash check passed (when the file has one), started on time (when the file says when it started)."""
    r = {"name": "16:10 下单记录", "label": "exec_record", "level": "ok", "detail": ""}
    due = due_day(now, EXEC_DUE, EXEC_GRACE_MIN, os.path.exists(os.path.join(out_dir, f"exec_{now.date()}.json")))
    path = os.path.join(out_dir, f"exec_{due}.json")
    j = _load_json(path)
    if j is None:
        # The file is named by the bar date. On a holiday the evening run rewrites the last session's file and
        # its last_session (the broker's calendar) is not the holiday; with stale bars it is, and nothing was planned.
        ran = [x for x in (_load_json(os.path.join(out_dir, f"exec_{due - dt.timedelta(days=k)}.json")) for k in range(1, 8))
               if x and str(x.get("generated_at", ""))[:10] == str(due)]
        if ran and all(x.get("last_session") != str(due) for x in ran):
            r["detail"] = f"{due} 休市(券商日历),当晚没有订单要下"
        elif ran:
            r.update(level="bad", detail=f"{due} 的任务只拿到 {ran[0].get('as_of')} 的日线,没有计划订单:"
                                         f"{ran[0].get('skipped_reason') or '日线没有更新'}")
        elif os.path.exists(path):
            r.update(level="bad", detail=f"exec_{due}.json 读不出来")
        else:
            r.update(level="bad", detail=f"{due} 16:10 的下单任务没有留下执行记录 exec_{due}.json(任务没运行,或在写记录之前出错)")
        return r
    bad, warn = [], []
    if j.get("mode") != "submit":
        bad.append(f"模式是 {j.get('mode')},没有真正下单(AGENT_EXEC 没开?)")
    if j.get("skipped_reason"):
        bad.append(f"没有计划订单:{j['skipped_reason']}")
    orders = j.get("orders") or []
    by_state = {}
    for o in orders:
        s = str(o.get("status") or "").lower()
        key = "rejected" if s.startswith(("error", "rejected")) else s
        by_state.setdefault(key, []).append(str(o.get("ticker")))
    for key, text in ORDER_PROBLEMS:
        if by_state.get(key):
            bad.append(f"{len(by_state[key])} 张订单{text}:{', '.join(by_state[key][:5])}")
    if j.get("not_attempted"):
        bad.append(f"{len(j['not_attempted'])} 张订单没有尝试发送(券商连不上,需要重跑)")
    rec = j.get("reconcile")
    if rec:
        bad.append(f"账本和券商持仓对不上:{('; '.join(map(str, rec)) if isinstance(rec, list) else str(rec))[:80]}")
    cash = None
    cc = j.get("cash_check")
    if isinstance(cc, dict) and "ok" in cc and cc["ok"] is None:
        # agent.execute checks cash only with the broker's cash in hand and no order in flight (ok = None otherwise)
        n = cc.get("orders_in_flight")
        warn.append("现金核对没做:" + (f"有 {n} 张订单在途,成交还没进账本" if n else "拿不到券商的现金数"))
    elif cc is not None:
        cash, note = cash_check_ok(cc)
        if cash is None:
            warn.append(f"现金核对的格式认不出:{note[:60]}")
        elif not cash:
            bad.append(f"现金核对不通过:{note[:80]}")
    started = parse_iso(j.get("started_at"))
    if started is not None:
        late = (started - dt.datetime.combine(due, dt.time(*EXEC_DUE))).total_seconds() / 60
        if late > LATE_MIN:
            warn.append(f"{started:%H:%M} 才开始,比 16:10 晚 {late:.0f} 分钟")
    if bad or warn:
        r.update(level="bad" if bad else "warn", detail=f"{due}:" + ";".join(bad + warn))
    else:
        r["detail"] = (f"{due} 已提交 {len(orders)} 张订单,对账一致" + (",现金核对通过" if cash else "")
                       + (f",{str(j.get('generated_at'))[11:16]} 写入" if j.get("generated_at") else ""))
    return r


def check_fetch_runs(now: dt.datetime, db: str = f"{A}/panel.db") -> list[dict]:
    """The Form 4 fetches logged an ok run on their last due day (panel.fetch_runs, written by the fetcher)."""
    try:
        con = duckdb.connect(db, read_only=True)
        try:
            have = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'fetch_runs'").fetchone()[0]
            rows = con.execute("SELECT source, started_at, status, n_items, n_rows, reason FROM fetch_runs "
                               "WHERE started_at >= ? ORDER BY started_at", [now - dt.timedelta(days=10)]).fetchall() if have else None
        finally:
            con.close()
    except Exception as exc:
        lock = "lock" in str(exc).lower()
        return [{"name": name, "label": f"fetch:{src}", "level": "warn", "detail": "panel.db 正在写入,稍后再查" if lock else str(exc)[:70]}
                for src, name, *_ in FETCH_CHECKS]
    out = []
    for src, name, sched, grace, miss in FETCH_CHECKS:
        r = {"name": name, "label": f"fetch:{src}", "level": "ok", "detail": ""}
        if rows is None:
            r.update(level="warn", detail="还没有抓取记录:fetch_runs 表在新版抓取程序第一次运行时建立")
            out.append(r)
            continue
        mine = [x for x in rows if x[0] == src]
        due = due_day(now, sched, grace, any(x[1].date() == now.date() for x in mine))
        on_due = [x for x in mine if x[1].date() == due]
        if not on_due:
            # the row is written when the run ends (a Monday daily index run can take hours)
            r.update(level=miss, detail=f"{due} 的抓取记录还没有写入(记录在运行结束时写:任务可能还在跑,也可能没运行或中途出错)")
        else:
            _, t, status, n_items, n_rows, reason = on_due[-1]
            when = f"{due} {t:%H:%M}"
            if status == "ok":
                r["detail"] = f"{when} 正常," + (f"{n_items} 份申报,新增 {n_rows} 行" if n_items else "当天没有新申报")
            elif status == "partial":
                r.update(level="warn", detail=f"{when} 有申报没取到(下次运行补抓):{(reason or '')[:80]}")
            else:
                r.update(level=miss, detail=f"{when} 抓取失败,不能当作没有申报:{(reason or '')[:80]}")
        out.append(r)
    return out


def check_jobs(now: dt.datetime) -> list[dict]:
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        listing = ""
    loaded = {}
    for line in listing.splitlines():
        p = line.split("\t")
        if len(p) == 3 and p[2].startswith("com.louis."):
            loaded[p[2]] = (p[0], p[1])
    out, cache = [], {}
    for label, what, log, marker, days, by_hour in JOBS:
        r = {"name": what, "label": label, "level": "ok", "detail": ""}
        if label not in loaded:
            r.update(level="bad", detail="没有加载到 launchd")
            out.append(r)
            continue
        pid, status = loaded[label]
        if status not in ("0", "-") and label != "com.louis.optradar.weekly":
            r.update(level="warn", detail=f"上次退出码 {status}")
        elif status not in ("0", "-"):
            # launchd keeps the exit code until the next scheduled run; a manual rerun that produced the report clears the warning
            sat = now.date() - dt.timedelta(days=(now.weekday() - 5) % 7)
            if os.path.exists(f"{OUT}/weekly/{sat}.html"):
                r.update(detail=f"定时运行失败(退出码 {status}),已手动重跑,{sat} 周报已生成")
            else:
                r.update(level="bad" if now.date() >= sat else "warn", detail=f"上次退出码 {status},{sat} 周报没有生成")
        if log and marker and os.path.exists(log):
            last = None
            with open(log, errors="ignore") as f:
                for line in f:
                    m = re.match(marker, line.strip())
                    if m:
                        last = m.group(1)
            if last:
                try:
                    t = dt.datetime.strptime(re.sub(r" [A-Z]{3} ", " ", last), "%a %b %d %H:%M:%S %Y")
                    age_h = (now - t).total_seconds() / 3600
                    r["last_run"] = t.strftime("%m-%d %H:%M")
                    # expected at least once per scheduled day
                    due = [now.date() - dt.timedelta(days=k) for k in range(0, 8)]
                    due = [d for d in due if d.weekday() in days and (d < now.date() or now.hour >= by_hour)]
                    if due and t.date() < due[0]:
                        r.update(level="bad", detail=f"应在 {due[0]} 运行,最后一次是 {r['last_run']}")
                    elif not r["detail"]:
                        r["detail"] = f"上次 {r['last_run']}({age_h:.0f} 小时前)"
                except ValueError:
                    r["detail"] = r["detail"] or f"上次 {last}"
        for level, text in run_findings(label, log, days, cache):
            if RANK[level] > RANK[r["level"]]:
                r["level"] = level
            r["detail"] = f"{r['detail']};{text}" if r["detail"] else text
        out.append(r)
    return out


def check_data(today: dt.date, now: dt.datetime) -> list[dict]:
    ref = last_session(today, now)
    out = []
    for name, db, sql, warn, bad in DATA:
        r = {"name": name, "level": "ok", "detail": ""}
        try:
            con = duckdb.connect(db, read_only=True)
            v = con.execute(sql).fetchone()[0]
            con.close()
        except Exception as exc:
            msg = str(exc)
            r.update(level="warn" if "lock" in msg.lower() else "bad", detail="正在写入,稍后再查" if "lock" in msg.lower() else msg[:70])
            out.append(r)
            continue
        if v is None:
            r.update(level="warn", detail="没有数据")
        else:
            d = v.date() if isinstance(v, dt.datetime) else v
            if hasattr(d, "to_pydatetime"):
                d = d.to_pydatetime().date()
            age = sessions_back(d, ref)
            r["latest"] = str(d)
            r["detail"] = f"最新 {d}" + (f",落后 {age} 个交易日" if age > 0 else "")
            if age > bad:
                r["level"] = "bad"
            elif age > warn:
                r["level"] = "warn"
        out.append(r)
    return out


def check_system(llm_probe: bool) -> list[dict]:
    out = []
    # power
    try:
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
        ac = "AC Power" in batt
        out.append({"name": "电源", "level": "ok" if ac else "warn", "detail": "已插电" if ac else "用电池:电脑会睡眠,大批量加载暂停,定时任务靠定时唤醒"})
    except Exception:
        pass
    # disk
    free = shutil.disk_usage(os.path.expanduser("~")).free / 1e9
    out.append({"name": "磁盘", "level": "ok" if free > 50 else "warn" if free > 15 else "bad", "detail": f"剩余 {free:,.0f} GB"})
    # OpenD
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 11111))
        out.append({"name": "moomoo OpenD", "level": "ok", "detail": "端口 11111 可连"})
    except Exception:
        out.append({"name": "moomoo OpenD", "level": "bad", "detail": "连不上:早报、实盘快照、IV 存档都会失败"})
    finally:
        s.close()
    # private site
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 8787))
        out.append({"name": "私有站", "level": "ok", "detail": "端口 8787 在服务"})
    except Exception:
        out.append({"name": "私有站", "level": "bad", "detail": "HTTP 服务没在跑"})
    finally:
        s.close()
    # Alpaca paper + the send switch
    try:
        from agent.broker import alpaca as b
        acct = b.from_env().account()
        env = open(os.path.expanduser("~/.hedge-fund/.env")).read()
        on = re.search(r"^AGENT_EXEC=(\S+)", env, re.M)
        out.append({"name": "Alpaca 模拟盘", "level": "ok", "detail": f"账户 ${float(acct['equity']):,.0f};下单开关 {'开' if on and on.group(1) == 'on' else '关(只做试运行)'}"})
    except Exception as exc:
        out.append({"name": "Alpaca 模拟盘", "level": "bad", "detail": str(exc)[:80]})
    # Alpha Vantage quota
    try:
        q = json.load(open(os.path.join(A, "av_quota.json")))
        out.append({"name": "Alpha Vantage 配额", "level": "ok", "detail": f"{q['date']} 已用 {q['used']}/25"})
    except Exception:
        pass
    # Claude login (narrator, translation, weekly report)
    if llm_probe:
        try:
            r = subprocess.run([os.path.expanduser("~/.claude/local/claude"), "-p", "reply with the single word OK", "--model", "haiku"],
                               capture_output=True, text=True, timeout=90)
            txt = (r.stdout + r.stderr).strip()
            good = r.returncode == 0 and "OK" in txt.upper() and "authenticate" not in txt.lower()
            out.append({"name": "Claude 登录", "level": "ok" if good else "bad",
                        "detail": "有效" if good else f"失效:{txt[:70]} → 在终端运行 claude 然后 /login"})
        except Exception as exc:
            out.append({"name": "Claude 登录", "level": "warn", "detail": f"探测失败:{str(exc)[:60]}"})
    # backup
    try:
        b = json.load(open(os.path.join(OUT, "backup_status.json")))
        age = (dt.date.today() - dt.date.fromisoformat(b["date"])).days
        bad = [x["file"] for x in b.get("daily", []) if x["status"] != "ok"]
        wk = b.get("weekly_date")
        skipped = [x["file"] for x in b.get("weekly", []) if x["status"] != "ok"]
        lvl = "bad" if age > 3 or bad else "warn" if age > 1 or skipped or not wk else "ok"
        out.append({"name": "备份(iCloud)", "level": lvl,
                    "detail": f"每日 {b['date']}" + (f",失败 {bad}" if bad else "") + f";每周 {wk or '还没有'}" + (f",跳过 {skipped}" if skipped else "")})
    except Exception:
        out.append({"name": "备份(iCloud)", "level": "bad", "detail": "还没有备份记录"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--no-llm-probe", action="store_true")
    args = ap.parse_args()
    now = dt.datetime.now()
    rep = {"generated_at": now.isoformat(timespec="seconds"), "jobs": check_jobs(now) + [check_exec(now)] + check_fetch_runs(now),
           "data": check_data(now.date(), now),
           "system": check_system(not args.no_llm_probe)}
    try:                                                  # agent.drift: paper book vs its rules and the backtest's range
        rep["drift"] = [{k: c[k] for k in ("name", "level", "detail")} for c in json.load(open(os.path.join(OUT, "agent", "drift.json")))["checks"]]
    except Exception:
        rep["drift"] = []
    allc = rep["jobs"] + rep["data"] + rep["system"] + rep["drift"]
    rep["n_bad"] = sum(1 for c in allc if c["level"] == "bad")
    rep["n_warn"] = sum(1 for c in allc if c["level"] == "warn")
    rep["level"] = "bad" if rep["n_bad"] else "warn" if rep["n_warn"] else "ok"
    try:
        prev = json.load(open(HEALTH))
    except Exception:
        prev = {}
    json.dump(rep, open(HEALTH, "w"), ensure_ascii=False, indent=1)
    for sec in ("jobs", "data", "system", "drift"):
        for c in rep[sec]:
            if c["level"] != "ok":
                # one line each: section_failures skips these by their '[bad ] ' / '[warn] ' prefix
                print(f"[{c['level']:4s}] {c['name']}: " + " ".join(str(c['detail']).split()))
    print(f"health: {rep['level']} ({rep['n_bad']} bad, {rep['n_warn']} warn, {len(allc)} checks)")
    bad_now = sorted(c["name"] for c in allc if c["level"] == "bad")
    bad_prev = sorted(c["name"] for sec in ("jobs", "data", "system", "drift") for c in prev.get(sec, []) if c["level"] == "bad")
    if bad_now and bad_now != bad_prev and not args.no_notify:            # notify on change, not every run
        tn = "/opt/homebrew/bin/terminal-notifier"
        if os.path.exists(tn):
            subprocess.run([tn, "-title", "系统健康:有故障", "-message", ";".join(bad_now)[:180], "-open", "https://optradar.tail5b470b.ts.net/",
                            "-group", "health"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
