#!/bin/zsh
# Right after the close (13:25 PT = 16:25 ET, com.louis.agent.postclose): the day's bookkeeping and the
# review, so the notification arrives 25 minutes after the bell instead of at 16:10 PT. Nothing here
# plans or sends an order — that stays in execute.sh at 16:10 PT (Alpaca's OPG window opens 19:00 ET
# and the evening's Form 4 filings need to be in first).
#   1. agent.execute --sync-only   bars through today (Alpaca, 16-min delay), SPY/IWM/VIX, fills synced,
#                                   model prices, lots vs positions reconciled, books marked
#   2. account_snapshot            moomoo real account (read-only): NAV, positions, today's deals
#   3. agent.watch                 watchlist alerts (runs again at 16:10 for evening filings; deduped)
#   4a. agent.auction_basis        auction-basis NAV + missed-trade shadow (S27b)
#   4. agent.review                today's review page + lessons + notification
#   5. private_index               front page
#   6. public site publish (hedge-fund.louisleng.com, paper page with today's close)
#   7. data archives               SEC forms, Alpha Vantage estimates, moomoo IV / consensus / ratings, Alpaca borrow flags
#
# Step 1 feeds the books (fills, lots, cash, NAV): when it fails it writes a line with FAILED and the script
# exits 1 after every other step has run. Every other step is a record or a page, logged "(non-fatal)".
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/execute.log
export PYTHONPATH=/Users/louis/hedge-fund
echo "=== postclose $(date) ===" >> "$LOG"
FAILED_STEPS=""
$PY -W ignore -m agent.execute --sync-only >> "$LOG" 2>&1 || { echo "sync FAILED (exit $?): fills, lots and NAV not booked" >> "$LOG"; FAILED_STEPS="$FAILED_STEPS sync"; }
/Users/louis/.moomoo/venv/bin/python -W ignore /Users/louis/optradar/bin/account_snapshot.py --quiet >> "$LOG" 2>&1 || echo "account snapshot failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.watch >> "$LOG" 2>&1 || echo "watchlist failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.watch_reminders >> "$LOG" 2>&1 || echo "moomoo reminder sync failed (non-fatal)" >> "$LOG"   # watchlist levels -> moomoo app push
$PY -W ignore -m agent.balder_log >> "$LOG" 2>&1 || echo "balder log failed (non-fatal)" >> "$LOG"          # posts the user dropped into iCloud/Balder, scored
$PY -W ignore -m agent.auction_basis >> "$LOG" 2>&1 || echo "auction basis failed (non-fatal)" >> "$LOG"      # fills re-priced at the opening cross; missed-trade shadow
if [ -f agent/dividends.py ]; then   # S48 package C; the guard lets this script run before that module is merged
  $PY -W ignore -m agent.dividends >> "$LOG" 2>&1 || echo "dividends failed (non-fatal)" >> "$LOG"
fi
$PY -W ignore -m agent.shadow_v2 >> "$LOG" 2>&1 || echo "shadow v2 failed (non-fatal)" >> "$LOG"            # S44: six shadow lines replayed on the recorded lists
$PY -W ignore -m agent.drift >> "$LOG" 2>&1 || echo "drift monitor failed (non-fatal)" >> "$LOG"                # paper vs rule replay vs backtest range
if [ -f agent/evaluate.py ]; then    # S48 package C (frozen evaluation metrics); guarded like dividends
  $PY -W ignore -m agent.evaluate >> "$LOG" 2>&1 || echo "evaluate failed (non-fatal)" >> "$LOG"
fi
$PY -W ignore -m agent.attribution >> "$LOG" 2>&1 || echo "attribution failed (non-fatal)" >> "$LOG"           # week to date; one row per week in attribution.jsonl
$PY -W ignore -m agent.review >> "$LOG" 2>&1 || echo "review failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.dashboard >> "$LOG" 2>&1 || echo "dashboard failed (non-fatal)" >> "$LOG"
$PY -W ignore site/build_paper.py >> "$LOG" 2>&1 || echo "paper page failed (non-fatal)" >> "$LOG"      # private copy of the public paper page (+ public copy, pushed at 08:41)
$PY -W ignore -m agent.health --no-llm-probe >> "$LOG" 2>&1 || echo "health check failed (non-fatal)" >> "$LOG"                               # out/health.json for the front page; notifies on a new failure
/Users/louis/.moomoo/venv/bin/python /Users/louis/optradar/bin/private_index.py >> "$LOG" 2>&1 || echo "private index failed (non-fatal)" >> "$LOG"
# --- public site: publish today's close (the 08:41 run publishes too, but then the data is a day old) ---
PUBLISH=1 zsh /Users/louis/hedge-fund/site/publish.sh "$(date +%F)" >> "$LOG" 2>&1 || echo "public site publish failed (non-fatal)" >> "$LOG"
# --- data archives (after the review and the site, so they never delay the notification) ---
$PY -W ignore -m agent.sources.sec_forms update --days 3 >> "$LOG" 2>&1 || echo "sec forms update failed (non-fatal)" >> "$LOG"      # 424B5 / S-3 / 144
$PY -W ignore -m agent.sources.av_estimates --max 20 >> "$LOG" 2>&1 || echo "av estimates failed (non-fatal)" >> "$LOG"              # 20 names/day, shares the AV quota
$PY -W ignore -m agent.sources.daily_archive >> "$LOG" 2>&1 || echo "daily archive failed (non-fatal)" >> "$LOG"                  # moomoo IV/consensus/ratings, Alpaca borrow flags
$PY -W ignore -m agent.sources.alpaca_intraday --table bars30 --universe --start 2024-01-01 --max-seconds 7200 >> "$LOG" 2>&1 || echo "intraday bars30 update failed (non-fatal)" >> "$LOG"   # incremental: only the new session
$PY -W ignore -m agent.sources.alpaca_intraday --table bars5 --options --start 2023-12-01 --max-seconds 3600 >> "$LOG" 2>&1 || echo "intraday bars5 update failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.backup >> "$LOG" 2>&1 || echo "backup failed (non-fatal)" >> "$LOG"                                    # daily tier to iCloud Drive (14 kept)
# --- last: health with the Claude login probe and today's backup status, front page again ---
$PY -W ignore -m agent.health >> "$LOG" 2>&1 || echo "health check failed (non-fatal)" >> "$LOG"                               # out/health.json for the front page; notifies on a new failure
/Users/louis/.moomoo/venv/bin/python /Users/louis/optradar/bin/private_index.py >> "$LOG" 2>&1 || echo "private index failed (non-fatal)" >> "$LOG"
if [ -n "$FAILED_STEPS" ]; then
  echo "postclose job FAILED steps:$FAILED_STEPS ($(date))" >> "$LOG"
  exit 1
fi
exit 0
