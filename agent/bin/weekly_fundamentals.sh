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
$PY -W ignore -m agent.sources.sec_xbrl load --force >> "$LOG" 2>&1 || echo "xbrl load failed" >> "$LOG"
$PY -W ignore -c "
from hedge_fund.features.panel import PanelStore
from agent.books.fundamentals import factor_inputs
with PanelStore() as s:
    df = factor_inputs(s)
print('fundamentals_pit', len(df), 'rows', df['ticker'].nunique(), 'names')
" >> "$LOG" 2>&1 || echo "fundamentals rebuild failed" >> "$LOG"
$PY -W ignore -m agent.sources.av_listing refresh >> "$LOG" 2>&1 || true
echo "=== done $(date) ===" >> "$LOG"
$PY -W ignore -m agent.audit >> "$LOG" 2>&1 || echo "audit reported failures" >> "$LOG"
