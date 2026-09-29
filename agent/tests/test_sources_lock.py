"""PanelStore waits for another process's lock on panel.db instead of failing the step. Temp DuckDB only."""
import subprocess
import sys

import duckdb
import pytest

from hedge_fund.features import panel
from hedge_fund.features.panel import PanelStore


def test_lock_conflict_is_retried_and_each_wait_logged(tmp_path, monkeypatch, capsys):
    db = tmp_path / "p.db"
    PanelStore(db).close()
    real, calls, slept = duckdb.connect, [], []

    def flaky(path, read_only=False):
        calls.append(read_only)
        if len(calls) < 3:
            raise duckdb.IOException('Could not set lock on file "p.db": Conflicting lock is held in python (PID 1)')
        return real(path, read_only=read_only)

    monkeypatch.setattr(panel.duckdb, "connect", flaky)
    monkeypatch.setattr(panel.time, "sleep", slept.append)
    with PanelStore(db, read_only=True) as s:
        assert s.con.execute("SELECT count(*) FROM bars").fetchone()[0] == 0
    assert calls == [True, True, True] and slept == [10.0, 10.0]
    err = capsys.readouterr().err
    assert err.count("is locked by another process; waiting 10 s") == 2
    assert "failed" not in err.lower()          # the health check reads 'failed' lines as step failures


def test_lock_held_past_the_wait_raises_and_other_errors_are_not_retried(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(panel.time, "sleep", slept.append)

    def locked(path, read_only=False):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(panel.duckdb, "connect", locked)
    with pytest.raises(duckdb.IOException):
        PanelStore(tmp_path / "p.db", lock_wait=300, lock_step=10)
    assert len(slept) == 30                      # 5 minutes in 10-second steps, then the error

    slept.clear()

    def broken(path, read_only=False):
        raise duckdb.IOException("Cannot open file: permission denied")

    monkeypatch.setattr(panel.duckdb, "connect", broken)
    with pytest.raises(duckdb.IOException):
        PanelStore(tmp_path / "p.db")
    assert slept == []


def test_waits_for_a_real_writer_in_another_process(tmp_path):
    db = tmp_path / "p.db"
    PanelStore(db).close()
    holder = subprocess.Popen([sys.executable, "-c", (
        "import duckdb, sys, time\n"
        f"con = duckdb.connect({str(db)!r})\n"
        "print('ready', flush=True)\n"
        "time.sleep(1.5)\n"
        "con.close()\n")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(duckdb.IOException):
            PanelStore(db, read_only=True, lock_wait=0)        # no wait: the old behaviour
        with PanelStore(db, read_only=True, lock_wait=20, lock_step=0.5) as s:
            assert s.con.execute("SELECT count(*) FROM insider_tx").fetchone()[0] == 0
    finally:
        holder.wait(timeout=30)
