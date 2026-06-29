# tail-bot

Copy-trades a Polymarket sharp's MLB bets onto Kalshi using a cumulative-size
trigger. The strategy: tail only trades that are part of a *meaningful*
position (his real directional bets), not the tiny ladder/exit fills that
make up most of his trade count.


## How it works (planned)

1. **Poll** Polymarket activity API for HomeRunHazard's trades
2. **Accumulate** USD size per `(conditionId, outcome)` key
3. **Trigger** when cumulative size on a market crosses `$TRIGGER_USD`
   (~$3K-5K based on backtest tuning)
4. **Filter** to MLB only, pregame only (skip after `first_pitch_time`)
5. **Map** Polymarket slug → Kalshi ticker
6. **Guard** — only execute if Kalshi best ask is within 2¢ of his entry
7. **Place** order on Kalshi at our small fixed stake
8. **Persist** state so we don't double-trigger after restart

## Layout

```
tail-bot/
├── README.md                this file
├── requirements.txt
├── src/
│   ├── polymarket.py        activity API client + historical fetch
│   ├── trigger.py           cumulative-size accumulator + trigger logic
│   ├── backtest.py          replay history through trigger logic
│   ├── kalshi_map.py        polymarket_slug -> kalshi_ticker (MLB)
│   └── runner.py            live runner (TBD - phase 2)
├── data/                    cached historical data
└── results/                 backtest outputs
```


## Dependencies

- Polymarket data APIs (read-only, no auth)
- Kalshi API (KalshiBot already has the client + auth)

## Usage
pkill -f "[p]ython3 -m bot.wallet_alerts"
bash scripts/start_matanovik_alerts.sh
