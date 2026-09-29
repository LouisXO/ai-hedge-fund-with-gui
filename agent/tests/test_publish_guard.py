"""site/publish_guard.py: the pre-push scan for real-account markers in the public repository.

Fixture strings are assembled at runtime (J(...)) so this file's own lines never match a marker: the guard
scans every added line a push would publish, including this test file.
"""
import importlib.util
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("publish_guard", os.path.join(ROOT, "site", "publish_guard.py"))
pg = importlib.util.module_from_spec(_spec)
sys.modules["publish_guard"] = pg          # dataclasses look the module up by name
_spec.loader.exec_module(pg)


def J(*parts):
    return "".join(parts)


LEAKS = [   # (synthetic line, marker expected)
    (J("- 用户持有", " 25 ", "股,", "成本", "为零"), "持股数量"),
    (J("note: \"持有", " 25 股", "(零成本)\""), "个人成本"),
    (J("用户的真实", "账户在 moo", "moo,年初到 9 月 ", "+", "31.5%"), "真实账户收益率"),
    (J("实盘", "年内 ", "+", "12.0%"), "实盘收益率"),
    (J("(\"实", "盘:同日加仓\", \"...\")"), "实盘标签"),
    (J("账户", "约 ", "$2.1 万,期权为主"), "账户规模"),
    (J("单只占", "实盘不超过现有持仓(已占 ", "40", "%)"), "占账户比例"),
    (J("- 用户", "操作:", "$3.2 买入"), "用户交易记录"),
    (J("用户", "想加仓"), "用户交易记录"),
    (J("acc", "_id", " = ", "1234", "5678", "90"), "账户号"),
    (J("真实", "账户 ", "12 只持仓"), "真实账户 + 数字"),
    (J("I ", "hold ", "300 ", "shares of XYZ"), "持股数量"),
]

CLEAN = [
    "长线 v1 +35.7%、SPY +12.4%(模拟盘,公开)",
    "实盘 9 月收益冻结的修复",
    "DAY 限价单(前收盘 + 3%);实盘下单用前收盘 +3% 限价",
    "真实账户(moomoo)的任何数字、持仓、成交永远不上公开站",
    "它的 +0.6%/边是模拟器的成本,不是真实账户会付的成本",
    J("数据 moomoo OpenD,判断由 Claude 生成</div></header>",
      "x" * 60,
      "<td>长线 +0.4%</td>"),                                     # a data credit far from the page's numbers
    "影子书六条线照常记录(成本为零,单条规则的实盘外记录对 v3 有用)",
    "<h2>实盘:今日成交</h2>",
    "内部人书持有 5 个交易日",
]


@pytest.mark.parametrize("line,marker", LEAKS)
def test_each_synthetic_leak_hits_its_marker(line, marker):
    hits = pg.scan_lines("docs/notes/X.md", [(1, line)])
    assert marker in [h.marker for h in hits], (line, [h.marker for h in hits])


@pytest.mark.parametrize("line", CLEAN)
def test_ordinary_public_text_passes(line):
    assert pg.scan_lines("docs/AGENT_PLAN.md", [(1, line)]) == []


def test_acct_table_names_are_markers_in_docs_and_pages_but_not_in_code():
    line = J("SELECT * FROM acct", "_positions")
    assert [h.marker for h in pg.scan_lines("site/public/paper.html", [(3, line)])] == ["acct_ 表名"]
    assert pg.scan_lines("agent/review.py", [(3, line)]) == []
    assert pg.scan_lines("AGENT.md", [(1, J("该脚本不读任何 `acct", "_*` 表"))]) == []


def test_file_names():
    assert pg.scan_name(".env") and pg.scan_name("agent/.env.local") and pg.scan_name(J("site/public/acct", "_x.json"))
    assert pg.scan_name(J("docs/PRIV", "ATE.md"))
    assert not pg.scan_name(".env.example") and not pg.scan_name("integrations/moomoo_client.py")


def test_private_terms_file_hits_without_echoing_the_term(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("# comment\nZX-998877\nab\n", encoding="utf-8")          # 'ab' is too short and ignored
    loaded = pg.load_terms(str(terms))
    assert loaded == ["ZX-998877"]
    hits = pg.scan_lines("site/public/index.html", [(9, "footer zx-998877 end")], loaded)
    assert [h.marker for h in hits] == ["私有词表第 1 项"] and "998877" not in str(hits[0])
    assert pg.load_terms(str(tmp_path / "missing.txt")) == []


def test_parse_diff_keeps_new_line_numbers_and_skips_removed_lines():
    diff = "\n".join([
        "diff --git a/docs/a.md b/docs/a.md", "--- a/docs/a.md", "+++ b/docs/a.md",
        "@@ -3,1 +3,2 @@", "-old line", "+new one", "+new two",
        "diff --git a/site/public/x.png b/site/public/x.png", "Binary files a/site/public/x.png and b/site/public/x.png differ",
    ])
    assert pg.parse_diff(diff) == {"docs/a.md": [(3, "new one"), (4, "new two")], "site/public/x.png": []}


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "README.md").write_text("公开说明\n", encoding="utf-8")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_staged_mode_scans_only_added_lines(repo, tmp_path):
    none = str(tmp_path / "no_terms.txt")
    (repo / "page.html").write_text("<p>模拟盘 +1.2%</p>\n", encoding="utf-8")
    _git(repo, "add", "page.html")
    assert pg.main(["--staged", "--repo", str(repo), "--terms", none]) == 0
    (repo / "page.html").write_text("<p>模拟盘 +1.2%</p>\n" + J("<p>用户持有", " 25 股</p>\n"), encoding="utf-8")
    _git(repo, "add", "page.html")
    assert pg.main(["--staged", "--repo", str(repo), "--terms", none]) == 1


def test_range_mode_scans_outgoing_commits_and_their_messages(repo, tmp_path, capsys):
    none = str(tmp_path / "no_terms.txt")
    _git(repo, "tag", "pushed")
    (repo / "notes.md").write_text("中性内容\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", "clean note")
    assert pg.main(["--range", "pushed..HEAD", "--repo", str(repo), "--terms", none]) == 0
    _git(repo, "commit", "-q", "--allow-empty", "-m", J("note: 账户", "约 $3 万"))
    assert pg.main(["--range", "pushed..HEAD", "--repo", str(repo), "--terms", none]) == 1
    assert "(提交说明)" in capsys.readouterr().out
    assert pg.main(["--range", "no-such-ref..HEAD", "--repo", str(repo), "--terms", none]) == 2


def test_publish_sh_runs_the_guard_before_commit_and_before_push():
    src = open(os.path.join(ROOT, "site", "publish.sh"), encoding="utf-8").read()
    i_add, i_staged, i_commit = src.index("git add site/public"), src.index("guard --staged"), src.index("git commit")
    i_pull, i_range, i_push = src.index("git pull"), src.index("guard --range origin/v2-rebuild..HEAD"), src.index("git push")
    assert i_add < i_staged < i_commit < i_pull < i_range < i_push
    assert "blocked " in src[i_staged:i_commit] and "blocked " in src[i_range:i_push] and "exit 3" in src
