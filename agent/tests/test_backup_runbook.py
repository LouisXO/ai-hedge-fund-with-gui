"""docs/RUNBOOK.md: commands that mirror code are checked against that code, so the two cannot drift apart."""
import os
import re

from integrations import claude_code_llm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _section(start, end):
    text = open(os.path.join(ROOT, "docs", "RUNBOOK.md"), encoding="utf-8").read()
    return text[text.index(start):text.index(end)]


def test_claude_hardening_commands_carry_the_sealed_env_and_isolation_flags():
    sec = _section("## 9. ", "## 10. ")
    blocks = [b for b in re.findall(r"```bash\n(.*?)```", sec, re.S) if re.search(r"claude\"? -p|CLAUDE\" -p", b)]
    assert len(blocks) == 3                                    # morning brief, weekly, manual check
    flags = claude_code_llm.SEALED_FLAGS
    # isolation flags as the code passes them; --tools and --output-format are per job by design
    wanted = []
    for i, f in enumerate(flags):
        if f.startswith("--") and f not in ("--tools", "--output-format"):
            nxt = flags[i + 1] if i + 1 < len(flags) and not flags[i + 1].startswith("--") else None
            wanted.append(f if nxt is None else f"{f} '{nxt}'" if nxt else f'{f} ""')
    assert "--no-session-persistence" in wanted and '--setting-sources ""' in wanted
    for b in blocks:
        for k, v in claude_code_llm.SEALED_ENV.items():
            assert f"{k}={v}" in b, b
        for w in wanted:
            assert w in b, (w, b)
