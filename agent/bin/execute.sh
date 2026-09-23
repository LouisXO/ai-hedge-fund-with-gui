#!/bin/zsh
# After the close (16:10 PT = 19:10 ET, com.louis.agent.execute; Alpaca takes OPG orders only 19:00-09:28 ET): pull today's Form 4 index,
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
$PY -W ignore -m agent.sources.sec_daily_form4 --realtime --days 2 >> "$LOG" 2>&1 || echo "form4 realtime refresh failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.sources.sec_13d update --days 5 >> "$LOG" 2>&1 || echo "13d refresh failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.sources.alpaca_news update --days 3 >> "$LOG" 2>&1 || echo "news refresh failed (non-fatal)" >> "$LOG"
if [ "${AGENT_EXEC:-off}" = "on" ]; then
  $PY -W ignore -m agent.execute --submit >> "$LOG" 2>&1 || echo "execute failed" >> "$LOG"
else
  $PY -W ignore -m agent.execute >> "$LOG" 2>&1 || echo "execute (dry) failed" >> "$LOG"
fi
$PY -W ignore -m agent.watch >> "$LOG" 2>&1 || echo "watchlist failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.dashboard >> "$LOG" 2>&1 || echo "dashboard failed (non-fatal)" >> "$LOG"
/Users/louis/.moomoo/venv/bin/python /Users/louis/optradar/bin/private_index.py >> "$LOG" 2>&1 || echo "private index failed (non-fatal)" >> "$LOG"
