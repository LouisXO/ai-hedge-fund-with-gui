#!/bin/zsh
# Late insider pass (19:15 PT = 22:15 ET, com.louis.agent.late_insider; S47b item 4). EDGAR stops accepting at
# 22:00 ET, and a Form 4 accepted between 17:30 and 22:00 ET still carries today's filing date. The 16:10 PT run
# only sees filings accepted by 19:10 ET; this pass fetches the rest of today's Form 4s and plans the insider book
# alone from the same bar, so a late filing is bought at the next open, as the backtest assumed. An order the
# 16:10 run already sent has the same client_order_id and is skipped.
# Orders go out only when BOTH AGENT_EXEC=on (~/.hedge-fund/.env) and ~/.hedge-fund/agent/late_insider.ok exist.
# The flag is created once S48-A is live (orders in flight occupy slots and cash); before that a second pass
# would spend the book's cash twice, so without the flag this is a dry run that prints the plan.
# Own log and output dir: the 16:10 section of execute.log and exec_<date>.json stay as that run wrote them.
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/late_insider.log
OUT=/Users/louis/optradar/out/agent/late
export PYTHONPATH=/Users/louis/hedge-fund
AGENT_EXEC=$(grep -E '^AGENT_EXEC=' "$HOME/.hedge-fund/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
FLAG=$HOME/.hedge-fund/agent/late_insider.ok
MODE=dry
[ "${AGENT_EXEC:-off}" = "on" ] && [ -f "$FLAG" ] && MODE=submit
echo "=== late_insider start $(date) AGENT_EXEC=${AGENT_EXEC:-off} flag=$([ -f "$FLAG" ] && echo yes || echo no) mode=$MODE ===" >> "$LOG"
$PY -W ignore -m agent.sources.sec_daily_form4 --realtime --days 2 >> "$LOG" 2>&1 || echo "form4 realtime refresh failed" >> "$LOG"
if [ "$MODE" = "submit" ]; then
  $PY -W ignore -m agent.execute --book insider --no-update --out-dir "$OUT" --submit >> "$LOG" 2>&1 || echo "late insider execute failed" >> "$LOG"
else
  $PY -W ignore -m agent.execute --book insider --no-update --out-dir "$OUT" >> "$LOG" 2>&1 || echo "late insider execute (dry) failed" >> "$LOG"
fi
echo "=== late_insider done $(date) ===" >> "$LOG"
