"""
Backtest v5 — Architecture B (per-trade streaming).

Strategy: bot watches every BUY trade in real-time. Fire on the FIRST
individual buy ≥ $X in a (conditionId, outcome) market. Enter at that
trade's price + small slippage.

This is fundamentally different from cumulative-threshold backtests because:
  - Selectivity = individual trade size, not cumulative
  - Entry price = price of his trigger trade (close to fair value at that
    moment — no laddering drift yet)
  - Some triggers fire on positions that NEVER grow big (low conviction)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "hrh"
SPORT = sys.argv[2] if len(sys.argv) > 2 else "mlb"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_data():
    with open(DATA_DIR / f"{PREFIX}_all_trades.json") as f:
        trades = json.load(f)
    with open(DATA_DIR / f"{PREFIX}_enriched_positions.json") as f:
        positions = {(p["condition_id"], p["outcome"]): p
                     for p in json.load(f)["positions"]}
    return trades, positions


def build_triggers(trades, positions, individual_threshold, sport_filter):
    """Fire on first BUY where individual size >= threshold."""
    by_key = defaultdict(list)
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
        if sport_filter and sport_filter not in slug:
            continue
        tlist.sort(key=lambda x: x.get("timestamp", 0))

        # Find first buy with usd >= threshold
        trigger_trade = None
        cum_before = 0.0
        for t in tlist:
            usd = float(t.get("usdcSize", 0) or 0)
            if usd >= individual_threshold:
                trigger_trade = t
                break
            cum_before += usd
        if trigger_trade is None:
            continue

        entry_price = float(trigger_trade.get("price", 0))
        if not (0 < entry_price < 1):
            continue

        pos = positions.get((cid, outcome))
        if pos is None or pos.get("is_winner") is None:
            continue

        # Compute final cumulative for context
        final_cum = sum(float(t.get("usdcSize", 0) or 0) for t in tlist)

        triggers.append({
            "entry_price": entry_price,
            "trigger_usd": float(trigger_trade.get("usdcSize", 0)),
            "cum_before_trigger": cum_before,
            "final_cumulative": final_cum,
            "is_winner": bool(pos["is_winner"]),
            "his_total_stake": pos.get("total_stake", 0),
            "his_pnl": pos.get("realized_pnl", 0),
            "trigger_ts": trigger_trade["timestamp"],
            "title": tlist[0].get("title"),
        })
    return triggers


def simulate(triggers, bot_stake, kalshi_slip, kalshi_fee_pct):
    pnl = 0.0
    wins = 0
    for tr in triggers:
        entry = tr["entry_price"] + kalshi_slip
        if entry <= 0 or entry >= 1:
            continue
        shares = bot_stake / entry
        if tr["is_winner"]:
            pnl_g = shares - bot_stake
            fee = kalshi_fee_pct * bot_stake
            wins += 1
        else:
            pnl_g = -bot_stake
            fee = 0.0
        pnl += pnl_g - fee
    n = len(triggers)
    cap = bot_stake * n
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n if n else 0,
        "pnl_net": pnl,
        "capital": cap,
        "roi_net_pct": 100 * pnl / cap if cap else 0,
    }


def main():
    print(f"Loading data for prefix={PREFIX!r} sport={SPORT!r}...")
    trades, positions = load_data()
    print(f"Loaded {len(trades):,} trades, {len(positions):,} positions\n")

    print("=" * 100)
    print(f"ARCHITECTURE B BACKTEST -- {PREFIX} / {SPORT}")
    print("Fire on first individual BUY ≥ threshold. Entry = that trade's price.")
    print("=" * 100)

    print(f"\n{'indiv_thresh':>12} {'kalshi_slip':>11} {'trades':>7} {'WR%':>5} "
          f"{'avg_entry':>10} {'PnL_net':>10} {'capital':>10} {'ROI_net%':>9}")
    print("-" * 95)

    triggers_cache = {}
    for indiv in [500, 1000, 1500, 2000, 3000, 5000]:
        trigs = build_triggers(trades, positions, indiv, SPORT)
        triggers_cache[indiv] = trigs
        avg_entry = sum(t["entry_price"] for t in trigs) / max(1, len(trigs))
        for slip in [0.00, 0.02, 0.04]:
            r = simulate(trigs, bot_stake=100, kalshi_slip=slip, kalshi_fee_pct=0.02)
            print(f"${indiv:>11,} {slip:>11.2f} {r['trades']:>7} {100*r['win_rate']:>5.1f} "
                  f"{avg_entry:>10.3f} {r['pnl_net']:>+10,.0f} {r['capital']:>10,.0f} "
                  f"{r['roi_net_pct']:>+9.2f}")
        print()

    # Walk-forward at best config
    print("=" * 100)
    best_indiv = 2000
    print(f"MONTHLY WALK-FORWARD at indiv_threshold=${best_indiv:,}, kalshi_slip=2c")
    print("=" * 100)
    trigs = triggers_cache[best_indiv]
    by_month = defaultdict(list)
    for tr in trigs:
        ym = datetime.fromtimestamp(tr["trigger_ts"]).strftime("%Y-%m")
        by_month[ym].append(tr)
    print(f"{'month':>10} {'trades':>7} {'WR%':>5} {'PnL_net':>10} {'cum_net':>10}")
    cum = 0
    for month in sorted(by_month):
        r = simulate(by_month[month], 100, 0.02, 0.02)
        cum += r["pnl_net"]
        print(f"{month:>10} {r['trades']:>7} {100*r['win_rate']:>5.1f} "
              f"{r['pnl_net']:>+10,.0f} {cum:>+10,.0f}")

    # Also: how does the cohort relate to "did it eventually become a big position"?
    print(f"\n=== Of triggers at ${best_indiv} individual threshold... ===")
    trigs = triggers_cache[best_indiv]
    grew_to_5k = [t for t in trigs if t["final_cumulative"] >= 5000]
    grew_to_10k = [t for t in trigs if t["final_cumulative"] >= 10000]
    stayed_small = [t for t in trigs if t["final_cumulative"] < 5000]
    print(f"  Total: {len(trigs)}")
    print(f"  Eventually reached $5K cumulative: {len(grew_to_5k)}")
    print(f"  Eventually reached $10K cumulative: {len(grew_to_10k)}")
    print(f"  Stayed under $5K: {len(stayed_small)}")
    if stayed_small:
        wr_small = sum(1 for t in stayed_small if t["is_winner"]) / len(stayed_small)
        print(f"    WR on stayed-small: {100*wr_small:.1f}%")
    if grew_to_5k:
        wr_big = sum(1 for t in grew_to_5k if t["is_winner"]) / len(grew_to_5k)
        print(f"    WR on grew-to-5K: {100*wr_big:.1f}%")
    if grew_to_10k:
        wr_huge = sum(1 for t in grew_to_10k if t["is_winner"]) / len(grew_to_10k)
        print(f"    WR on grew-to-10K: {100*wr_huge:.1f}%")


if __name__ == "__main__":
    main()
