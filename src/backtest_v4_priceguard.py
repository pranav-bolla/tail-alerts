"""
Backtest v4 — snapshot bot WITH PRICE GUARD.

Strategy: same as v3 (snapshot polling, trigger on cumulative ≥ threshold),
but ADD a price guard: only enter if (current_price - his_first_fill) ≤ guard.

If the price has drifted too far during his buildup, the info is already
priced in — skip the trade. This filters out the right-tail outliers
that killed v3's EV.
"""
from __future__ import annotations

import json
import sys
import itertools
from datetime import datetime
from collections import defaultdict
from pathlib import Path

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "hrh"
SPORT = sys.argv[2] if len(sys.argv) > 2 else "mlb"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_data():
    with open(DATA_DIR / f"{PREFIX}_all_trades.json") as f:
        trades = json.load(f)
    with open(DATA_DIR / f"{PREFIX}_enriched_positions.json") as f:
        positions = {(p["condition_id"], p["outcome"]): p
                     for p in json.load(f)["positions"]}
    return trades, positions


def build_triggers(trades, positions, threshold, sport_filter):
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
            "trigger_ts": tlist[trigger_idx]["timestamp"],
            "his_stake": pos.get("total_stake", 0),
            "title": tlist[0].get("title"),
        })
    return triggers


def simulate(triggers, bot_stake, kalshi_slip, kalshi_fee_pct, price_guard):
    """price_guard = max acceptable drift in dollars (0.05 = 5c). None = no guard."""
    skipped = 0
    pnl_net = 0.0
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
            pnl_gross = shares - bot_stake
            fee = kalshi_fee_pct * bot_stake
            wins += 1
        else:
            pnl_gross = -bot_stake
            fee = 0.0
        pnl_net += pnl_gross - fee
        n += 1
    capital = bot_stake * n
    return {
        "trades": n,
        "skipped": skipped,
        "wins": wins,
        "win_rate": wins / n if n else 0,
        "pnl_net": pnl_net,
        "capital": capital,
        "roi_net_pct": 100 * pnl_net / capital if capital else 0,
    }


def main():
    print(f"Loading data for prefix={PREFIX!r} sport={SPORT!r}...")
    trades, positions = load_data()
    print(f"Loaded {len(trades):,} trades, {len(positions):,} positions\n")

    # Build triggers at each threshold once
    triggers_by_thresh = {}
    for thresh in [2000, 3000, 5000, 7500, 10000]:
        triggers_by_thresh[thresh] = build_triggers(trades, positions, thresh, SPORT)
        n = len(triggers_by_thresh[thresh])
        if n:
            avg_drift = sum(t["drift"] for t in triggers_by_thresh[thresh]) / n * 100
            print(f"  threshold=${thresh:>6,}: {n:>4} markets, mean drift {avg_drift:+.2f}c")

    print("\n" + "=" * 100)
    print(f"PRICE-GUARD SWEEP -- {PREFIX} / {SPORT}")
    print("Skip trade if (current_price - his_first_fill) > guard")
    print("=" * 100)

    print(f"\n{'thresh':>7} {'guard':>5} {'trades':>7} {'skipped':>8} "
          f"{'WR%':>5} {'PnL_net':>10} {'capital':>10} {'ROI_net%':>9}")
    print("-" * 90)

    for thresh in [2000, 3000, 5000, 7500, 10000]:
        trigs = triggers_by_thresh[thresh]
        for guard in [None, 0.10, 0.05, 0.03, 0.02, 0.01, 0.00]:
            guard_str = "none" if guard is None else f"{int(100*guard):>2}c"
            r = simulate(trigs, bot_stake=100, kalshi_slip=0.02,
                         kalshi_fee_pct=0.02, price_guard=guard)
            print(f"{thresh:>7,} {guard_str:>5} {r['trades']:>7} {r['skipped']:>8} "
                  f"{100*r['win_rate']:>5.1f} {r['pnl_net']:>+10,.0f} "
                  f"{r['capital']:>10,.0f} {r['roi_net_pct']:>+9.2f}")
        print()

    # Monthly walk-forward at the sweet spot
    print("=" * 100)
    print("MONTHLY WALK-FORWARD at threshold=$5K, price_guard=+3c, kalshi_slip=2c")
    print("=" * 100)
    trigs = triggers_by_thresh[5000]
    by_month = defaultdict(list)
    for tr in trigs:
        ym = datetime.fromtimestamp(tr["trigger_ts"]).strftime("%Y-%m")
        by_month[ym].append(tr)

    print(f"{'month':>10} {'n_seen':>7} {'taken':>6} {'skipped':>8} "
          f"{'WR%':>5} {'PnL_net':>10} {'cum_net':>10}")
    cumulative = 0
    for month in sorted(by_month):
        r = simulate(by_month[month], 100, 0.02, 0.02, price_guard=0.03)
        cumulative += r["pnl_net"]
        print(f"{month:>10} {len(by_month[month]):>7} {r['trades']:>6} "
              f"{r['skipped']:>8} {100*r['win_rate']:>5.1f} "
              f"{r['pnl_net']:>+10,.0f} {cumulative:>+10,.0f}")


if __name__ == "__main__":
    main()
