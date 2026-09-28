#!/bin/zsh
# Weekly: re-pull XBRL companyfacts (new 10-Q/10-K arrive daily) and rebuild the
# point-in-time factor table. ~45 min with 4 fetch threads; runs Sunday 03:00 PT
# (com.louis.agent.fundamentals) so nothing else holds panel.db's write lock.
set -u
cd /Users/louis/hedge-fund
PY=/Users/louis/.hedgefund-venv/bin/python
LOG=/Users/louis/optradar/out/agent/collect.log
export PYTHONPATH=/Users/louis/hedge-fund
echo "=== weekly fundamentals $(date) ===" >> "$LOG"
FAIL=0
# --max-age-days 6 = re-fetch every company (2026-09-28: the job called a --force flag that never existed, so nothing was ever refreshed)
# The first full refresh also brings ~4,000 companies loaded on 2026-09-20 up to the current tag list, which moves
# the long book's list; it waits for the owner's date (docs/AGENT_PLAN.md S46): touch ~/.hedge-fund/agent/refresh_fundamentals.ok
if [ -f "$HOME/.hedge-fund/agent/refresh_fundamentals.ok" ]; then
  $PY -W ignore -m agent.sources.sec_xbrl load --max-age-days 6 >> "$LOG" 2>&1 || { echo "xbrl load failed" >> "$LOG"; FAIL=1; }
else
  echo "xbrl refresh not run: waiting for the owner's go-ahead (refresh_fundamentals.ok missing)" >> "$LOG"
fi
[ "$FAIL" = "0" ] && $PY -W ignore -c "
from hedge_fund.features.panel import PanelStore
from agent.books.fundamentals import factor_inputs
with PanelStore() as s:
    df = factor_inputs(s)
print('fundamentals_pit', len(df), 'rows', df['ticker'].nunique(), 'names')
" >> "$LOG" 2>&1 || { echo "fundamentals rebuild failed or skipped" >> "$LOG"; FAIL=1; }
$PY -W ignore -m agent.sources.av_listing refresh >> "$LOG" 2>&1 || true
$PY -W ignore -m agent.sources.finra_short update >> "$LOG" 2>&1 || echo "finra short interest update failed (non-fatal)" >> "$LOG"   # S38 data, twice-monthly source
echo "=== done $(date) ===" >> "$LOG"
$PY -W ignore -m agent.audit >> "$LOG" 2>&1 || echo "audit reported failures" >> "$LOG"
$PY -W ignore -m agent.backup --weekly >> "$LOG" 2>&1 || echo "weekly backup failed (non-fatal)" >> "$LOG"   # large databases to iCloud Drive (2 kept)
exit $FAIL
