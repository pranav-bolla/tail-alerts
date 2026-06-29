"""
Backtest v3 — snapshot-realistic entry price model.

Key change vs v2: the entry price is NOT his first_entry_price. It's the
price at the MOMENT cumulative size crosses the trigger threshold —
which models what a snapshot-polling bot would actually fill at.

This is a much more honest backtest because v1/v2 used his first fill
as the entry, but by the time cumulative crosses $5K he's been
ladder-buying for hours and the price has run.

We also model a configurable polling delay: after the threshold is
crossed, the bot doesn't see it until the next snapshot poll (uniform
random delay in [0, poll_interval_sec]). The execution price is then
the LAST price he traded at within that delay window (proxy for current
market price).
"""
from __future__ import annotations

import json
import itertools
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import sys

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "hrh"
SPORT_FILTER = sys.argv[2] if len(sys.argv) > 2 else "mlb"

TRADES_FILE = Path(__file__).resolve().parent.parent / "data" / f"{PREFIX}_all_trades.json"
POSITIONS_FILE = Path(__file__).resolve().parent.parent / "data" / f"{PREFIX}_enriched_positions.json"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def load_data():
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    with open(POSITIONS_FILE) as f:
        positions = {(p["condition_id"], p["outcome"]): p for p in json.load(f)["positions"]}
    return trades, positions


def build_mlb_triggers(
    trades: list[dict],
    positions: dict,
    threshold: float,
    poll_interval_sec: int = 0,
    seed: int = 42,
) -> list[dict]:
    """
    For each MLB (cid, outcome), find the moment cumulative buy size hits
    `threshold`. Return entry-price candidates simulating snapshot polling.
    """
    rng = random.Random(seed)
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        if (t.get("side") or "").upper() != "BUY":
            continue
        cid = t.get("conditionId")
        out = t.get("outcome")
        if not cid or not out:
            continue
        by_key[(cid, out)].append(t)

    triggers = []
    for (cid, outcome), tlist in by_key.items():
        slug = (tlist[0].get("eventSlug") or "").lower()
        if SPORT_FILTER and SPORT_FILTER not in slug:
            continue
        tlist.sort(key=lambda x: x.get("timestamp", 0))

        cum = 0.0
        trigger_idx = None
        for i, t in enumerate(tlist):
            cum += float(t.get("usdcSize", 0) or 0)
            if cum >= threshold:
                trigger_idx = i
                break
        if trigger_idx is None:
            continue

        trigger_trade = tlist[trigger_idx]
        trigger_ts = trigger_trade["timestamp"]
        trigger_price = float(trigger_trade.get("price", 0))
        if not (0 < trigger_price < 1):
            continue

        # Simulate the polling delay: the bot fires at trigger_ts + delay
        # where delay is uniform in [0, poll_interval_sec].
        # During that delay, more of HRH's trades happen; the bot fills
        # at roughly the LAST price within the delay window.
        if poll_interval_sec > 0:
            delay = rng.randint(0, poll_interval_sec)
        else:
            delay = 0
        exec_ts_max = trigger_ts + delay
        # Use the most recent BUY price <= exec_ts_max as a proxy for
        # current market price (this is what Kalshi would be tracking)
        exec_price = trigger_price
        for t in tlist[trigger_idx:]:
            if t["timestamp"] > exec_ts_max:
                break
            p = float(t.get("price", 0))
            if 0 < p < 1:
                exec_price = p

        pos = positions.get((cid, outcome))
        if pos is None or pos.get("is_winner") is None:
            continue

        triggers.append({
            "cid": cid,
            "outcome": outcome,
            "trigger_ts": trigger_ts,
            "first_entry_price": float(tlist[0].get("price", 0)),
            "trigger_price": trigger_price,
            "exec_price": exec_price,
            "his_total_stake": pos.get("total_stake", 0),
            "his_pnl": pos.get("realized_pnl", 0),
            "is_winner": bool(pos["is_winner"]),
            "title": tlist[0].get("title"),
        })

    return triggers


def simulate(
    triggers: list[dict],
    bot_stake: float,
    kalshi_slippage: float,
    kalshi_fee_pct: float,
) -> dict:
    """
    Run one combo. kalshi_slippage models the EXTRA spread on Kalshi
    vs Polymarket (i.e., Kalshi's best-ask above Polymarket's current
    price). exec_price already accounts for poll-delay drift.
    """
    pnl_net = 0.0
    wins = 0
    n = 0
    capital = 0.0
    for tr in triggers:
        entry = tr["exec_price"] + kalshi_slippage
        if entry <= 0 or entry >= 1:
            continue
        shares = bot_stake / entry
        if tr["is_winner"]:
            pnl_gross = shares - bot_stake
            fee = kalshi_fee_pct * bot_stake
            wins += 1
        else:
            pnl_gross = -bot_stake
            fee = 0.0
        pnl_net += pnl_gross - fee
        n += 1
        capital += bot_stake

    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n if n else 0,
        "pnl_net": pnl_net,
        "capital": capital,
        "roi_net_pct": 100 * pnl_net / capital if capital else 0,
    }


def main() -> None:
    print(f"Loading data for prefix={PREFIX!r} sport={SPORT_FILTER!r}...")
    trades, positions = load_data()
    print(f"Loaded {len(trades):,} trades and {len(positions):,} positions")

    print("\n" + "=" * 88)
    print(f"SNAPSHOT-REALISTIC BACKTEST -- {PREFIX} / {SPORT_FILTER}")
    print("Entry price = market price AT moment cumulative crosses threshold (not his first fill)")
    print("=" * 88)

    # Sweep threshold × kalshi_slippage at instant polling
    triggers_cache = {}
    for threshold in [2000, 3000, 5000, 7500, 10000, 15000]:
        triggers = build_mlb_triggers(trades, positions, threshold, poll_interval_sec=0)
        triggers_cache[threshold] = triggers

    print(f"\n--- Instant polling (no delay) ---")
    print(f"{'thresh':>7} {'trades':>7} {'WR%':>5} {'avg_entry':>10} "
          f"{'kalshi_slip':>11} {'PnL_net':>10} {'ROI_net%':>9}")
    print("-" * 80)
    for threshold in [2000, 3000, 5000, 7500, 10000, 15000]:
        trigs = triggers_cache[threshold]
        if not trigs:
            continue
        avg_entry = sum(t["exec_price"] for t in trigs) / len(trigs)
        for slip in [0.00, 0.02, 0.04]:
            r = simulate(trigs, bot_stake=100, kalshi_slippage=slip, kalshi_fee_pct=0.02)
            print(f"{threshold:>7,} {r['trades']:>7} {100*r['win_rate']:>5.1f} "
                  f"{avg_entry:>10.3f} {slip:>11.2f} {r['pnl_net']:>+10,.0f} "
                  f"{r['roi_net_pct']:>+9.2f}")

    # Now show polling-interval sensitivity at threshold=$5K
    print(f"\n--- Polling-interval sensitivity (threshold=$5,000, kalshi_slip=2c) ---")
    print(f"{'poll_min':>10} {'trades':>7} {'WR%':>5} {'avg_entry':>10} "
          f"{'PnL_net':>10} {'ROI_net%':>9}")
    print("-" * 70)
    for poll_min in [0, 5, 15, 30, 60]:
        trigs = build_mlb_triggers(trades, positions, 5000, poll_interval_sec=poll_min * 60)
        if not trigs:
            continue
        avg_entry = sum(t["exec_price"] for t in trigs) / len(trigs)
        r = simulate(trigs, bot_stake=100, kalshi_slippage=0.02, kalshi_fee_pct=0.02)
        print(f"{poll_min:>10} {r['trades']:>7} {100*r['win_rate']:>5.1f} "
              f"{avg_entry:>10.3f} {r['pnl_net']:>+10,.0f} {r['roi_net_pct']:>+9.2f}")

    # Comparison: v2 vs v3 at $5K
    print("\n--- v2 (first_entry_price) vs v3 (exec_price) at $5K ---")
    trigs = triggers_cache[5000]
    v2_avg = sum(t["first_entry_price"] for t in trigs) / len(trigs)
    v3_avg = sum(t["exec_price"] for t in trigs) / len(trigs)
    print(f"  v2 avg entry price (first fill):  {v2_avg:.4f}")
    print(f"  v3 avg entry price (at trigger):  {v3_avg:.4f}")
    print(f"  Spread:                           {100*(v3_avg-v2_avg):.2f}c per trade worse")

    # Monthly walk-forward
    print(f"\n--- Monthly walk-forward (threshold=$5K, instant poll, kalshi_slip=2c) ---")
    trigs = triggers_cache[5000]
    by_month: dict[str, list[dict]] = defaultdict(list)
    for tr in trigs:
        ym = datetime.fromtimestamp(tr["trigger_ts"]).strftime("%Y-%m")
        by_month[ym].append(tr)
    print(f"{'month':>10} {'n':>5} {'WR%':>5} {'PnL_net':>10} {'cum_net':>10}")
    cumulative = 0
    for month in sorted(by_month):
        cohort = by_month[month]
        r = simulate(cohort, 100, 0.02, 0.02)
        cumulative += r["pnl_net"]
        print(f"{month:>10} {r['trades']:>5} {100*r['win_rate']:>5.1f} "
              f"{r['pnl_net']:>+10,.0f} {cumulative:>+10,.0f}")


if __name__ == "__main__":
    main()
