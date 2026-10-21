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

Restore: first move BOTH the old <name>.db and <name>.db.wal out of the way (DuckDB silently replays a WAL left
next to the path onto the restored file), then `zstd -d <file>.zst -o <name>.db` into ~/.hedge-fund/agent/ (or
~/optradar/ for optradar.db). docs/RUNBOOK.md section 7 has the exact commands.

Snapshot (S54, 2026-10-20): the Sunday job (agent/bin/weekly_fundamentals.sh) rewrites panel.db's history (the XBRL
re-fetch, the ADS ratios, the fundamentals rebuild, the listing refresh), and the weekly tier above is taken only after
it, so the 10/05 refresh changed the backtest with no copy of the panel before it (AGENT_PLAN §9 "基数漂移诊断").
`--snapshot` copies panel.db the same way (read-only connection, zstd, restore test) into a local directory BEFORE the
job writes; the job runs none of those steps without a good copy. SNAPSHOT_KEEP kept (1.8 GB uncompressed, about 0.7 GB
compressed each); the oldest is removed only after a good copy. Local, not iCloud: it is there to restate on, not to
survive the Mac.

Usage: python -m agent.backup [--weekly] [--dest DIR]
       python -m agent.backup --snapshot panel [--snapshot-dir DIR]   exit 1 when no good copy was made
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
SNAPSHOTS = f"{A}/snapshots"            # before the Sunday refresh (S54): <dir>/<db name>/<date>/<name>.db.zst
SNAPSHOT_KEEP = 2
SNAPSHOT_LOCK_WAIT_S, SNAPSHOT_LOCK_STEP_S = 1800.0, 10.0   # the Sunday job can wait out a writer (a late or manual run
                                                           # may meet the 16:10 job): one failed open skips a week's refresh
SNAPSHOT_DBS = {"panel": f"{A}/panel.db"}


def _tables(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                      "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]


def _row_counts(con, tables: list[str]) -> dict[str, int]:
    return {t: int(con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]) for t in tables}


def _compress(src: str, dst: str) -> None:
    subprocess.run([ZSTD, "-q", "-T0", "-3", "-f", src, "-o", dst], check=True)


def _decompress(src: str, dst: str) -> None:
    subprocess.run([ZSTD, "-q", "-d", "-f", src, "-o", dst], check=True)


def backup_db(path: str, out_dir: str, lock_wait: float = 0.0) -> dict:
    """Copy under a read-only connection, compress, then restore-test the .zst that was written: decompress it into
    an empty temp directory and require the same tables and row counts the source had while it was held.
    lock_wait: seconds to retry a file a writer holds (panel.connect_with_retry); 0 = skip it at once, as the nightly
    backup does (it comes back tomorrow)."""
    name = os.path.basename(path)
    if not os.path.exists(path):
        return {"file": name, "status": "missing"}
    t0 = time.time()
    try:
        if lock_wait > 0:
            from hedge_fund.features.panel import connect_with_retry
            con = connect_with_retry(path, read_only=True, wait=lock_wait, step=SNAPSHOT_LOCK_STEP_S)
        else:
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
    return prune_dir(os.path.join(dest, kind), KEEP[kind])


def prune_dir(root: str, keep: int) -> list[str]:
    """Remove all but the `keep` newest dated subdirectories of root (names sort by date; '.' ones are work in progress)."""
    days = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith("."))
    gone = days[:-keep] if len(days) > keep else []
    for d in gone:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return gone


def snapshot(path: str, root: str = SNAPSHOTS, keep: int = SNAPSHOT_KEEP, day: str | None = None,
             lock_wait: float | None = None) -> dict:
    """A restore-tested .zst of `path` in root/<name>/<day>/ (see the module doc). It is written into a '.<day>.partial'
    directory and renamed only once it passed, so a failed copy leaves nothing behind, replaces nothing (not even an
    earlier copy of the same day) and prunes nothing: the last good copies stay. A '.partial' left by a run that was
    killed (any day) is removed first, so disk use stays at `keep` copies plus the one being written. A writer holding
    the file is waited for (SNAPSHOT_LOCK_WAIT_S)."""
    name = os.path.splitext(os.path.basename(path))[0]
    day = day or dt.date.today().isoformat()
    base = os.path.join(root, name)
    out, work = os.path.join(base, day), os.path.join(base, f".{day}.partial")
    if os.path.isdir(base):
        for d in os.listdir(base):
            if d.startswith(".") and d.endswith(".partial"):
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
    os.makedirs(work)
    res = backup_db(path, work, SNAPSHOT_LOCK_WAIT_S if lock_wait is None else lock_wait)
    res["dir"] = out
    if res["status"] != "ok":
        shutil.rmtree(work, ignore_errors=True)
        return res
    shutil.rmtree(out, ignore_errors=True)
    os.rename(work, out)
    res["pruned"] = prune_dir(base, keep)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--snapshot", choices=sorted(SNAPSHOT_DBS), default=None, help="local copy before the Sunday refresh (S54)")
    ap.add_argument("--snapshot-dir", default=SNAPSHOTS)
    args = ap.parse_args()
    if args.snapshot:                                   # nothing else: the weekly job calls this first, then refreshes
        r = snapshot(SNAPSHOT_DBS[args.snapshot], args.snapshot_dir)
        size = f"{r.get('bytes', 0) / 1e6:,.0f} MB → {r.get('compressed', 0) / 1e6:,.0f} MB, {r.get('rows', 0):,} rows" if r["status"] == "ok" else ""
        print(f"  snapshot {r['file']:14s} {r['status']:18s} {size} {r['dir']}" + (f" (removed {r['pruned']})" if r.get("pruned") else ""))
        return 0 if r["status"] == "ok" else 1
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
