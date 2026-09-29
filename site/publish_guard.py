"""Pre-push guard for the public repository: refuse to publish anything that looks like the owner's
real (moomoo) account.

This repository is a public GitHub fork, and `site/publish.sh` pushes the whole `v2-rebuild` branch,
not only `site/public`. So the guard scans what a push would make public:

  --staged        names and added lines of the index against HEAD (the site/public files just built)
  --range A..B    names, added lines and commit messages of the commits a push would send
  --files F...    whole files (fixtures and manual checks)

Only added lines are scanned: text that is already public cannot be unpublished by blocking a push, and
scanning it again would block every later publish. Markers are deliberately narrow (real-account wording
next to a number, account-number shapes, `acct_*` table names outside code), because a false positive
stops the daily public site. An optional private list of literal terms (account numbers, exact amounts)
lives outside the repository in ~/.hedge-fund/publish_guard_terms.txt, one per line, `#` for comments.

Exit status: 0 clean, 1 a marker was found (each hit printed as file:line, marker, excerpt), 2 git error.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

TERMS_FILE = os.path.expanduser("~/.hedge-fund/publish_guard_terms.txt")
CODE_EXT = (".py", ".sh", ".zsh", ".sql")          # code reads acct_* tables by name; only data/docs may not

# (marker, regexes that must all match the same line, applies to code files too). Calibrated on the lines removed
# in 092f339 / bb19c72 and on the added lines of every commit since 2026-09-01 (docs, code, site/public):
# the leaks hit, nothing else does except .env.example-style names, which are excluded below.
_R = re.compile
_REAL = r"(?:真实账户|真实持仓|moomoo|futu|富途|用户的(?:实盘|账户))"
_SIGNED_PCT = r"[+\-−]\d[\d,.]*\s?%"
CONTENT_MARKERS: list[tuple[str, list[re.Pattern], bool]] = [
    ("acct_ 表名", [_R(r"\bacct_[a-z][a-z0-9_]*")], False),
    ("账户号", [_R(r"(?i)(\bacc(?:ount)?[_ ]?(?:id|no|num|number)\b|account\s*#|账户号码?|账号|户号)\s*[:=:#]?\s*\d{5,}")], True),
    ("真实账户 + 数字", [_R(r"真实(?:账户|持仓|仓位)[^\n\d]{0,10}\d")], True),
    ("真实账户收益率", [_R(_REAL + r"[^\n]{0,40}?" + _SIGNED_PCT)], True),
    ("实盘收益率", [_R(r"实盘[^\n\d]{0,4}" + _SIGNED_PCT)], True),
    ("实盘标签", [_R(r"[\"“'(]实盘[::]")], True),
    ("账户规模", [_R(r"(?:账户|实盘)(?:规模|总额|净值)?\s*(?:约|大约|有)\s*[$¥]?\s*\d")], True),
    ("占账户比例", [_R(r"占(?:实盘|真实账户|账户)"), _R(r"\d[\d.]*\s?%")], True),
    ("持股数量", [_R(r"持有\s*\d[\d,]*\s*股|(?i:\b(?:i|we|user|owner)\s+(?:own|owns|hold|holds)\s+\d[\d,]*\s+shares)")], True),
    ("个人成本", [_R(r"零成本的\s*\d|股[^\n]{0,4}(?:成本为零|零成本|已回本)|持仓成本\s*[$¥]?\d")], True),
    ("用户交易记录", [_R(r"(?:用户|我|本人)的?\s*(?:持有|持仓|操作|买入|卖出|成本价?)\s*[::]?\s*[$¥]?\d|用户想(?:加仓|减仓|买|卖|入)")], True),
    ("密钥", [_R(r"\b(?:PK|AK)[A-Z0-9]{18}\b|ALPACA_SECRET\s*=\s*\S{20,}")], True),
]
NAME_MARKERS = [
    ("文件名", re.compile(r"(?i)(^|/)(\.env(\.(?!example$|sample$)[^/]*)?|.*acct_.*|.*real_account.*|.*account_snapshot.*\.(json|csv|html)|PRIVATE[^/]*\.md)$")),
]


@dataclass
class Hit:
    path: str
    line: int
    marker: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  [{self.marker}]  {self.excerpt}"


def load_terms(path: str = TERMS_FILE) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return [t.strip() for t in fh if t.strip() and not t.lstrip().startswith("#") and len(t.strip()) >= 4]
    except OSError:
        return []


def scan_name(path: str) -> list[Hit]:
    return [Hit(path, 0, m, "文件名命中") for m, rx in NAME_MARKERS if rx.search(path)]


def scan_lines(path: str, lines: list[tuple[int, str]], terms: list[str] | None = None) -> list[Hit]:
    """lines: (line number, text). Returns every marker hit, one per (line, marker)."""
    is_code = path.endswith(CODE_EXT)
    low_terms = [t.lower() for t in (terms or [])]
    hits = []
    for n, text in lines:
        for marker, rxs, in_code in CONTENT_MARKERS:
            if is_code and not in_code:
                continue
            ms = [rx.search(text) for rx in rxs]
            if all(ms):
                a = max(0, ms[0].start() - 20)
                hits.append(Hit(path, n, marker, text[a:ms[0].end() + 40].strip()[:90]))
        low = text.lower()
        for i, t in enumerate(low_terms):
            if t in low:
                hits.append(Hit(path, n, f"私有词表第 {i + 1} 项", "(不回显)"))
    return hits


def parse_diff(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Added lines per file from `git diff -U0` output (binary files keep an empty list, so names still count)."""
    out: dict[str, list[tuple[int, str]]] = {}
    path, n = None, 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = raw.split(" b/", 1)[1] if " b/" in raw else None
            if path is not None:
                out.setdefault(path, [])
        elif raw.startswith("+++ "):
            if raw[4:] != "/dev/null":
                path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
                out.setdefault(path, [])
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            n = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path is not None:
            out[path].append((n, raw[1:]))
            n += 1
    return out


def scan_diff(diff: str, terms: list[str] | None = None) -> list[Hit]:
    hits = []
    for path, lines in parse_diff(diff).items():
        hits += scan_name(path) + scan_lines(path, lines, terms)
    return hits


def scan_files(paths: list[str], terms: list[str] | None = None) -> list[Hit]:
    hits = []
    for p in paths:
        hits += scan_name(p)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                hits += scan_lines(p, list(enumerate(fh.read().splitlines(), 1)), terms)
        except OSError as exc:
            hits.append(Hit(p, 0, "读取失败", str(exc)[:80]))
    return hits


def _git(args: list[str], cwd: str | None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          encoding="utf-8", errors="replace").stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--range", metavar="A..B")
    g.add_argument("--files", nargs="+")
    ap.add_argument("--repo", default=None, help="git working tree (default: current directory)")
    ap.add_argument("--terms", default=TERMS_FILE)
    args = ap.parse_args(argv)
    terms = load_terms(args.terms)
    try:
        if args.files:
            hits = scan_files(args.files, terms)
        elif args.staged:
            hits = scan_diff(_git(["diff", "--cached", "-U0", "--no-color", "--no-ext-diff"], args.repo), terms)
        else:
            base, _, tip = args.range.partition("..")
            hits = scan_diff(_git(["diff", "-U0", "--no-color", "--no-ext-diff", base, tip or "HEAD"], args.repo), terms)
            msgs = _git(["log", "--format=%h %B", args.range], args.repo)
            hits += scan_lines("(提交说明)", list(enumerate(msgs.splitlines(), 1)), terms)
    except subprocess.CalledProcessError as exc:
        print(f"publish_guard: git 出错,按命中处理:{(exc.stderr or '').strip()[:200]}", file=sys.stderr)
        return 2
    if not hits:
        print(f"publish_guard: 未发现真实账户标记(私有词表 {len(terms)} 项)")
        return 0
    print(f"publish_guard: 发现 {len(hits)} 处疑似真实账户信息,中止推送:")
    for h in hits[:50]:
        print("  " + str(h))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
