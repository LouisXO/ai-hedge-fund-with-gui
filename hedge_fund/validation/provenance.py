"""Pre-registration provenance: was the defining file pushed before the run? (S48, audit research #6)

A pre-registration only counts if its definition was public before the result existed. The
audit found four of eight experiments whose definition reached origin together with (or after)
the result, and one where definition and result shared a commit. This helper makes the check
mechanical and puts the evidence into the report.

Use it at the top of a NEW experiment script (existing scripts are not edited):

    from hedge_fund.validation.provenance import require_pushed
    PROV = require_pushed(__file__)          # raises before any computation if not pushed
    ...
    report["provenance"] = PROV              # {"file", "commit", "remote_branches", "checked_at"}

Rules (docs/AGENT_PLAN.md §5.5, thresholds v3):
  - the defining file must have no uncommitted changes;
  - the last commit that touched it must be contained in a tracking branch of origin
    (`git branch -r --contains <commit>`, filtered to `origin/`), i.e. pushed; another remote
    (a fork, an upstream) does not count;
  - the commit hash goes into the report JSON.

`git branch -r` reads the local remote-tracking refs, so no network call is made; push first,
then run. `allow_unpushed=True` is for dry runs only, and the returned dict then says so.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess


class ProvenanceError(RuntimeError):
    """The defining file is dirty, uncommitted or not on the remote."""


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


def definition_provenance(path: str, repo: str | None = None, remote: str = "origin") -> dict:
    """Facts about the file that defines an experiment: last commit, dirty or not, branches of `remote`.

    Only `remote`'s tracking branches count: a push to a fork or another remote is not public here.
    """
    path = os.path.abspath(path)
    repo = repo or _git(os.path.dirname(path), "rev-parse", "--show-toplevel")
    rel = os.path.relpath(path, repo)
    commit = _git(repo, "log", "-n", "1", "--format=%H", "--", rel)
    dirty = bool(_git(repo, "status", "--porcelain", "--", rel))
    branches = []
    if commit:
        branches = [b.strip() for b in _git(repo, "branch", "-r", "--contains", commit).splitlines()
                    if b.strip().startswith(remote + "/") and "->" not in b]
    return {"file": rel, "commit": commit or None, "dirty": dirty, "remote_branches": branches,
            "pushed": bool(commit) and not dirty and bool(branches),
            "checked_at": dt.datetime.now().isoformat(timespec="seconds")}


def require_pushed(path: str, repo: str | None = None, allow_unpushed: bool = False, remote: str = "origin") -> dict:
    """definition_provenance(), raising ProvenanceError unless the definition is committed and pushed to `remote`."""
    p = definition_provenance(path, repo, remote)
    if not p["pushed"]:
        why = ("not committed" if not p["commit"] else "uncommitted changes" if p["dirty"]
               else f"commit {p['commit'][:10]} is on no {remote} branch (push it first)")
        if not allow_unpushed:
            raise ProvenanceError(f"{p['file']}: {why}")
        p["warning"] = why
    return p
