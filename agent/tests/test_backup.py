"""agent.backup: the restore test reads the .zst that was written, not the copy made before compression."""
import os
import subprocess
import tempfile

import duckdb
import pytest

from agent import backup

pytestmark = pytest.mark.skipif(not os.path.exists(backup.ZSTD), reason="zstd not installed")


def _make_db(path, wal_rows=0):
    con = duckdb.connect(path)
    con.execute("CREATE TABLE a (x INTEGER)")
    con.execute("INSERT INTO a SELECT * FROM range(100)")
    con.execute("CREATE TABLE b (s VARCHAR)")
    con.execute("INSERT INTO b VALUES ('x'), ('y')")
    con.execute("CREATE VIEW v AS SELECT * FROM a")
    con.execute("CHECKPOINT")
    if wal_rows:                                   # leave rows only in the WAL, as a crashed writer would
        con.execute("PRAGMA disable_checkpoint_on_shutdown")
        con.execute(f"INSERT INTO a SELECT * FROM range({wal_rows})")
    con.close()


def _restore(zst, tmp_path):
    out = str(tmp_path / "restored.db")
    subprocess.run([backup.ZSTD, "-q", "-d", "-f", zst, "-o", out], check=True)
    con = duckdb.connect(out, read_only=True)
    try:
        return con.execute("SELECT count(*) FROM a").fetchone()[0]
    finally:
        con.close()


def test_backup_writes_zst_and_counts_rows_from_it(tmp_path):
    src = str(tmp_path / "x.db")
    _make_db(src)
    out = tmp_path / "out"
    out.mkdir()
    r = backup.backup_db(src, str(out))
    assert r["status"] == "ok" and r["restore"] == "zst"
    assert r["tables"] == 2 and r["rows"] == 102                    # base tables only; the view is not counted
    assert _restore(str(out / "x.db.zst"), tmp_path) == 100


def test_rows_left_in_a_wal_reach_the_compressed_file(tmp_path):
    src = str(tmp_path / "w.db")
    _make_db(src, wal_rows=7)
    assert os.path.exists(src + ".wal")
    out = tmp_path / "out"
    out.mkdir()
    r = backup.backup_db(src, str(out))
    assert r["status"] == "ok" and r["rows"] == 109
    assert _restore(str(out / "w.db.zst"), tmp_path) == 107         # before the fix the .zst held only 100
    assert os.path.exists(src + ".wal")                             # the source itself is untouched


def test_a_bad_zst_fails_the_restore_test(tmp_path, monkeypatch):
    src = str(tmp_path / "x.db")
    _make_db(src)
    other = str(tmp_path / "other.db")
    con = duckdb.connect(other)
    con.execute("CREATE TABLE a (x INTEGER)")
    con.close()
    real = backup._compress
    monkeypatch.setattr(backup, "_compress", lambda s, d: real(other, d))   # the written file is not the copy
    out = tmp_path / "out"
    out.mkdir()
    r = backup.backup_db(src, str(out))
    assert r["status"].startswith("failed: restore mismatch in 2 table(s)")


def test_unreadable_zst_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    src = str(tmp_path / "x.db")
    _make_db(src)
    monkeypatch.setattr(backup, "_compress", lambda s, d: open(d, "wb").write(b"not zstd"))
    out = tmp_path / "out"
    out.mkdir()
    assert backup.backup_db(src, str(out))["status"].startswith("failed:")


def test_missing_locked_and_temp_dirs_cleaned(tmp_path, monkeypatch):
    scratch = tmp_path / "t"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    assert backup.backup_db(str(tmp_path / "nope.db"), str(tmp_path))["status"] == "missing"
    src = str(tmp_path / "x.db")
    _make_db(src)
    held = duckdb.connect(src)                     # same process: DuckDB refuses a second, read-only config
    try:
        assert backup.backup_db(src, str(tmp_path))["status"] == "skipped: in use"
    finally:
        held.close()
    assert backup.backup_db(src, str(tmp_path))["status"] == "ok"
    assert os.listdir(scratch) == []               # the copy and the restore directory are both removed


RUNBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs", "RUNBOOK.md")


def _restore_section():
    text = open(RUNBOOK, encoding="utf-8").read()
    return text[text.index("## 7. "):text.index("## 8. ")]


def test_runbook_restore_parks_the_old_wal_before_every_decompress():
    import re
    sec = _restore_section()
    targets = re.findall(r'zstd -d "[^"]+" -o (\S+)', sec)
    assert len(targets) == 3
    for t in targets:                  # each decompress is guarded by park on the same target
        assert re.search(r"park " + re.escape(t) + r" && zstd -d", sec), t
    assert "mv ~/optradar/optradar.db " not in sec and 'mv ~/.hedge-fund/agent/$f.db "$OLD"' not in sec


def test_runbook_restore_procedure_does_not_replay_the_old_wal(tmp_path):
    # the old database crashed with rows only in its WAL; the backup holds 100 rows in table a
    live = str(tmp_path / "live.db")
    _make_db(live, wal_rows=5)
    assert os.path.exists(live + ".wal")
    clean = str(tmp_path / "clean.db")
    _make_db(clean)
    out = tmp_path / "bk"
    out.mkdir()
    assert backup.backup_db(clean, str(out))["status"] == "ok"
    park = next(l for l in _restore_section().splitlines() if l.startswith("park() {"))
    script = "\n".join([f'OLD="{tmp_path}/old"; mkdir -p "$OLD"', park,
                        f'park "{live}" && {backup.ZSTD} -q -d "{out}/clean.db.zst" -o "{live}"'])
    subprocess.run(["/bin/zsh", "-c", script], check=True, capture_output=True)
    assert not os.path.exists(live + ".wal")
    assert sorted(os.listdir(tmp_path / "old")) == ["live.db", "live.db.wal"]
    con = duckdb.connect(live, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM a").fetchone()[0] == 100      # 105 if the old WAL were replayed
    finally:
        con.close()
