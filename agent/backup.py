"""Backups to iCloud Drive: ~/Library/Mobile Documents/com~apple~CloudDocs/OptRadarBackup/.

Code is on GitHub; the data was only on this Mac. Two tiers:

  daily   (after the close, 14 kept)  what cannot be rebuilt: optradar.db (paper ledger, real-account
          history, option ledger), the point-in-time archives (archive.db, estimates.db, retail.db),
          auctions.db, filings.db, the small state files, and out/agent (exec / review / watch JSON)
  weekly  (Sunday, 2 kept)            what can be rebuilt but takes days: panel.db, options.db,
          news.db, short.db, intraday.db

Each DuckDB file is copied while this process holds a read-only connection to it (so no writer can
be mid-transaction; a leftover WAL is folded into the copy), compressed with zstd, and then the .zst that
was written is decompressed into an empty temp directory and must show the same tables and row counts the
source had: the restore test covers the file that is actually kept. A database that a writer holds is
skipped and reported; the next run picks it up.
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


def _tables(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                      "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]


def _row_counts(con, tables: list[str]) -> dict[str, int]:
    return {t: int(con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]) for t in tables}


def _compress(src: str, dst: str) -> None:
    subprocess.run([ZSTD, "-q", "-T0", "-3", "-f", src, "-o", dst], check=True)


def _decompress(src: str, dst: str) -> None:
    subprocess.run([ZSTD, "-q", "-d", "-f", src, "-o", dst], check=True)


def backup_db(path: str, out_dir: str) -> dict:
    """Copy under a read-only connection, compress, then restore-test the .zst that was written: decompress it into
    an empty temp directory and require the same tables and row counts the source had while it was held."""
    name = os.path.basename(path)
    if not os.path.exists(path):
        return {"file": name, "status": "missing"}
    t0 = time.time()
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as exc:
        return {"file": name, "status": "skipped: in use", "detail": str(exc)[:80]}
    tmp = tempfile.mkdtemp(prefix="bk_")
    rdir = tempfile.mkdtemp(prefix="bk_restore_")
    try:
        try:
            tables = _tables(con)
            expected = _row_counts(con, tables)                 # no writer can hold the file while we do
            shutil.copy2(path, os.path.join(tmp, name))
            has_wal = os.path.exists(path + ".wal")
            if has_wal:
                shutil.copy2(path + ".wal", os.path.join(tmp, name + ".wal"))
        finally:
            con.close()
        if has_wal:                                             # fold the WAL into the copy: only the .db is compressed
            w = duckdb.connect(os.path.join(tmp, name))
            w.execute("CHECKPOINT")
            w.close()
        dst = os.path.join(out_dir, name + ".zst")
        _compress(os.path.join(tmp, name), dst)
        shutil.rmtree(tmp, ignore_errors=True)                  # free the disk before the restore copy
        # restore test on what was written, not on the pre-compression copy
        restored = os.path.join(rdir, name)
        _decompress(dst, restored)
        chk = duckdb.connect(restored, read_only=True)
        try:
            got_tables = _tables(chk)
            got = _row_counts(chk, got_tables)
        finally:
            chk.close()
        bad = sorted(t for t in set(tables) | set(got_tables) if expected.get(t) != got.get(t))
        res = {"file": name, "bytes": os.path.getsize(path), "compressed": os.path.getsize(dst), "tables": len(got_tables),
               "rows": int(sum(got.values())), "restore": "zst", "seconds": round(time.time() - t0, 1)}
        if bad:
            res["status"] = f"failed: restore mismatch in {len(bad)} table(s): {', '.join(bad[:5])}"
        else:
            res["status"] = "ok"
        return res
    except Exception as exc:
        return {"file": name, "status": f"failed: {str(exc)[:100]}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(rdir, ignore_errors=True)


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
