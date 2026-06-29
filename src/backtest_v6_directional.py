"""
Backtest v6 — snapshot bot with HEDGE FILTER.

Key insight: HRH (and similar high-volume wallets) operate in two modes:
  - Market-making: hedged on both sides of a market (~50% WR)
  - Directional: one-sided big bets (~69% WR — the real edge)

Filter triggers to only include markets where he ONLY holds one outcome.
Skip any market where he's already holding the opposite side (hedge).

Combined with snapshot polling + price guard + realistic entry pricing.
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
ANALYZER_FILE = None  # to be set in main


def load_data():
    with open(DATA_DIR / f"{PREFIX}_all_trades.json") as f:
        trades = json.load(f)
    with open(DATA_DIR / f"{PREFIX}_enriched_positions.json") as f:
        positions = {(p["condition_id"], p["outcome"]): p
                     for p in json.load(f)["positions"]}
    return trades, positions


def build_hedge_set(positions):
    """Return set of conditionIds where the trader held BOTH outcomes."""
    cid_outcomes = defaultdict(set)
    for (cid, outcome), p in positions.items():
        if p.get("total_stake", 0) >= 100:  # only count meaningful positions
            cid_outcomes[cid].add(outcome)
    return {cid for cid, outs in cid_outcomes.items() if len(outs) >= 2}


def build_triggers(trades, positions, threshold, sport_filter, hedge_set):
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
        if cid in hedge_set:
            continue  # SKIP HEDGED MARKETS
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

        first_price = float(tlist[0].get("price", 0))
        trigger_price = float(tlist[trigger_idx].get("price", 0))
        if not (0 < first_price < 1) or not (0 < trigger_price < 1):
            continue

        pos = positions.get((cid, outcome))
        if pos is None or pos.get("is_winner") is None:
            continue

        triggers.append({
            "first_price": first_price,
            "trigger_price": trigger_price,
            "drift": trigger_price - first_price,
            "is_winner": bool(pos["is_winner"]),
            "his_stake": pos.get("total_stake", 0),
            "his_pnl": pos.get("realized_pnl", 0),
            "trigger_ts": tlist[trigger_idx]["timestamp"],
            "title": tlist[0].get("title"),
        })
    return triggers


def simulate(triggers, bot_stake, kalshi_slip, kalshi_fee_pct, price_guard):
    skipped = 0
    pnl = 0.0
    wins = 0
    n = 0
    for tr in triggers:
        if price_guard is not None and tr["drift"] > price_guard:
            skipped += 1
            continue
        entry = tr["trigger_price"] + kalshi_slip
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
        n += 1
    capital = bot_stake * n
    return {
        "trades": n,
        "skipped": skipped,
        "wins": wins,
        "win_rate": wins / n if n else 0,
        "pnl_net": pnl,
        "capital": capital,
        "roi_net_pct": 100 * pnl / capital if capital else 0,
    }


def main():
    print(f"Loading data for prefix={PREFIX!r} sport={SPORT!r}...")
    trades, positions = load_data()
    print(f"Loaded {len(trades):,} trades, {len(positions):,} positions")

    hedge_set = build_hedge_set(positions)
    print(f"Detected {len(hedge_set):,} hedged markets (where both outcomes held ≥$100)")
    total_cids = len({cid for (cid, _) in positions.keys()})
    print(f"Out of {total_cids:,} total markets ({100*len(hedge_set)/total_cids:.1f}% hedged)")

    print("\n" + "=" * 100)
    print(f"DIRECTIONAL-ONLY BACKTEST -- {PREFIX} / {SPORT}")
    print("Skips markets where trader also held the opposite outcome (hedge legs)")
    print("=" * 100)

    print(f"\n{'thresh':>7} {'guard':>5} {'trades':>7} {'skipped':>8} "
          f"{'WR%':>5} {'PnL_net':>10} {'capital':>10} {'ROI_net%':>9}")
    print("-" * 90)

    triggers_cache = {}
    for thresh in [1000, 2000, 3000, 5000, 7500, 10000]:
        trigs = build_triggers(trades, positions, thresh, SPORT, hedge_set)
        triggers_cache[thresh] = trigs
        if trigs:
            avg_drift = sum(t["drift"] for t in trigs) / len(trigs) * 100
            avg_wr = sum(1 for t in trigs if t["is_winner"]) / len(trigs) * 100
            print(f"\n  >>> threshold=${thresh:>6,}: {len(trigs):>4} markets, "
                  f"naive WR {avg_wr:.1f}%, mean drift {avg_drift:+.2f}c")
        for guard in [None, 0.05, 0.03, 0.02, 0.01, 0.00]:
            guard_str = "none" if guard is None else f"{int(100*guard):>2}c"
            r = simulate(trigs, bot_stake=100, kalshi_slip=0.02,
                         kalshi_fee_pct=0.02, price_guard=guard)
            print(f"{thresh:>7,} {guard_str:>5} {r['trades']:>7} {r['skipped']:>8} "
                  f"{100*r['win_rate']:>5.1f} {r['pnl_net']:>+10,.0f} "
                  f"{r['capital']:>10,.0f} {r['roi_net_pct']:>+9.2f}")

    # Walk-forward at sweet spot
    print("\n" + "=" * 100)
    print("MONTHLY WALK-FORWARD at threshold=$5K, price_guard=none, kalshi_slip=2c")
    print("=" * 100)
    trigs = triggers_cache[5000]
    by_month = defaultdict(list)
    for tr in trigs:
        ym = datetime.fromtimestamp(tr["trigger_ts"]).strftime("%Y-%m")
        by_month[ym].append(tr)
    print(f"{'month':>10} {'trades':>7} {'WR%':>5} {'PnL_net':>10} {'cum_net':>10}")
    cum = 0
    for month in sorted(by_month):
        r = simulate(by_month[month], 100, 0.02, 0.02, price_guard=None)
        cum += r["pnl_net"]
        print(f"{month:>10} {r['trades']:>7} {100*r['win_rate']:>5.1f} "
              f"{r['pnl_net']:>+10,.0f} {cum:>+10,.0f}")


if __name__ == "__main__":
    main()
