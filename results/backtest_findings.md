# HomeRunHazard Tail Strategy — Backtest Findings (v3)

## v3 update: snapshot-realistic entry pricing

The v2 backtest used HRH's `first_entry_price` as the bot's entry. Realistic
for a per-trade-streaming bot with sub-second latency, but **NOT realistic for
a snapshot-polling bot** (which is the design we're actually pursuing). By the
time cumulative size crosses $5K, he's been ladder-buying for ~3 hours and
the market has moved a median of +2¢, mean +7¢, p75 +17¢ from his first fill.

**The right model for our bot uses the price AT the moment cumulative crosses
the threshold.** When we redid the backtest with this entry model:

| Trigger | WR | v2 (idealized) ROI net | **v3 (realistic) ROI net** |
|---|---|---|---|
| $2K | 54.6% | +5.9% | **–2.6%** |
| $5K | 61.1% | +16.0% | **–0.9%** |
| **$10K** | **69.8%** | +32.6% | **+3.6%** |
| $15K | 71.8% | +34.1% | **+4.8%** |

**The strategy only has positive EV at $10K+ trigger.** Below that, his own
laddering runs the price up enough that there's no edge left by the time we
detect the threshold crossing.

**Polling interval doesn't materially affect results** (5/15/30/60-min polls
all give similar PnL at $5K trigger) — because his buildups take hours, the
additional drift from polling-delay is small. Snapshot polling at 15 min is
fine.

### Updated recommended parameters

- **Trigger:** $10K cumulative
- **Polling:** 15 min snapshots (no urgency — his buildups take 3+ hours)
- **Sample:** 364 trades over 6 weeks = ~60/month
- **Expected edge:** +$2.20/trade gross → ~+3.6% ROI net at $100 stake

This is thinner than the v2 numbers suggested but is the honest read.

---

# v2 findings (kept for reference — uses unrealistic entry pricing)

**Date:** 2026-06-09
**Wallet:** HomeRunHazard (`0x5268527977f700f9bf9b6d5cd843859e4e70135d`)
**Data:** 12,524 closed positions, fully enriched with trade-level data
**Trade history pulled:** 202,585 trades from 2026-04-24 → 2026-06-09 (his entire wallet history)

## Headline result

At **trigger=$5K, bot stake=$100, 2¢ slippage, 2% Kalshi fees**:

- **982 trades** over 6 weeks
- **60.5% win rate**
- **+$15,716 net PnL** on **$98,200** capital deployed
- **+16.0% ROI net**

At **trigger=$10K** (more selective):

- **388 trades**, **69.6% WR**, **+$12,654 net** on $38,800 → **+32.6% ROI net**

## Why I trust this number more than v1

The previous backtest only had data on 54 trades from a single 4-day window in late April — turns out that was actually his WORST week (he lost $25K on those positions). With his full wallet history pulled, the v2 sample is:

- **18× more trades** at the same threshold
- **6 weeks instead of 4 days**
- **All 3 months positive** in walk-forward

## Monthly walk-forward (no lookahead bias)

| Month | Trades @ $5K | WR | Bot PnL | His PnL |
|---|---|---|---|---|
| 2026-04 | 58 | 69.0% | +$2,111 | –$24,597 |
| 2026-05 | 664 | 58.9% | +$7,225 | +$479,893 |
| 2026-06 | 260 | 62.7% | +$6,380 | +$314,055 |
| **Cumulative** | **982** | **60.5%** | **+$15,716** | **+$769,350** |

Every month positive. No drawdown month.

## Win rate scales with trigger size (signal validation)

| Trigger | Trades | WR | Bot ROI @ 2¢ slip |
|---|---|---|---|
| $500 | 3,387 | 52.5% | +1.9% |
| $1,000 | 2,730 | 53.5% | +3.5% |
| $2,000 | 2,064 | 54.7% | +5.9% |
| $3,000 | 1,588 | 57.1% | +9.9% |
| $4,000 | 1,227 | 58.8% | +12.8% |
| **$5,000** | **982** | **60.5%** | **+16.0%** |
| $7,500 | 609 | 65.0% | +24.8% |
| **$10,000** | **388** | **69.6%** | **+32.6%** |
| $15,000 | 189 | 72.0% | +34.1% |
| $20,000 | 102 | 74.5% | +41.1% |

The monotonic relationship is the strongest evidence the edge is real. If his big bets were random, WR wouldn't climb consistently with stake size — bigger bets carry more conviction/info, and the outcomes confirm it.

## Slippage sensitivity

| Trigger | 0¢ ROI | 1¢ | 2¢ | 3¢ | 4¢ |
|---|---|---|---|---|---|
| $500 | +6.4% | +4.1% | +1.9% | -0.2% | -2.3% |
| $1K | +8.1% | +5.8% | +3.5% | +1.4% | -0.7% |
| $5K | +21.0% | +18.5% | +16.0% | +13.6% | +11.4% |
| $10K | +38.3% | +35.4% | +32.6% | +30.0% | +27.4% |
| $20K | +47.0% | +44.0% | +41.1% | +38.3% | +35.7% |

**Higher triggers are much more robust to slippage** — confirms what we want. We can pay 4¢ above his entry and still make money at $5K+.

## Why the bot beats him

His big positions show a striking pattern:
- When the market resolves his way (60–75%): he captures only ~$1K avg of a possible $15K
- When the market resolves against him (25–40%): he eats the full $4K+ loss

He sells winners early; holds losers to settlement. The bot does the opposite (fixed $100 stake, hold to settlement) and captures the EV he gives up. His underlying directional accuracy is high — it's his exit discipline that's bad.

## Recommended parameters

| Setting | Conservative | Aggressive |
|---|---|---|
| Trigger USD | $5,000 | $10,000 |
| Sample size | 982 trades / 6 weeks | 388 trades / 6 weeks |
| Expected WR | 60% | 70% |
| Expected ROI net | +13–18% | +28–35% |
| Required Kalshi liquidity | moderate | high (his $10K+ bets are usually high-vol markets) |

**My suggested start: $5K trigger.** Bigger sample, more triggers per day, lower variance. Step up to $10K only after live data confirms the model.

## Remaining caveats (read before risking money)

### 1. is_winner reliability
`is_winner` comes from `curPrice == 1` in Polymarket's positions API. For *resolved* markets this equals actual outcome. For *unresolved* markets currently trading near 1.0, this can be misleading. Since MLB games resolve same-day, this should only affect the last 1-2 days of data. To be safe, the sample's June 2026 month carries slightly more uncertainty than April/May.

### 2. Backtest entry price vs. live execution
Backtest uses HRH's `first_entry_price` + a slippage adjustment. The live bot fires when cumulative size crosses the threshold — by then he's been ladder-buying for some time and the price has moved. The 2–4¢ slippage band tries to model this; reality may be worse if he's a fast scaler.

### 3. Polymarket ≠ Kalshi prices
The backtest treats his Polymarket fill as if we'd execute at the same price on Kalshi. **We have not yet verified Kalshi price alignment** on these specific MLB games. The next step before live trading should be a Polymarket-vs-Kalshi spread study on recent MLB markets. If the cross-exchange spread is consistently >3¢, the strategy degrades significantly.

### 4. Game-start filter not yet validated
Backtest doesn't filter to pregame-only because we'd lose historical data (only 891 positions had `is_pregame` set). The live bot WILL apply this filter via game-start lookup. Since 99.8% of his timestamped positions were pregame, this likely matches the data we backtested on, but worth tracking in paper trading.

### 5. He could disappear or change behavior
Wallet history starts only April 24 — he's been active 6 weeks. Could be a one-trick run that fades. Strategy needs a drawdown stop (e.g., kill switch if 30-day rolling PnL goes negative).

## Next steps

1. **Build Polymarket-vs-Kalshi spread checker** — sample current MLB matchups, compare prices and liquidity
2. **Build the paper-trader** on top of Poly-Monitor — capture his real-time trade activity, simulate triggers, log what would have happened
3. **Run paper-trader for 1–2 weeks** to validate signal capture timing and Kalshi execution feasibility
4. **Wire to KalshiBot** for real order placement
5. **Deploy with drawdown stop**: kill switch if rolling 30-day PnL goes negative
