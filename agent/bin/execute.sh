#!/bin/zsh
# After the close (16:10 PT = 19:10 ET, com.louis.agent.execute; Alpaca takes OPG orders only 19:00-09:28 ET): pull today's Form 4 index,
# refresh bars for the whole universe, then run the paper executor. Orders are only sent
# when AGENT_EXEC=on is set in ~/.hedge-fund/.env; anything else is a dry run that prints
# the order list to the log. The broker client refuses non-paper credentials regardless.
#
# Steps that feed the books (the Form 4 realtime fetch, agent.execute) write a line with FAILED when they fail,
# and the script then exits 1 after every other step has run (launchd keeps the code; agent.health reads the
# FAILED line). The other steps are archives and pages: their failure is logged "(non-fatal)" and nothing more.
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/execute.log
export PYTHONPATH=/Users/louis/hedge-fund
AGENT_EXEC=$(grep -E '^AGENT_EXEC=' "$HOME/.hedge-fund/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
echo "=== execute $(date) AGENT_EXEC=${AGENT_EXEC:-off} ===" >> "$LOG"
FAILED_STEPS=""
# book-feeding: the insider book's signals for tonight (exit 1 = EDGAR could not be asked; 0 filings is exit 0)
$PY -W ignore -m agent.sources.sec_daily_form4 --realtime --days 2 >> "$LOG" 2>&1 || { echo "form4 realtime refresh FAILED (exit $?): the insider book plans on what the panel already holds" >> "$LOG"; FAILED_STEPS="$FAILED_STEPS form4_realtime"; }
$PY -W ignore -m agent.sources.sec_13d update --days 5 >> "$LOG" 2>&1 || echo "13d refresh failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.sources.alpaca_news update --days 3 >> "$LOG" 2>&1 || echo "news refresh failed (non-fatal)" >> "$LOG"
# book-feeding: plan and send tonight's orders (exit 3 = nothing planned, e.g. stale bars)
if [ "${AGENT_EXEC:-off}" = "on" ]; then
  $PY -W ignore -m agent.execute --submit >> "$LOG" 2>&1 || { echo "execute FAILED (exit $?)" >> "$LOG"; FAILED_STEPS="$FAILED_STEPS execute"; }
else
  $PY -W ignore -m agent.execute >> "$LOG" 2>&1 || { echo "execute (dry) FAILED (exit $?)" >> "$LOG"; FAILED_STEPS="$FAILED_STEPS execute_dry"; }
fi
$PY -W ignore -m agent.watch >> "$LOG" 2>&1 || echo "watchlist failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.balder_record >> "$LOG" 2>&1 || echo "balder record failed (non-fatal)" >> "$LOG"   # balder-ai.com/record, once a day; private
$PY -W ignore -m agent.dashboard >> "$LOG" 2>&1 || echo "dashboard failed (non-fatal)" >> "$LOG"
$PY -W ignore site/build_paper.py >> "$LOG" 2>&1 || echo "paper page failed (non-fatal)" >> "$LOG"      # private copy of the public paper page (+ public copy, pushed at 08:41)
$PY -W ignore -m agent.health --no-llm-probe >> "$LOG" 2>&1 || echo "health check failed (non-fatal)" >> "$LOG"                               # out/health.json for the front page; notifies on a new failure
/Users/louis/.moomoo/venv/bin/python /Users/louis/optradar/bin/private_index.py >> "$LOG" 2>&1 || echo "private index failed (non-fatal)" >> "$LOG"
if [ -n "$FAILED_STEPS" ]; then
  echo "execute job FAILED steps:$FAILED_STEPS ($(date))" >> "$LOG"
  exit 1
fi
exit 0
