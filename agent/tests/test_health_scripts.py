"""Job scripts: a failed book-feeding step writes FAILED and the script exits 1 after the other steps ran;
archive steps stay non-fatal. Each script runs on a rewritten copy whose python is a stub, so nothing real runs.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
STUB = """#!/bin/zsh
echo "ran $*"
for m in ${=FAIL_MODULES}; do
  if [[ " $* " == *" $m "* ]]; then echo "Traceback (most recent call last):"; exit 3; fi
done
exit 0
"""


def run(tmp_path: Path, script: str, fail: str = "", env_exec: str = "on", modules=()) -> tuple[int, list[str]]:
    repo, home, log = tmp_path / "repo", tmp_path / "home", tmp_path / "job.log"
    (repo / "agent").mkdir(parents=True, exist_ok=True)
    for m in modules:                                            # modules another package adds (guarded in the script)
        (repo / "agent" / f"{m}.py").write_text("")
    (home / ".hedge-fund" / "agent").mkdir(parents=True, exist_ok=True)
    (home / ".hedge-fund" / ".env").write_text(f"AGENT_EXEC={env_exec}\n")
    stub = tmp_path / "py"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    text = (BIN / script).read_text()
    text = re.sub(r"^LOG=.*$", f"LOG={log}", text, flags=re.M)
    text = re.sub(r"^PY=.*$", f"PY={stub}", text, flags=re.M)
    text = text.replace("/Users/louis/.moomoo/venv/bin/python", str(stub))
    text = text.replace("PUBLISH=1 zsh /Users/louis/hedge-fund/site/publish.sh", f"{stub} publish")
    text = text.replace("cd /Users/louis/hedge-fund", f"cd {repo}").replace("=/Users/louis/hedge-fund", f"={repo}")
    assert "/Users/louis" not in text.replace("/Users/louis/optradar/bin/", ""), "the copy must not reach production"
    (tmp_path / script).write_text(text)
    env = {**os.environ, "HOME": str(home), "FAIL_MODULES": fail}
    rc = subprocess.run(["zsh", str(tmp_path / script)], env=env, capture_output=True, text=True, timeout=60).returncode
    return rc, log.read_text().splitlines()


def ran(lines, module):
    return any(l.startswith("ran ") and f" {module}" in f"{l} " for l in lines)


def test_execute_all_ok_exits_0(tmp_path):
    rc, lines = run(tmp_path, "execute.sh")
    assert rc == 0 and not any("FAILED" in l or "failed" in l for l in lines)
    assert ran(lines, "--submit")


def test_execute_form4_failure_is_fatal_but_the_other_steps_still_run(tmp_path):
    rc, lines = run(tmp_path, "execute.sh", fail="agent.sources.sec_daily_form4")
    assert rc == 1
    assert any(l.startswith("form4 realtime refresh FAILED (exit 3)") for l in lines)
    assert ran(lines, "agent.execute") and ran(lines, "agent.health")
    assert lines[-1].startswith("execute job FAILED steps: form4_realtime")


def test_execute_failure_and_archive_failure(tmp_path):
    rc, lines = run(tmp_path, "execute.sh", fail="agent.execute agent.sources.sec_13d")
    assert rc == 1 and "13d refresh failed (non-fatal)" in lines and "execute FAILED (exit 3)" in lines
    assert lines[-1].startswith("execute job FAILED steps: execute")
    rc, lines = run(tmp_path / "b", "execute.sh", fail="agent.sources.sec_13d agent.watch agent.dashboard")
    assert rc == 0 and not any("FAILED" in l for l in lines)


def test_postclose_sync_is_fatal_and_the_new_steps_are_guarded(tmp_path):
    rc, lines = run(tmp_path, "postclose.sh")
    assert rc == 0 and not ran(lines, "agent.dividends") and not ran(lines, "agent.evaluate")
    rc, lines = run(tmp_path / "b", "postclose.sh", fail="agent.dividends", modules=("dividends", "evaluate"))
    assert rc == 0 and "dividends failed (non-fatal)" in lines and ran(lines, "agent.evaluate")
    order = [l.split()[4] for l in lines if l.startswith("ran -W ignore -m ")]
    assert order.index("agent.dividends") == order.index("agent.auction_basis") + 1
    assert order.index("agent.evaluate") == order.index("agent.drift") + 1
    rc, lines = run(tmp_path / "c", "postclose.sh", fail="--sync-only")
    assert rc == 1 and any(l.startswith("sync FAILED (exit 3)") for l in lines)
    assert ran(lines, "agent.backup") and lines[-1].startswith("postclose job FAILED steps: sync")


def test_weekly_listing_refresh_failure_is_logged_and_fails_the_job(tmp_path):
    rc, lines = run(tmp_path, "weekly_fundamentals.sh", fail="agent.sources.av_listing")
    assert rc == 1 and "listing refresh FAILED (exit 3)" in lines
    assert ran(lines, "agent.backup") and lines[-1].startswith("weekly fundamentals job FAILED")
    rc, lines = run(tmp_path / "b", "weekly_fundamentals.sh", fail="agent.sources.finra_short")
    assert rc == 0 and "finra short interest update failed (non-fatal)" in lines
