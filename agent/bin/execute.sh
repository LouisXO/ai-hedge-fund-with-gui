#!/bin/zsh
# After the close (13:40 PT = 16:40 ET, com.louis.agent.execute): pull today's Form 4 index,
# refresh bars for the whole universe, then run the paper executor. Orders are only sent
# when AGENT_EXEC=on is set in ~/.hedge-fund/.env; anything else is a dry run that prints
# the order list to the log. The broker client refuses non-paper credentials regardless.
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/execute.log
export PYTHONPATH=/Users/louis/hedge-fund
AGENT_EXEC=$(grep -E '^AGENT_EXEC=' "$HOME/.hedge-fund/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
echo "=== execute $(date) AGENT_EXEC=${AGENT_EXEC:-off} ===" >> "$LOG"
$PY -W ignore -m agent.sources.sec_daily_form4 --days 2 >> "$LOG" 2>&1 || echo "form4 refresh failed (non-fatal)" >> "$LOG"
if [ "${AGENT_EXEC:-off}" = "on" ]; then
  $PY -W ignore -m agent.execute --submit >> "$LOG" 2>&1 || echo "execute failed" >> "$LOG"
else
  $PY -W ignore -m agent.execute >> "$LOG" 2>&1 || echo "execute (dry) failed" >> "$LOG"
fi
