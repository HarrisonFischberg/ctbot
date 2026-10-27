#!/bin/bash
# Research refresh jobs (all read-only), weekdays 2x a day. Run by launchd (com.ctbot.c2-track) on
# the Mac, or by ctbot-research.timer on a server (deploy/).
# 1) grow the local 5-minute bar history for the universe (Robinhood keeps only ~8 months)
# 2) refresh SPY daily bars for the Collective2 SPY comparison
# 3) update data/c2/track.json (Collective2, read-only GET endpoints only)
# 4) score the day's reviewed trade ideas (approved vs rejected) from the fresh bar cache
# 5) daily bars for the ranking model's stock list, the premarket plan and the end-of-day
#    reconciliation (data/day_plan/, data/eod/; read-only)
# 6) after the close only: the strategy factory (ctbot-factory), whose live list the factory book trades;
#    it first fetches full daily history once for any symbol whose cache is short
set -u -o pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=logs/c2-track.log
mkdir -p logs
days_ago() { .venv/bin/python -c "import datetime, sys; print(datetime.date.today() - datetime.timedelta(days=int(sys.argv[1])))" "$1"; }
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  .venv/bin/ctbot-fetch-bars --interval 5minute --since "$(days_ago 14)" 2>&1 | tail -1 \
    || echo "5-minute bar refresh failed (Robinhood sign-in expired? run .venv/bin/ctbot-robinhood-login)"
  .venv/bin/ctbot-fetch-bars --interval day --since "$(days_ago 30)" \
    SPY QQQ QLD IWM DIA XLK XLF XLV XLY XLI XLP XLU XLE XLB SMH GLD TLT 2>&1 | tail -1 \
    || echo "daily bar refresh failed (Robinhood sign-in expired? run .venv/bin/ctbot-robinhood-login); tracking anyway"
  .venv/bin/ctbot-fetch-bars --interval day --since "$(days_ago 30)" \
    $(.venv/bin/python -c 'from ctbot.research.ranker import default_universe as u; print(" ".join(u()))') \
    2>&1 | tail -1 || echo "stock daily bar refresh failed"
  .venv/bin/ctbot-c2-track 2>&1
  .venv/bin/ctbot-score-ideas 2>&1 || echo "idea scoring failed"
  .venv/bin/ctbot-perf > /dev/null 2>&1 || echo "strategy performance refresh failed"
  .venv/bin/ctbot-preflight 2>&1 || echo "PREFLIGHT FAILED (see data/preflight.json and the dashboard)"
  .venv/bin/ctbot-dayplan 2>&1 || echo "day plan failed"
  .venv/bin/ctbot-reconcile 2>&1 || echo "RECONCILIATION MISMATCH (see data/eod/)"
  if [ "$(TZ=America/New_York date +%H)" -ge 12 ]; then
    nice -n 10 .venv/bin/ctbot-factory --backfill 2>&1 | grep -v '^  ' | tail -6 || echo "strategy factory failed"
  fi
} >> "$LOG" 2>&1
