"""agent/bin/install_launchd.sh: reports repo vs installed plists and only prints launchctl commands."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "agent", "bin", "install_launchd.sh")

pytestmark = pytest.mark.skipif(not shutil.which("zsh") or not shutil.which("plutil"), reason="macOS only")


def _plist(label, hour):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>{label}</string>'
            f'<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer></dict></dict></plist>\n')


def test_reports_states_and_never_runs_launchctl_beyond_list(tmp_path):
    hf, opt, inst, fake = (tmp_path / d for d in ("hf", "opt", "inst", "bin"))
    for d in (hf / "agent" / "launchd", opt / "launchd", inst, fake):
        d.mkdir(parents=True)
    (hf / "agent/launchd/com.louis.a.plist").write_text(_plist("com.louis.a", 6))
    (hf / "agent/launchd/com.louis.b.plist").write_text(_plist("com.louis.b", 7))
    (hf / "agent/launchd/com.louis.c.plist").write_text(_plist("com.louis.c", 8))
    (opt / "launchd/com.louis.d.plist").write_text(_plist("com.louis.d", 9))
    (inst / "com.louis.a.plist").write_text(_plist("com.louis.a", 6).replace("<dict>", "<dict>\n\t"))   # same, other layout
    (inst / "com.louis.b.plist").write_text(_plist("com.louis.b", 17))                                 # differs
    (inst / "com.louis.c.plist.disabled").write_text(_plist("com.louis.c", 8))                         # stopped on purpose
    (inst / "com.louis.x.plist").write_text(_plist("com.louis.x", 1))                                  # not in any repo
    calls = tmp_path / "calls.txt"
    (fake / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" = list ] && printf "1\\t0\\tcom.louis.a\\n"\n')
    os.chmod(fake / "launchctl", 0o755)
    env = dict(os.environ, HF_DIR=str(hf), OPTRADAR_DIR=str(opt), LAUNCH_DIR=str(inst), PATH=f"{fake}:{os.environ['PATH']}")
    r = subprocess.run(["zsh", SCRIPT, "--diff"], capture_output=True, text=True, env=env)
    out = r.stdout
    assert r.returncode == 1                                            # b differs, d not installed
    line = {l.split()[0]: l for l in out.splitlines() if l.startswith("  com.louis.")}
    assert "一致" in line["com.louis.a"] and "已加载" in line["com.louis.a"]
    assert "不同" in line["com.louis.b"] and "未加载" in line["com.louis.b"]
    assert "已停用" in line["com.louis.c"] and "未安装" in line["com.louis.d"]
    assert "com.louis.x.plist 已安装但不在任何仓库里" in out
    assert "<integer>17</integer>" in out                               # --diff shows the difference
    assert "bootstrap gui/$(id -u)" in out and "com.louis.b.plist" in out
    assert "launchctl bootout gui/$(id -u)/com.louis.a " not in out      # nothing printed for a matching job
    assert calls.read_text().split() == ["list"]                         # printed, never executed
    assert (inst / "com.louis.b.plist").read_text() == _plist("com.louis.b", 17)
    assert not (inst / "com.louis.d.plist").exists()
