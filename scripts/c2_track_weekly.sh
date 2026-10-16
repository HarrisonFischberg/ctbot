#!/bin/zsh
# Research refresh jobs (all read-only), weekdays 2x a day. Run by launchd: com.ctbot.c2-track.
# 1) grow the local 5-minute bar history for the universe (Robinhood keeps only ~8 months)
# 2) refresh SPY daily bars for the Collective2 SPY comparison
# 3) update data/c2/track.json (Collective2, read-only GET endpoints only)
# 4) score the day's reviewed trade ideas (approved vs rejected) from the fresh bar cache
set -u -o pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=logs/c2-track.log
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  .venv/bin/ctbot-fetch-bars --interval 5minute --since "$(date -v-14d +%Y-%m-%d)" 2>&1 | tail -1 \
    || echo "5-minute bar refresh failed (Robinhood sign-in expired? run ctbot-robinhood-login)"
  .venv/bin/ctbot-fetch-bars --interval day --since "$(date -v-30d +%Y-%m-%d)" \
    SPY QQQ QLD IWM DIA XLK XLF XLV XLY XLI XLP XLU XLE XLB SMH 2>&1 | tail -1 \
    || echo "daily bar refresh failed (Robinhood sign-in expired? run ctbot-robinhood-login); tracking anyway"
  .venv/bin/ctbot-c2-track 2>&1
  .venv/bin/ctbot-score-ideas 2>&1 || echo "idea scoring failed"
  .venv/bin/ctbot-perf > /dev/null 2>&1 || echo "strategy performance refresh failed"
  .venv/bin/ctbot-preflight 2>&1 || echo "PREFLIGHT FAILED (see data/preflight.json and the dashboard)"
} >> "$LOG" 2>&1
