"""S48: the pre-registration provenance check, on a throwaway repo with a local bare 'origin'."""
from __future__ import annotations

import subprocess

import pytest

from hedge_fund.validation.provenance import ProvenanceError, definition_provenance, require_pushed


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    (work / "s99_experiment.py").write_text("# definition\n")
    _git(work, "add", "s99_experiment.py")
    _git(work, "commit", "-q", "-m", "S99 pre-registration")
    return work


def test_committed_but_not_pushed_is_refused(repo):
    f = str(repo / "s99_experiment.py")
    p = definition_provenance(f)
    assert p["commit"] and not p["dirty"] and p["remote_branches"] == [] and not p["pushed"]
    with pytest.raises(ProvenanceError, match="no remote branch"):
        require_pushed(f)
    assert require_pushed(f, allow_unpushed=True)["warning"].startswith("commit ")


def test_pushed_definition_returns_the_commit_for_the_report(repo):
    _git(repo, "push", "-q", "origin", "main")
    f = str(repo / "s99_experiment.py")
    p = require_pushed(f)
    assert p["pushed"] and p["commit"] == _git(repo, "rev-parse", "HEAD")
    assert p["remote_branches"] == ["origin/main"] and p["file"] == "s99_experiment.py"


def test_uncommitted_edit_after_the_push_is_refused(repo):
    _git(repo, "push", "-q", "origin", "main")
    (repo / "s99_experiment.py").write_text("# definition, edited after the push\n")
    with pytest.raises(ProvenanceError, match="uncommitted"):
        require_pushed(str(repo / "s99_experiment.py"))


def test_a_file_never_committed_is_refused(repo):
    (repo / "s98_new.py").write_text("x = 1\n")
    with pytest.raises(ProvenanceError, match="not committed"):
        require_pushed(str(repo / "s98_new.py"))
