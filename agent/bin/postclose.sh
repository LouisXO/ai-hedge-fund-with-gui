#!/bin/zsh
# Right after the close (13:25 PT = 16:25 ET, com.louis.agent.postclose): the day's bookkeeping and the
# review, so the notification arrives 25 minutes after the bell instead of at 16:10 PT. Nothing here
# plans or sends an order — that stays in execute.sh at 16:10 PT (Alpaca's OPG window opens 19:00 ET
# and the evening's Form 4 filings need to be in first).
#   1. agent.execute --sync-only   bars through today (Alpaca, 16-min delay), SPY/IWM/VIX, fills synced,
#                                   model prices, lots vs positions reconciled, books marked
#   2. account_snapshot            moomoo real account (read-only): NAV, positions, today's deals
#   3. agent.watch                 watchlist alerts (runs again at 16:10 for evening filings; deduped)
#   4. agent.review                today's review page + lessons + notification
#   5. private_index               front page
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/execute.log
export PYTHONPATH=/Users/louis/hedge-fund
echo "=== postclose $(date) ===" >> "$LOG"
$PY -W ignore -m agent.execute --sync-only >> "$LOG" 2>&1 || echo "sync failed" >> "$LOG"
/Users/louis/.moomoo/venv/bin/python -W ignore /Users/louis/optradar/bin/account_snapshot.py --quiet >> "$LOG" 2>&1 || echo "account snapshot failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.watch >> "$LOG" 2>&1 || echo "watchlist failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.review >> "$LOG" 2>&1 || echo "review failed (non-fatal)" >> "$LOG"
/Users/louis/.moomoo/venv/bin/python /Users/louis/optradar/bin/private_index.py >> "$LOG" 2>&1 || echo "private index failed (non-fatal)" >> "$LOG"
