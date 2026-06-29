# Strategy V2 — Revised Approach (June 2026)

## What went wrong in V1

### 1. swisstony dominates every signal (market maker, not directional)
Live data confirms swisstony holds **500 open positions** across **114 event slugs**, often with **both Yes and No** on the same market (e.g. Norway win Yes **and** No), plus draw/spread/total legs on the same game.

Our filter treats each binary market independently. swisstony's $20k "No" on Iraq win and $10k "No" on Norway win both pass as separate 100%-conviction signals — but his book is a **portfolio**, not a single directional bet.

**Verdict:** Do not count swisstony (and similar wallets) toward consensus until they pass directional screening.

### 2. Per-market conviction is wrong for soccer
Polymarket splits one match into 15+ markets:
- Will Iraq win? / Will Norway win? / Will it end in a draw?
- Spreads, totals, half-time props

Each can show "100% conviction" with 2 sharps on **No** — but that does **not** mean "bet Iraq" or "bet Norway." It often means **low-scoring / neither wins in 90** thesis spread across markets.

We fired **both** Norway Yes and Norway No as separate signals on the same `conditionId` — internally contradictory.

### 3. Pool collapse
Of 17–19 "active" sharps, only **5 ever appear** on signals. swisstony alone on **60/63** tracked signals. This is not the diverse 38-wallet backtest pool.

### 4. Stake threshold too low
$1k minimum per sharp → $2–3k total "consensus" from GamblingIsAllYouNeed + swisstony passes. Backtest had many signals with $50k–$900k stake; small signals had worse edge.

---

## What successful bots do (research synthesis)

Sources: [Polyburg conviction scoring](https://polyburg.com/polymarket-copy-trading), [Ratio whale signals](https://ratio.you/blog/polymarket-copy-trading-whale-signals), [PolySignal consensus](https://dev.to/__747bb5a1521/when-three-sharp-wallets-agree), [PolySyncer on-chain patterns](https://www.polysyncer.com/blog/polymarket-signals-guide), open arb/MM repos.

| Pattern | What works | What we were doing wrong |
|---------|-----------|-------------------------|
| **Independent cluster** | 3+ *uncorrelated* wallets same side, same market, 24h window | 2 wallets, often same two every time |
| **Conviction vs median** | Alert when bet size >> trader's historical median (7–10 score) | Raw stake $1k+ passes |
| **Directional filter** | Exclude MMs, hedgers, both-side holders | Count all open positions |
| **Event-level view** | One thesis per game, not 15 binary markets | Each binary market = separate signal |
| **Position adds** | "Whale added $50k to existing position" = high signal | Only snapshot open positions |
| **Latency tiers** | Auto for arb (<60s); manual OK for cluster entry (hours) | 10-min poll OK for manual |

**Realistic edge for manual Kalshi tailing:** Pattern 3/4/5 (cluster entry, book imbalance, resolution window) — not sub-minute arb.

---

## V2 Architecture — Three signal types

### A. Event thesis (primary — replaces naive consensus)

**Unit:** `event_slug` (one game), not `condition_id`.

1. Collect all open positions from **directional-only** sharps on that slug.
2. Map each position to canonical bucket:
   - **Soccer:** `team_a_win`, `team_b_win`, `draw`, `prop` (totals/spreads/H1)
   - **MLB/NHL:** `team_a_ml`, `team_b_ml`, `prop`
3. Sum stake per bucket (directional sharps only).
4. **Net thesis** = highest stake bucket with ≥2 independent sharps and ≥85% of *game-level* stake (not per-binary-market).
5. Emit **one signal per game** with human-readable thesis:
   - e.g. `"Iraq vs Norway: low-scoring / draw lean"` not three conflicting No signals.

**Mutual exclusion rules:**
- Never emit both Yes and No on same `conditionId`.
- Never emit team A win + team B win without explicit draw coverage flagged as **portfolio**.
- Suppress if top two buckets within 30% stake (split book).

### B. Conviction add (secondary — user asked for this)

Track `(wallet, conditionId)` stake each cycle.

Alert when:
- Stake increases ≥ **50%** OR ≥ **$5k** absolute since last snapshot
- Wallet passes directional filter
- Optional: increase vs wallet's 90-day median bet size (conviction score)

Email subject: `[tail-bot] ADD: swisstony +$24k on Dodgers ML`

### C. Directional cluster (strict consensus)

Only for **clean moneyline** markets (no spreads/totals in title).

Requirements:
- ≥ **3** sharps (not 2)
- ≥ **2** must have directional_rate ≥ **75%** (from tail-analysis tailability)
- No sharp holds opposite side of same cid
- Max **1** sharp from any highly-correlated pair (swisstony+RN1 count as 1.5 — see below)
- Total directional stake ≥ **$25k**
- Individual stake ≥ **$5k** per sharp

---

## Sharp pool V2

### Exclude / downweight
- **swisstony** — MM until proven directional on sport (run tailability on World Cup positions)
- Wallets with directional_rate < **70%**
- Wallets with median fills/position > **15**

### Correlation groups (count as partial independence)
If 2+ sharps from same cluster agree, count as **1.0 + 0.25 per extra** toward min-3 threshold:
- `{swisstony, RN1, GamblingIsAllYouNeed}` — often co-trade World Cup

### Re-screen weekly
Use existing `tailability_report.py` + `fresh_screen.py` on sports-only resolved positions.

---

## Email V2 (manual Kalshi)

**Type A — New event thesis:**
```
[MLB] TB @ LAD — Jun 15 10:10 PM ET
THESIS: Dodgers ML (3 directional sharps, $84k game stake)
Poly avg: $0.58 | max: $0.60
Sharps: RN1($12k), surfandturf($8k), ...
```

**Type B — Conviction add:**
```
ADD: RN1 +$15k on existing Dodgers ML (now $27k total)
```

No Kalshi ticker in email (per user preference).

---

## Implementation phases

1. **Phase 1 (now):** Stop bot ✓. Add `sharp_screener.py` — directional_rate, MM flags, exclude list.
2. **Phase 2:** `event_thesis.py` — group by slug, canonical buckets, one signal/game.
3. **Phase 3:** `position_delta.py` — stake snapshots, add alerts.
4. **Phase 4:** Re-backtest V2 rules on historical sharp_wallets cache.
5. **Phase 5:** Restart bot with `--mode signal-v2`.

---

## Immediate filters if restarting V1 temporarily

If you need something before V2 ships:
```python
EXCLUDE_WALLETS = {"swisstony"}  # until screened
MIN_SHARPS = 3
MIN_TOTAL_STAKE = 25_000
MIN_INDIVIDUAL_STAKE = 5_000
SKIP_TITLE_PREFIXES = ("Spread:", "O/U", "1st Half", "Both Teams")
DEDUP_OPPOSING_CID = True  # never signal both sides of same market
```

---

## Backtest ledger

All V1 signals preserved in `data/bot/backtest_ledger.jsonl` for post-mortem.
Run: `python -m bot.backtest_report` after games settle.
