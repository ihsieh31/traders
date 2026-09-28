#!/bin/zsh
# Long-running supervisor for the survivorship data pull.
#
# Free tier, read from Tiingo's own pricing bundle:
#   50 requests/hour, 1000 requests/day, 500 unique symbols/MONTH, 1 GB/month
#
# 7,231 dead names are wanted. At 500 unique symbols/month that is ~14 months on
# one account. The loop is therefore deliberately left running: it takes 500 per
# month, in priority order, and stops cleanly when the priority list is empty.
# Re-running it next month continues where it stopped.
#
# The per-process time budget (0.9h) is deliberately SHORTER than the cooldown
# ceiling so the process exits and the supervisor re-enters cleanly, rather than
# sitting inside one long sleep.
cd /Users/zongen/Downloads/codex/tradingBuffett
export TIINGO_TOKENS="$TIINGO_TOKENS"
LOG=/tmp/dl/tier2_loop.log
WANT=$(wc -l < /tmp/dl/worklist_2005_2025.csv)
for round in $(seq 1 500); do
  have=$(ls research/out/20260928-external-anchor/tiingo/bars/*.gz 2>/dev/null | wc -l | tr -d ' ')
  echo "[supervisor] round $round  bars=$have  want=$WANT  at $(date +%Y-%m-%dT%H:%M:%S)" >> $LOG
  if [ "$have" -ge "$WANT" ]; then echo "[supervisor] COMPLETE $have" >> $LOG; break; fi
  .venv-p2/bin/python -u research/src/tools/tiingo_fetch_loop.py 0.85 >> /tmp/dl/loop_out.log 2>&1
  sleep 20
done
