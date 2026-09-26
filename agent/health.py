"""System health: every scheduled job, every dataset's freshness, logins, power, disk, backup.

Why: in the week of 2026-09-21 four failures were found only by accident — a job that was never
loaded, an expired Claude login (the weekly report failed silently), the Mac sleeping through a
load, an exhausted API quota. This runs at the end of every scheduled job, writes
out/health.json, is rendered on the private front page, and notifies when something is red.

Levels: ok / warn (degraded, the system still trades and reports) / bad (something the books or
the record depend on is broken). Read-only everywhere; the Claude login probe is one tiny call.

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
    ("com.louis.optradar", "08:41 早报", f"{OUT}/cron.log", r"^=== (.+) done ===", range(0, 5), 9),
    ("com.louis.agent.postclose", "13:25 收盘后(复盘、快照、备份)", f"{OUT}/agent/execute.log", r"^=== postclose (.+) ===", range(0, 5), 14),
    ("com.louis.agent.execute", "16:10 下单", f"{OUT}/agent/execute.log", r"^=== execute (.+?) AGENT_EXEC", range(0, 5), 17),
    ("com.louis.agent.form4", "06:00 Form 4", None, None, range(0, 5), 7),
    ("com.louis.agent.news", "13:15 新闻情绪", None, None, range(0, 5), 14),
    ("com.louis.optradar.weekly", "周六 09:30 周报", None, None, [5], 10),
    ("com.louis.agent.fundamentals", "周日 03:00 基本面 + 审计 + 周备份", f"{OUT}/agent/collect.log", r"^=== weekly fundamentals (.+) ===", [6], 4),
    ("com.louis.optradar.private", "私有站 HTTP 服务", None, None, None, None),
    ("com.louis.power", "电源策略", None, None, None, None),
]
DATA = [  # name, db, sql for the latest date, max age in sessions before warn / bad
    ("价格日线 bars", f"{A}/panel.db", "SELECT max(trade_date) FROM bars", 1, 3),
    ("指数 SPY/VIX", f"{A}/panel.db", "SELECT max(trade_date) FROM index_daily WHERE symbol = 'SPY'", 1, 3),
    ("内部人 Form 4", f"{A}/panel.db", "SELECT max(filing_date) FROM insider_tx", 2, 4),
    ("基本面 XBRL", f"{A}/panel.db", "SELECT max(filed) FROM fundamentals_pit", 8, 15),
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
    out = []
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
            r.update(level="warn", detail=f"上次退出码 {status}(周报失败会这样)")
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
    rep = {"generated_at": now.isoformat(timespec="seconds"), "jobs": check_jobs(now), "data": check_data(now.date(), now),
           "system": check_system(not args.no_llm_probe)}
    allc = rep["jobs"] + rep["data"] + rep["system"]
    rep["n_bad"] = sum(1 for c in allc if c["level"] == "bad")
    rep["n_warn"] = sum(1 for c in allc if c["level"] == "warn")
    rep["level"] = "bad" if rep["n_bad"] else "warn" if rep["n_warn"] else "ok"
    try:
        prev = json.load(open(HEALTH))
    except Exception:
        prev = {}
    json.dump(rep, open(HEALTH, "w"), ensure_ascii=False, indent=1)
    for sec in ("jobs", "data", "system"):
        for c in rep[sec]:
            if c["level"] != "ok":
                print(f"[{c['level']:4s}] {c['name']}: {c['detail']}")
    print(f"health: {rep['level']} ({rep['n_bad']} bad, {rep['n_warn']} warn, {len(allc)} checks)")
    bad_now = sorted(c["name"] for c in allc if c["level"] == "bad")
    bad_prev = sorted(c["name"] for sec in ("jobs", "data", "system") for c in prev.get(sec, []) if c["level"] == "bad")
    if bad_now and bad_now != bad_prev and not args.no_notify:            # notify on change, not every run
        tn = "/opt/homebrew/bin/terminal-notifier"
        if os.path.exists(tn):
            subprocess.run([tn, "-title", "系统健康:有故障", "-message", ";".join(bad_now)[:180], "-open", "https://optradar.tail5b470b.ts.net/",
                            "-group", "health"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
