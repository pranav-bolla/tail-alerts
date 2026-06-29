#!/bin/bash
# Start consensus-tail bot in V2 signal mode (email alerts + backtest ledger).
set -euo pipefail
cd /home/pranav/tail-bot

if pgrep -f "python3 -m bot.auto_pilot" >/dev/null 2>&1; then
  echo "$(date): bot already running, skipping"
  exit 0
fi

mkdir -p logs
echo "$(date): starting auto_pilot (signal-v2)" >> logs/scheduled_starts.log
setsid nohup python3 -m bot.auto_pilot --mode signal-v2 --cycle-seconds 600 \
  >> logs/auto_pilot_stdout.log 2>&1 &
disown
sleep 2
pgrep -af "python3 -m bot.auto_pilot" || { echo "failed to start"; exit 1; }
echo "$(date): started OK" >> logs/scheduled_starts.log
