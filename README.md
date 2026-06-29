# tail-bot

Copy-trades a Polymarket sharp's MLB bets onto Kalshi using a cumulative-size
trigger. The strategy: tail only trades that are part of a *meaningful*
position (his real directional bets), not the tiny ladder/exit fills that
make up most of his trade count.

## Current target wallet

`HomeRunHazard` — `0x5268527977f700f9bf9b6d5cd843859e4e70135d`

Chosen because:
- **100% MLB pure-play** — perfect mapping to Kalshi (no skipped markets)
- **12,524 closed positions** all-time, **6,050 in last 90 days** — huge
  statistical sample, edge confidence is real
- Bucketed by stake size, his **$5K+ positions show +5–8% ROI** on 2,000+
  positions (his small bets are noise)
- Currently active daily — MLB peak season

See `../tail-analysis/` for the original tailability analysis that selected
this wallet from a pool of 45 candidates.

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

## Phases

- **Phase 1 (now): Backtest** — Replay HomeRunHazard's historical activity
  through the trigger logic, compute hypothetical PnL across parameter sweeps.
  Validates the strategy before any real money.
- **Phase 2: Live runner** — Wire into Poly-Monitor's polling +
  KalshiBot's order placement. Paper-trade for 2 weeks first.
- **Phase 3: Production** — Real capital, with stop-loss and drawdown limits.

## Dependencies

- Polymarket data APIs (read-only, no auth)
- Kalshi API (KalshiBot already has the client + auth)
- HomeRunHazard's closed-position data (cached from `../tail-analysis/`)
