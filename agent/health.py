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

2026-10-19: the long book's recorded lists of the last two weeks must come from the 16:10 job (check_list_writers):
10-16's were written by the next brief on a panel with a tenth of the day's bars, and no check looked at who wrote them.
2026-10-20: the broker's DIV and FEE activities must reach the books (check_cash_events, agent/cash_events.py): one that
could not be given to a book is bad; an unknown activity type, a booked one the broker canceled, a dividend unpaid 5 days
past its pay date and a failed corporate-actions fetch warn.

2026-10-20 (S54): the insider all-event shadow (agent/insider_shadow.py, 13:25 job) must have written today's report
without an error (check_insider_shadow: warn, never bad: no book reads it), and the factor ETFs the long alarm needs
must be fresh (DATA); the long alarm itself is a drift check (agent/drift.py).

2026-10-21 (S54 investigation): a failed dividends update is named as what auction_basis used instead, and a newest
auction row without its with-dividends NAV is bad (check_cash_events): unknown dividends are never $0 any more.

2026-10-27: Balder's private archive and its scoring (check_balder: warn, never bad: no book, signal or alert reads
any of it): the X feed saved within 2 sessions, at most 40 posts waiting for a full text or image, each tab's
read_through within 7 days, the three summaries regenerated within 2 sessions; the record page's prices outside our
bars and the scored returns beyond ±50% are counted, and warn only while one is dated within the last 7 days.

Usage: python -m agent.health [--no-notify] [--no-llm-probe]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
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
ZERO_FAIL_RE = re.compile(r"\bfailed['\"]?:?\s*0\b|\b0 failed\b")   # progress counters ("... failed 0", "'failed': 0")
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
LIST_SIGNALS = ("composite_long", "composite_long_v2", "largecap_qlv")   # agent/books/live.py; agent.execute long_targets writes them
LIST_WINDOW = 10            # sessions: a list from elsewhere shows for two weeks, long enough to look at and repair
REPAIR_PREFIX = "repair-"   # run_id of a list rewritten after the fact on purpose
LIST_RUNS_CHECKED = {       # run_id of a list from elsewhere, compared with a recompute: a warning, not bad
    "2026-10-08-75f33e": "10-19 核对过,和重算一致",
}
UNPAID_GRACE_DAYS = 5       # an entitled dividend this many days past its pay date with no broker DIV is named (a note only)
SHADOW = os.path.join(OUT, "agent", "insider_shadow.json")
SHADOW_DUE, SHADOW_GRACE_MIN = (13, 25), 90          # postclose.sh runs it after agent.evaluate
SPRT_ZH = {"none": "未越线", "H1": '判"有"', "H0": '判"无"'}
BALDER_FEED = os.path.join(OUT, "agent", "balder_x_feed.jsonl")      # agent/balder_x_save: one line per post saved
BALDER_STATE = os.path.join(A, "balder_x_state.json")                 # read_through per tab, todo
BALDER_OUTPUTS = {          # name, summary json: balder_log / balder_views at 13:25, balder_record at 16:10
    "交易记分": os.path.join(OUT, "agent", "balder_latest.json"),
    "观点记分": os.path.join(OUT, "agent", "balder_views.json"),
    "公开战绩": os.path.join(OUT, "agent", "balder_record.json"),
}
BALDER_STALE_SESSIONS, BALDER_TODO_MAX, BALDER_READ_DAYS = 2, 40, 7
BALDER_RECENT_DAYS = 7      # a suspect return or a price outside our bars warns while it is this many days old or newer

DATA = [  # name, db, sql for the latest date, max age in sessions before warn / bad
    ("价格日线 bars", f"{A}/panel.db", "SELECT max(trade_date) FROM bars", 1, 3),
    # the oldest of SPY / IWM / QQQ's last days (2026-10-21 review: only SPY was read, so a QQQ that stopped updating, the
    # pages' same-period benchmark, or an IWM the long alarm needs, showed nowhere; ^VIX is not read, as before)
    ("指数 SPY/IWM/QQQ", f"{A}/panel.db", "SELECT min(d) FROM (SELECT max(trade_date) AS d FROM index_daily "
     "WHERE symbol IN ('SPY', 'IWM', 'QQQ') GROUP BY symbol)", 1, 3),
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
    # S54: the long book's frozen ETF benchmark (agent/drift.py) needs all four; the oldest of their last days counts
    ("因子 ETF(MTUM/VLUE/QUAL/USMV)", f"{A}/panel.db", "SELECT min(d) FROM (SELECT max(trade_date) AS d FROM index_daily "
     "WHERE symbol IN ('MTUM', 'VLUE', 'QUAL', 'USMV') GROUP BY symbol)", 2, 5),
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


def check_list_writers(now: dt.datetime, db: str = "/Users/louis/optradar/optradar.db") -> dict:
    """The long book's lists of the last LIST_WINDOW sessions in agent_picks were written by the 16:10 job (run_id 'exec').

    The drift replay, the evaluation and the attribution replay these lists, and only the 16:10 job checks the day's bars
    before it scores (agent/execute.py bars_coverage). 10-16's lists came from the 10-19 brief instead, scored on the bars
    of 502 names, and nothing showed it. Any other writer is bad; a run_id starting 'repair-' is a list rewritten after
    the fact on purpose: a warning that names it, since it is not what the system saw that day. A writer in
    LIST_RUNS_CHECKED (its list matched a recompute) and a session with no list warn. The v2 cluster lists
    (run_id 'shadow_v2', agent.shadow_v2 at 13:25 or the 16:10 job) are not read."""
    r = {"name": "长线名单来源", "label": "list_writers", "level": "ok", "detail": ""}
    start = last_session(now.date(), now)
    for _ in range(LIST_WINDOW - 1):
        start = prev_weekday(start)
    try:
        con = duckdb.connect(db, read_only=True)
        try:
            rows = con.execute(f"""SELECT DISTINCT as_of, run_id, signal_name FROM agent_picks
                                   WHERE signal_name IN ({', '.join('?' * len(LIST_SIGNALS))}) AND as_of >= ?
                                   ORDER BY as_of DESC, signal_name""", [*LIST_SIGNALS, start]).fetchall()
        finally:
            con.close()
    except Exception as exc:
        lock = "lock" in str(exc).lower()
        r.update(level="warn", detail="optradar.db 正在写入,稍后再查" if lock else str(exc)[:70])
        return r
    if not rows:
        r.update(level="warn", detail=f"最近 {LIST_WINDOW} 个交易日没有长线名单")
        return r
    by_run: dict[tuple, list[str]] = {}
    for as_of, run_id, sig in rows:
        if run_id != "exec":
            by_run.setdefault((as_of, run_id), []).append(sig)
    bad, notes = [], []
    for (d, rid), s in by_run.items():
        rid = str(rid or "")
        if rid.startswith(REPAIR_PREFIX):
            notes.append(f"{d} {'、'.join(s)} 是事后修复({rid}),不是当天的数据")
        elif rid in LIST_RUNS_CHECKED:
            notes.append(f"{d} {'、'.join(s)} 由 {rid} 写入,{LIST_RUNS_CHECKED[rid]}")
        else:
            bad.append(f"{d} {'、'.join(s)} 由 {rid or '未知任务'} 写入")
    # a day the brief's bars guard skipped (agent.books.live.thin_bar_day) or the 16:10 job missed has no list at all;
    # the latest session is check_exec's. Holidays count as weekdays: a warning on one is harmless.
    have = {x[0] for x in rows if x[2] == LIST_SIGNALS[0]}
    missing, d = [], prev_weekday(last_session(now.date(), now))
    while d >= start:
        if d not in have:
            missing.append(str(d))
        d = prev_weekday(d)
    if missing:
        notes.append("没有长线名单:" + "、".join(sorted(missing)) + "(16:10 任务没写;早报遇到日线不全也不写)")
    if bad:
        r.update(level="bad", detail="不是 16:10 下单任务写的名单:" + ";".join(bad) + "。偏离检查、评估和归因都按名单重放,先核对当天日线"
                                     + (";" + ";".join(notes) if notes else ""))
    elif notes:
        r.update(level="warn", detail=";".join(notes))
    else:
        r["detail"] = f"最近 {LIST_WINDOW} 个交易日的 {len({x[0] for x in rows})} 天名单都由 16:10 下单任务写入"
    return r


def _latest_run_json(out_dir: str) -> tuple[str | None, dict | None]:
    """(file name, content) of the newest exec_<day>.json or sync_<day>.json by generated_at (the 16:10 run or the
    13:25 sync, whichever wrote last; both run agent/cash_events.py)."""
    names = sorted(glob.glob(os.path.join(out_dir, "exec_*.json")))[-5:] + sorted(glob.glob(os.path.join(out_dir, "sync_*.json")))[-5:]
    runs = []
    for n in names:
        j = _load_json(n)
        if isinstance(j, dict):
            runs.append((str(j.get("generated_at") or ""), os.path.basename(n), j))
    if not runs:
        return None, None
    _, name, j = max(runs, key=lambda r: (r[0], r[1]))
    return name, j


def _usd(v) -> str:
    return f"${float(v or 0):,.2f}"


def check_cash_events(now: dt.datetime, out_dir: str = f"{OUT}/agent", db: str = "/Users/louis/optradar/optradar.db",
                      auction: str | None = None) -> dict:
    """The broker's DIV and FEE activities reached the books (agent/cash_events.py, run by every 16:10 / 13:25 run).

    From the newest run record: an activity that could not be given to a book (no holder lot, no fill that day, a status
    other than executed / canceled) is bad, since the books' cash then differs from the broker's; an activity type
    nothing handles, a booked activity the broker has since canceled, or a sync that failed, warns. From the ledger (read-only): an entitled dividend more than UNPAID_GRACE_DAYS past its pay date with no
    broker DIV of that ticker warns, with names and amounts (a note: the paper broker does not pay every dividend).
    From auction_basis.json: a corporate-actions fetch that failed warns (the dividends come from the cache). Since
    2026-10-21 (dividends.status): a dividends update that failed warns and names what was used instead (the rows stored
    at <time>, or none: before, it read "from the cache" while every div_cum had been written as 0), with the days whose
    with-dividends NAV was left unknown; a book whose newest auction row has no equity_auction_tr is bad, since the S54
    long alarm and the evaluation cannot read that day (the next run whose update succeeds fills it in, so nothing is
    lost, but the registered alarm skipped a day and the notification should say so)."""
    r = {"name": "券商分红和费用入账", "label": "cash_events", "level": "ok", "detail": ""}
    bad, warn, ok = [], [], []
    name, j = _latest_run_json(out_dir)
    ce = (j or {}).get("cash_events")
    if j is None:
        warn.append("没有找到执行记录(exec/sync json)")
    elif not isinstance(ce, dict):
        warn.append(f"{name} 里没有现金事件记录(写它的程序还没有 cash_events)")
    elif ce.get("error"):
        warn.append(f"{name} 的现金事件没有同步:{str(ce['error'])[:80]}(现金核对会显示没入账的部分)")
    else:
        un = ce.get("unallocated") or []
        if un:
            bad.append(f"{len(un)} 笔券商流水分不到书里,现金核对会差这么多:" + "、".join(
                f"{u.get('ticker') or u.get('type')} {u.get('type')} {u.get('date')} {_usd(u.get('net_usd'))}({u.get('why')})" for u in un[:3])
                + (" 等" if len(un) > 3 else ""))
        uh = ce.get("unhandled") or []
        if uh:
            warn.append(f"{len(uh)} 笔没有处理的流水类型:" + "、".join(f"{u.get('type')} {u.get('date')} {_usd(u.get('net_usd'))}"
                                                            for u in uh[:3]) + (" 等" if len(uh) > 3 else ""))
        if ce.get("unhandled_error"):
            warn.append(f"未处理流水的检查没做成:{str(ce['unhandled_error'])[:60]}")
        back = [c for c in ce.get("canceled") or [] if c.get("booked")]
        if back:
            warn.append(f"{len(back)} 笔已入账的流水被券商撤销(账本没改,现金核对会显示是否差钱):" + "、".join(
                f"{c.get('ticker') or c.get('type')} {c.get('type')} {c.get('date')} {_usd(c.get('net_usd'))}" for c in back[:3])
                + (" 等" if len(back) > 3 else ""))
        booked = ce.get("booked") or []
        ok.append(f"{name} 新入账 {len(booked)} 笔" + (f"({_usd(sum(sum((b.get('books') or {}).values()) for b in booked))})" if booked else ""))
    try:
        con = duckdb.connect(db, read_only=True)
        try:
            have = {x[0] for x in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            dcols = {x[0] for x in con.execute("DESCRIBE agent_dividends").fetchall()} if "agent_dividends" in have else set()
            ent = con.execute("""SELECT ticker, book, ex_date, record_date, pay_date, usd FROM agent_dividends
                                 WHERE kind = 'dividend' AND pay_date IS NOT NULL""").fetchall() if {"pay_date", "record_date"} <= dcols else []
            paid = con.execute("""SELECT ticker, record_date, activity_date FROM agent_cash_events WHERE kind = 'DIV'"""
                               ).fetchall() if "agent_cash_events" in have else []
            n_ev = con.execute("SELECT count(DISTINCT activity_id) FROM agent_cash_events").fetchone()[0] if "agent_cash_events" in have else 0
        finally:
            con.close()
        from agent.cash_events import unpaid
        late = unpaid([{"ticker": t, "book": b, "ex_date": x, "record_date": rd, "pay_date": pay, "usd": u} for t, b, x, rd, pay, u in ent],
                      [{"ticker": t, "record_date": rd, "activity_date": ad} for t, rd, ad in paid], now.date(), UNPAID_GRACE_DAYS)
        if late:
            warn.append(f"过了支付日 {UNPAID_GRACE_DAYS} 天以上券商还没付的分红(只是提示,模拟盘不一定付):" + "、".join(
                f"{u['ticker']} {_usd(u['usd'])}(支付日 {u['pay_date']:%m-%d})" for u in late[:6]) + (" 等" if len(late) > 6 else ""))
        ok.append(f"账本里共 {n_ev} 笔券商分红和费用流水")
    except Exception as exc:
        lock = "lock" in str(exc).lower()
        warn.append("optradar.db 正在写入,稍后再查" if lock else f"读不出分红和流水:{str(exc)[:60]}")
    a = _load_json(auction or os.path.join(OUT, "agent", "auction_basis.json"))
    dv = (a or {}).get("dividends") or {}
    err, status = dv.get("error"), dv.get("status")
    if status == "fallback" and dv.get("computed_at"):
        warn.append(f"分红更新失败,用的是 {str(dv['computed_at'])[:16].replace('T', ' ')} 存下的结果(之后的除息日要等下次更新成功):"
                    f"{str(err)[:80]}")
    elif status == "fallback":
        warn.append(f"分红更新失败,但模拟盘没有持仓,不欠分红:{str(err)[:80]}")
    elif status == "unknown":
        warn.append(f"分红更新失败,存下的结果也用不了:已有的日子保留上次的含分红净值,其余的日子不算(不按 0 计):{str(err)[:80]}")
    elif err:                                             # status "cache", or a json from before 2026-10-21
        warn.append(f"分红数据有问题(按缓存的公司行动计算):{str(err)[:80]}")
    gaps = dv.get("tr_unknown_days") or {}
    if gaps:
        warn.append("这些天的含分红竞价净值没算(分红不知道):" + ";".join(
            f"{b} {', '.join(d[-3:])}" + (f" 等 {len(d)} 天" if len(d) > 3 else "") for b, d in sorted(gaps.items())))
    newest = [(b, v.get("as_of")) for b, v in sorted(((a or {}).get("books") or {}).items())
              if isinstance(v, dict) and "equity_auction_tr" in v and v["equity_auction_tr"] is None]
    if newest:
        bad.append("最新一天的含分红竞价净值没算,长线冻结基准报警和评估这一天都不算:" + "、".join(f"{b} {d}" for b, d in newest))
    if bad or warn:
        r.update(level="bad" if bad else "warn", detail=";".join(bad + warn))
    else:
        r["detail"] = ",".join(ok) + ",没有分不下去的流水"
    return r


def shadow_first_run(config: str = os.path.join(os.path.dirname(__file__), "config.yaml")) -> dt.datetime:
    """The first 13:25 run with an evening to read: the weekday after config s54's first event day."""
    import yaml
    d = dt.date.fromisoformat(str(yaml.safe_load(open(config))["s54"]["insider_shadow"]["first_event_day"])) + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(*SHADOW_DUE))


def check_insider_shadow(now: dt.datetime, path: str = SHADOW) -> dict:
    """The S54 shadow's report: warn when it is missing, older than the last 13:25 run, or says the run failed; else one
    line with the events counted and the SPRT's state. The exit-curve readings stay out until the report shows them."""
    r = {"name": "内部人全事件影子账(S54)", "label": "insider_shadow", "level": "ok", "detail": ""}
    j = _load_json(path)
    if not isinstance(j, dict):
        first = shadow_first_run()
        if now < first + dt.timedelta(minutes=SHADOW_GRACE_MIN):
            r["detail"] = f"第一次运行在 {first:%m-%d %H:%M}(13:25 收盘后任务)"
        else:
            r.update(level="warn", detail="还没有 insider_shadow.json(13:25 收盘后任务写)")
        return r
    gen = parse_iso(j.get("generated_at"))
    due = due_day(now, SHADOW_DUE, SHADOW_GRACE_MIN, bool(gen and gen.date() == now.date()))
    warn = []
    if j.get("error"):
        warn.append(f"上次运行失败({j.get('error_at') or '?'}):{str(j['error'])[:80]};下面是上一次成功的结果")
    if gen is None or gen.date() < due:
        warn.append(f"报告是 {gen:%m-%d %H:%M} 的,{due} 的没有写" if gen else "报告没有生成时间")
    s = j.get("sprt") or {}
    dec = s.get("decided") or {}
    state = SPRT_ZH.get(s.get("decision"), str(s.get("decision")))
    if dec:
        state += f"(第 {dec.get('n')} 个事件,{dec.get('entry_day')}{',截断' if dec.get('truncated') else ''})"
    cur = (f"均值 {s['mean_pct']:+.3f}%(n {s['n']}),边界 {s['lower_pct']:+.2f}% / {s['upper_pct']:+.2f}%"
           if s.get("n") else f"自 {j.get('first_event_day')} 起还没有 E1")
    rr = j.get("exit_readings") or {}
    info = (f"{j.get('n_events', 0)} 个事件(有 E1 的 {j.get('n_with_E1', 0)});SPRT {state}:{cur};"
            f"持有期读法 {'已读' if rr.get('status') == 'read' else str(rr.get('due')) + ' 之前不看'}")
    r.update(level="warn" if warn else "ok", detail=";".join(warn + [info]))
    return r


def last_saved(feed: str) -> dt.datetime | None:
    """saved_at of the feed's last line that has one."""
    try:
        with open(feed, errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            t = parse_iso(json.loads(line).get("saved_at")) if line.strip() else None
        except (ValueError, AttributeError):
            t = None
        if t:
            return t
    return None


def check_balder(now: dt.datetime, feed: str | None = None, state: str | None = None, outputs: dict | None = None) -> dict:
    """Balder's archive and scoring: the feed's last save, the reader's todo and read_through, the three summaries'
    age, the record page's price audit and the suspect returns (warn on a recent one only; the totals are always in the
    detail). Warn, never bad: display and audit only."""
    r = {"name": "Balder 存档与记分(只看不交易)", "label": "balder", "level": "ok", "detail": ""}
    try:
        warn, info = balder_findings(now, feed or BALDER_FEED, state or BALDER_STATE, outputs or BALDER_OUTPUTS)
    except Exception as exc:                              # a hand-edited state file must not stop the health report
        warn, info = [f"检查出错:{type(exc).__name__}: {str(exc)[:60]}"], []
    r.update(level="warn" if warn else "ok", detail=";".join(warn + info))
    return r


def n_recent(days: list, today: dt.date) -> int:
    """How many of these ISO dates are at most BALDER_RECENT_DAYS old; an unreadable date counts (it is looked at)."""
    n = 0
    for d in days:
        try:
            n += (today - dt.date.fromisoformat(str(d)[:10])).days <= BALDER_RECENT_DAYS
        except ValueError:
            n += 1
    return n


def balder_findings(now: dt.datetime, feed: str, state: str, outputs: dict) -> tuple[list[str], list[str]]:
    """(warnings, notes) for check_balder."""
    ref = last_session(now.date(), now)
    warn, info = [], []
    t = last_saved(feed)
    if t is None:
        warn.append("X 存档没有记录(balder_x_feed.jsonl)")
    elif sessions_back(t.date(), ref) > BALDER_STALE_SESSIONS:
        warn.append(f"X 存档最后一次是 {t:%m-%d %H:%M},落后 {sessions_back(t.date(), ref)} 个交易日(读帖循环停了?)")
    else:
        info.append(f"X 存档最后 {t:%m-%d %H:%M}")
    st = _load_json(state)
    if not isinstance(st, dict):
        warn.append("没有读帖状态(balder_x_state.json)")
    else:
        todo = len(st.get("todo") or [])
        if todo > BALDER_TODO_MAX:
            warn.append(f"待补全文/图 {todo} 条(超过 {BALDER_TODO_MAX})")
        else:
            info.append(f"待补全文/图 {todo} 条")
        for tab, v in sorted((st.get("read_through") or {}).items()):
            rt = parse_iso(v)                             # UTC, compared with local time: a few hours do not matter here
            if rt is None:
                warn.append(f"{tab} 的 read_through 读不出:{str(v)[:30]}")
            elif (now - rt).days > BALDER_READ_DAYS:
                warn.append(f"{tab} 只读到 {rt:%m-%d}({(now - rt).days} 天前)")
            else:
                info.append(f"{tab} 读到 {rt:%m-%d}")
    for name, path in outputs.items():
        j = _load_json(path)
        if not isinstance(j, dict):
            warn.append(f"{name}没有 {os.path.basename(path)}")
            continue
        gen, base = parse_iso(j.get("generated_at")), os.path.basename(path)
        if gen is None:
            warn.append(f"{name} {base} 没有生成时间")
        elif sessions_back(gen.date(), ref) > BALDER_STALE_SESSIONS:
            warn.append(f"{name} {base} 是 {gen:%m-%d %H:%M} 的,落后 {sessions_back(gen.date(), ref)} 个交易日")
        sus = j.get("suspect") or []
        if sus:
            m = n_recent([x.get("posted") for x in sus], now.date())
            (warn if m else info).append(f"{name}收益绝对值超过 50%:历史 {len(sus)} 条,近 {BALDER_RECENT_DAYS} 天 {m} 条"
                                         "(先核对日线,分数没改)")
        pc = j.get("price_check")
        if isinstance(pc, dict) and pc.get("error"):
            warn.append(f"{name}价格核对失败:{str(pc['error'])[:80]}")
        elif isinstance(pc, dict):
            if pc.get("n_outside"):                     # `outside` lists the newest ones, so a recent one is in it
                m = n_recent([x.get("day") for x in pc.get("outside") or []], now.date())
                (warn if m else info).append(f"{name}价格核对:历史 {pc['n_outside']} 条超出日线范围,近 {BALDER_RECENT_DAYS} 天 "
                                             f"{m} 条(共核对 {pc.get('n_checked', 0)} 个)")
            else:
                info.append(f"{name}价格核对 {pc.get('n_checked', 0)} 个都在日线范围里")
            if pc.get("no_bars"):
                info.append(f"{len(pc['no_bars'])} 个代码我们没有日线")
    return warn, info


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
    rep = {"generated_at": now.isoformat(timespec="seconds"),
           "jobs": check_jobs(now) + [check_exec(now)] + check_fetch_runs(now) + [check_list_writers(now), check_cash_events(now),
                                                                                  check_insider_shadow(now), check_balder(now)],
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
