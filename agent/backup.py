"""Backups to iCloud Drive: ~/Library/Mobile Documents/com~apple~CloudDocs/OptRadarBackup/.

Code is on GitHub; the data was only on this Mac. Two tiers:

  daily   (after the close, 14 kept)  what cannot be rebuilt: optradar.db (paper ledger, real-account
          history, option ledger), the point-in-time archives (archive.db, estimates.db, retail.db),
          auctions.db, filings.db, the small state files, and out/agent (exec / review / watch JSON)
  weekly  (Sunday, 2 kept)            what can be rebuilt but takes days: panel.db, options.db,
          news.db, short.db, intraday.db

Each DuckDB file is copied while this process holds a read-only connection to it (so no writer can
be mid-transaction), the copy is opened and a table is counted as a restore test, then it is
compressed with zstd. A database that a writer holds is skipped and reported; the next run picks it up.
Secrets (~/.hedge-fund/.env, optradar/.env) are NOT copied to the cloud: keys are re-issued, not restored.

Restore: `zstd -d <file>.zst -o <name>.db` into ~/.hedge-fund/agent/ (or ~/optradar/ for optradar.db).

Usage: python -m agent.backup [--weekly] [--dest DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time

import duckdb

from hedge_fund.paths import AGENT_DIR

DEST = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/OptRadarBackup")
STATUS = "/Users/louis/optradar/out/backup_status.json"
ZSTD = "/opt/homebrew/bin/zstd"
A = AGENT_DIR
DAILY_DBS = ["/Users/louis/optradar/optradar.db", f"{A}/archive.db", f"{A}/estimates.db", f"{A}/retail.db", f"{A}/auctions.db", f"{A}/filings.db"]
WEEKLY_DBS = [f"{A}/panel.db", f"{A}/options.db", f"{A}/news.db", f"{A}/short.db", f"{A}/intraday.db"]
STATE_FILES = [f"{A}/watch_state.json", f"{A}/av_quota.json", f"{A}/balder_seen.json", f"{A}/translate_cache.json", f"{A}/company_sic.csv"]
KEEP = {"daily": 14, "weekly": 2}


def backup_db(path: str, out_dir: str) -> dict:
    name = os.path.basename(path)
    if not os.path.exists(path):
        return {"file": name, "status": "missing"}
    t0 = time.time()
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as exc:
        return {"file": name, "status": "skipped: in use", "detail": str(exc)[:80]}
    tmp = tempfile.mkdtemp(prefix="bk_")
    try:
        try:
            tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
            shutil.copy2(path, os.path.join(tmp, name))
            if os.path.exists(path + ".wal"):
                shutil.copy2(path + ".wal", os.path.join(tmp, name + ".wal"))
        finally:
            con.close()
        # restore test on the copy
        chk = duckdb.connect(os.path.join(tmp, name), read_only=True)
        rows = {t: chk.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables[:40]}
        chk.close()
        dst = os.path.join(out_dir, name + ".zst")
        subprocess.run([ZSTD, "-q", "-T0", "-3", "-f", os.path.join(tmp, name), "-o", dst], check=True)
        return {"file": name, "status": "ok", "bytes": os.path.getsize(path), "compressed": os.path.getsize(dst), "tables": len(tables),
                "rows": int(sum(rows.values())), "seconds": round(time.time() - t0, 1)}
    except Exception as exc:
        return {"file": name, "status": f"failed: {str(exc)[:100]}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prune(kind: str, dest: str) -> list[str]:
    root = os.path.join(dest, kind)
    days = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    gone = days[:-KEEP[kind]] if len(days) > KEEP[kind] else []
    for d in gone:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return gone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--dest", default=DEST)
    args = ap.parse_args()
    today = dt.date.today().isoformat()
    report = {"date": today, "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "dest": args.dest, "daily": [], "weekly": []}
    d_dir = os.path.join(args.dest, "daily", today)
    os.makedirs(d_dir, exist_ok=True)
    for p in DAILY_DBS:
        report["daily"].append(backup_db(p, d_dir))
    st_dir = os.path.join(d_dir, "state")
    os.makedirs(st_dir, exist_ok=True)
    for f in STATE_FILES:
        if os.path.exists(f):
            shutil.copy2(f, st_dir)
    out_agent = "/Users/louis/optradar/out/agent"
    if os.path.isdir(out_agent):
        subprocess.run(["tar", "-czf", os.path.join(d_dir, "out_agent.tgz"), "-C", "/Users/louis/optradar/out", "agent"], check=False)
    report["pruned_daily"] = prune("daily", args.dest)
    if args.weekly:
        w_dir = os.path.join(args.dest, "weekly", today)
        os.makedirs(w_dir, exist_ok=True)
        for p in WEEKLY_DBS:
            report["weekly"].append(backup_db(p, w_dir))
        if any(r["status"] == "ok" for r in report["weekly"]):
            report["pruned_weekly"] = prune("weekly", args.dest)
    # remember the last good weekly across daily runs
    try:
        prev = json.load(open(STATUS))
    except Exception:
        prev = {}
    if not args.weekly:
        report["weekly"] = prev.get("weekly", [])
        report["weekly_date"] = prev.get("weekly_date")
    else:
        report["weekly_date"] = today
    json.dump(report, open(STATUS, "w"), ensure_ascii=False, indent=1)
    json.dump(report, open(os.path.join(args.dest, "last_backup.json"), "w"), ensure_ascii=False, indent=1)
    for kind in ("daily", "weekly"):
        for r in report[kind] if (kind == "daily" or args.weekly) else []:
            size = f"{r.get('bytes', 0) / 1e6:,.0f} MB → {r.get('compressed', 0) / 1e6:,.0f} MB, {r.get('rows', 0):,} rows" if r["status"] == "ok" else ""
            print(f"  {kind:6s} {r['file']:14s} {r['status']:18s} {size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
