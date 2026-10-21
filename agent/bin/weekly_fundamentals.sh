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
# S54: a restore-tested copy of panel.db before anything below rewrites its history (the 10/05 refresh moved the backtest
# with no copy to compare against); 2 kept in ~/.hedge-fund/agent/snapshots/panel/. Without a good copy nothing that
# rewrites panel.db's history runs this week (the XBRL re-fetch, the ADS ratios, the fundamentals rebuild, the listing
# refresh), and the job fails so the health page shows it: a week of stale fundamentals costs less than a refresh that
# can never be diffed. The short-interest update (short.db), the audit and the weekly backup still run.
SNAP_OK=1
$PY -W ignore -m agent.backup --snapshot panel >> "$LOG" 2>&1 || { echo "panel snapshot FAILED (exit $?): XBRL, ADS ratios, fundamentals and listing not refreshed this week" >> "$LOG"; SNAP_OK=0; FAIL=1; }
if [ "$SNAP_OK" = "1" ]; then
# --max-age-days 6 = re-fetch every company (2026-09-28: the job called a --force flag that never existed, so nothing was ever refreshed)
# The first full refresh also brings ~4,000 companies loaded on 2026-09-20 up to the current tag list, which moves
# the long book's list; it waits for the owner's date (docs/AGENT_PLAN.md S46): touch ~/.hedge-fund/agent/refresh_fundamentals.ok
if [ -f "$HOME/.hedge-fund/agent/refresh_fundamentals.ok" ]; then
  $PY -W ignore -m agent.sources.sec_xbrl load --max-age-days 6 >> "$LOG" 2>&1 || { echo "xbrl load FAILED (exit $?)" >> "$LOG"; FAIL=1; }
else
  echo "xbrl refresh not run: waiting for the owner's go-ahead (refresh_fundamentals.ok missing)" >> "$LOG"
fi
# S53a: ADS ratios from the covers of 20-F/40-F filings not read yet (new ones since last week; ~1 s each), then
# panel.ads_ratio with agent/ads_ratio.yaml applied. Non-fatal: a new 20-F of a depositary-share company without a
# ratio gives that row no share count (flag ads_unknown; the daily moomoo count covers it) until the next run.
$PY -W ignore -m agent.sources.ads_ratio fetch --workers 4 >> "$LOG" 2>&1 || echo "ads_ratio fetch failed (non-fatal)" >> "$LOG"
$PY -W ignore -m agent.sources.ads_ratio commit >> "$LOG" 2>&1 || echo "ads_ratio commit failed (non-fatal: the old table stays)" >> "$LOG"
[ "$FAIL" = "0" ] && $PY -W ignore -c "
from hedge_fund.features.panel import PanelStore
from agent.books.fundamentals import factor_inputs
with PanelStore() as s:
    df = factor_inputs(s)
print('fundamentals_pit', len(df), 'rows', df['ticker'].nunique(), 'names')
" >> "$LOG" 2>&1 || { echo "fundamentals rebuild FAILED or skipped" >> "$LOG"; FAIL=1; }
# the listing table is the books' universe mask: a failed refresh is logged and fails the job (it used to be '|| true')
$PY -W ignore -m agent.sources.av_listing refresh >> "$LOG" 2>&1 || { echo "listing refresh FAILED (exit $?)" >> "$LOG"; FAIL=1; }
fi   # SNAP_OK
$PY -W ignore -m agent.sources.finra_short update >> "$LOG" 2>&1 || echo "finra short interest update failed (non-fatal)" >> "$LOG"   # S38 data, twice-monthly source
echo "=== done $(date) ===" >> "$LOG"   # the audit and the weekly backup below stay non-fatal
$PY -W ignore -m agent.audit >> "$LOG" 2>&1 || echo "audit reported failures" >> "$LOG"
$PY -W ignore -m agent.backup --weekly >> "$LOG" 2>&1 || echo "weekly backup failed (non-fatal)" >> "$LOG"   # large databases to iCloud Drive (2 kept)
[ "$FAIL" = "0" ] || echo "weekly fundamentals job FAILED ($(date))" >> "$LOG"
exit $FAIL
