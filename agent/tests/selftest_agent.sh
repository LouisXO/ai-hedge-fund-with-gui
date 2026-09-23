#!/bin/zsh
# Isolated smoke test of the paper-execution chain (docs/AGENT_PLAN.md §6):
#   1. pytest agent/tests
#   2. agent.execute DRY RUN against a COPY of optradar.db, JSON to a temp dir (reads the paper
#      account and panel.db read-only; never submits; the real ledger and out/ are not touched)
#   3. agent.brief --append-html into a stub page from the copied ledger, LLM off
# Asserts the JSON shape, the HTML sections, and that the real optradar.db / out/agent were unchanged.
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
export PYTHONPATH=/Users/louis/hedge-fund
REAL_DB=/Users/louis/optradar/optradar.db
REAL_OUT=/Users/louis/optradar/out/agent
T=$(mktemp -d /tmp/agent-selftest.XXXX)
fail=0

before_db=$(stat -f %m "$REAL_DB"); before_out=$(ls "$REAL_OUT" | md5)

$PY -m pytest -q agent/tests > "$T/pytest.log" 2>&1 || { echo "❌ pytest failed (see $T/pytest.log)"; tail -5 "$T/pytest.log"; fail=1; }

cp "$REAL_DB" "$T/test.db"
$PY -W ignore -m agent.execute --no-update --optradar-db "$T/test.db" --out-dir "$T/out" > "$T/execute.log" 2>&1
RC=$?
[ $RC -eq 0 ] || { echo "❌ agent.execute dry run exit $RC"; tail -5 "$T/execute.log"; fail=1; }
J=$(ls "$T"/out/exec_*.json 2>/dev/null | head -1)
[ -s "$J" ] || { echo "❌ no exec json"; fail=1; }
if [ -s "$J" ]; then
  AS_OF=$($PY - "$J" <<'PY' || fail=1
import json, sys
d = json.load(open(sys.argv[1])); bad = []
for k in ("as_of", "last_session", "stale_bars", "tif", "mode", "account_equity", "sync", "reconcile", "targets", "books", "orders"):
    if k not in d: bad.append(f"json missing {k}")
if d.get("mode") != "dry_run": bad.append(f"mode is {d.get('mode')!r}, expected dry_run")
if any(not o.get("dry_run") for o in d.get("orders", [])): bad.append("an order is not marked dry_run")
if not d.get("books"): bad.append("no book NAV rows")
if bad:
    print("\n".join("❌ " + b for b in bad), file=sys.stderr); sys.exit(1)
print(d["as_of"])
PY
)
fi

echo '<html><body><h1>stub</h1><footer>x</footer></body></html>' > "$T/page.html"
AGENT_NO_LLM=1 $PY -W ignore -m agent.brief "${AS_OF:-$(date +%F)}" --optradar-db "$T/test.db" --append-html "$T/page.html" > "$T/brief.log" 2>&1 \
  || { echo "❌ agent.brief exit $?"; tail -3 "$T/brief.log"; fail=1; }
grep -q "⑨ Agent" "$T/page.html" || { echo "❌ html missing ⑨ Agent"; fail=1; }
grep -q "Alpaca 模拟盘" "$T/page.html" || { echo "❌ html missing 模拟盘 section"; fail=1; }
grep -q "评估点" "$T/page.html" || { echo "❌ html missing evaluation-point line"; fail=1; }

[ "$(stat -f %m "$REAL_DB")" = "$before_db" ] || { echo "❌ real optradar.db was modified"; fail=1; }
[ "$(ls "$REAL_OUT" | md5)" = "$before_out" ] || { echo "❌ real out/agent listing changed"; fail=1; }

if [ $fail -eq 0 ]; then
  echo "✅ selftest ok: as_of ${AS_OF:-?} · $(grep -c . "$T/execute.log") log lines · $(grep -o 'passed' "$T/pytest.log" | head -1) pytest"
  rm -rf "$T"
else
  echo "artifacts kept in $T"
fi
exit $fail
