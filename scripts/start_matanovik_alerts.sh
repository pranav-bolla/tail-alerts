#!/bin/bash
# Email when configured Polymarket wallets place new trades.
# Watches are listed in .env as WALLET_ALERTS=username:0xwallet,...
set -euo pipefail
cd /home/pranav/tail-bot

# Stop legacy consensus bot if still running
if pgrep -f "python3 -m bot.auto_pilot" >/dev/null 2>&1; then
  echo "$(date): stopping auto_pilot" >> logs/scheduled_starts.log
  pkill -f "python3 -m bot.auto_pilot" || true
  sleep 1
fi

if pgrep -f "[p]ython3 -m bot.wallet_alerts" >/dev/null 2>&1; then
  echo "$(date): wallet_alerts already running, skipping"
  exit 0
fi

mkdir -p logs
echo "$(date): starting wallet_alerts (WALLET_ALERTS from .env)" >> logs/scheduled_starts.log
setsid nohup python3 -m bot.wallet_alerts --poll-seconds 90 \
  >> logs/wallet_alerts_stdout.log 2>&1 &
disown
sleep 2
pgrep -af "[p]ython3 -m bot.wallet_alerts" || { echo "failed to start"; exit 1; }
echo "$(date): wallet_alerts started OK" >> logs/scheduled_starts.log
