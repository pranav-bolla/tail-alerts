"""
Backtest v7 — snapshot bot with RUNNING-AVERAGE PRICE GUARD.

User's question: "what if we set a safe boundary, only take his trades if we
are within two cents of his average price?"

Difference vs v4 guard:
  - v4 guard = price_at_trigger - first_entry_price  (drift from first fill)
  - v7 guard = price_at_trigger - running_weighted_avg  (distance from his avg)

The hypothesis: tailing his AVG price (not his first or his ladder-top fill)
gives the cleanest entry. If the market has moved away from his avg by more
than the guard, the trade is no longer near his fair-value anchor.

We also test BOTH directions of the guard:
  - asymmetric: only trade if current_price <= his_avg + guard  (don't pay up)
  - symmetric: only trade if |current_price - his_avg| <= guard (close to avg)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "hrh"
SPORT = sys.argv[2] if len(sys.argv) > 2 else "mlb"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

KALSHI_SLIP_CENTS = 2.0
KALSHI_FEE_PCT = 0.07  # 7% of winnings (conservative)
STAKE = 100.0


def load_data():
    with open(DATA_DIR / f"{PREFIX}_all_trades.json") as f:
        trades = json.load(f)
    with open(DATA_DIR / f"{PREFIX}_enriched_positions.json") as f:
        positions = {(p["condition_id"], p["outcome"]): p
                     for p in json.load(f)["positions"]}
    return trades, positions


def build_hedge_set(positions):
    cid_outcomes = defaultdict(set)
    for (cid, outcome), p in positions.items():
        if p.get("total_stake", 0) >= 100:
            cid_outcomes[cid].add(outcome)
    return {cid for cid, outs in cid_outcomes.items() if len(outs) >= 2}


def simulate(trades, positions, threshold, guard_cents, guard_mode,
             sport_filter, hedge_set):
    """
    guard_mode: 'none' = no guard
                'asymm' = skip if current > avg + guard (only pay at/below avg)
                'symm' = skip if |current - avg| > guard
    """
    # Group buys by (cid, outcome), sort by timestamp
    by_key = defaultdict(list)
    for t in trades:
        if (t.get("side") or "").upper() != "BUY":
            continue
        cid = t.get("conditionId")
        outcome = t.get("outcome")
        if not cid or not outcome:
            continue
        by_key[(cid, outcome)].append(t)

    triggers = []
    for (cid, outcome), tlist in by_key.items():
        slug = (tlist[0].get("eventSlug") or "").lower()
        if sport_filter and sport_filter not in slug:
            continue
        if cid in hedge_set:
            continue

        tlist.sort(key=lambda t: t.get("timestamp", 0))
        cumul_stake = 0.0
        cumul_shares = 0.0
        trigger_fired = False
        for trade in tlist:
            usd = float(trade.get("usdcSize", 0) or 0)
            shares = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            if not (usd > 0 and shares > 0 and price > 0):
                continue
            cumul_stake += usd
            cumul_shares += shares
            running_avg = cumul_stake / cumul_shares if cumul_shares else price

            if not trigger_fired and cumul_stake >= threshold:
                # Apply guard
                if guard_mode == 'asymm':
                    if price > running_avg + guard_cents / 100:
                        trigger_fired = True
                        continue  # skip (price too high vs avg)
                elif guard_mode == 'symm':
                    if abs(price - running_avg) > guard_cents / 100:
                        trigger_fired = True
                        continue  # skip
                triggers.append({
                    "cid": cid, "outcome": outcome,
                    "trigger_price": price, "running_avg": running_avg,
                    "first_price": float(tlist[0].get("price", 0) or 0),
                })
                trigger_fired = True

    # Simulate execution + PnL
    n_total = sum(1 for (cid, _), tlist in by_key.items()
                  if not (sport_filter and sport_filter not in (tlist[0].get("eventSlug") or "").lower())
                  and cid not in hedge_set
                  and sum(float(t.get("usdcSize", 0) or 0) for t in tlist) >= threshold)
    n_fired = len(triggers)
    n_skipped = n_total - n_fired

    wins = losses = pnl_gross = pnl_net = 0
    avg_drift_from_first = 0
    avg_drift_from_avg = 0
    for trg in triggers:
        key = (trg["cid"], trg["outcome"])
        pos = positions.get(key)
        if not pos:
            continue
        is_winner = pos.get("is_winner")
        if is_winner is None:
            continue

        exec_price = min(0.99, trg["trigger_price"] + KALSHI_SLIP_CENTS / 100)
        shares = STAKE / exec_price
        if is_winner:
            gross = shares - STAKE
            net = gross * (1 - KALSHI_FEE_PCT)
            wins += 1
        else:
            net = gross = -STAKE
            losses += 1
        pnl_gross += gross
        pnl_net += net
        avg_drift_from_first += trg["trigger_price"] - trg["first_price"]
        avg_drift_from_avg += trg["trigger_price"] - trg["running_avg"]

    resolved = wins + losses
    wr = wins / resolved if resolved else 0
    capital = resolved * STAKE
    roi_net = pnl_net / capital if capital else 0

    return {
        "n_fired": n_fired, "n_total": n_total, "n_skipped": n_skipped,
        "resolved": resolved, "wr": wr, "pnl_net": pnl_net,
        "roi_net": roi_net, "capital": capital,
        "drift_first": avg_drift_from_first / resolved * 100 if resolved else 0,
        "drift_avg": avg_drift_from_avg / resolved * 100 if resolved else 0,
    }


def main():
    print(f"Loading data for prefix='{PREFIX}' sport='{SPORT}'...")
    trades, positions = load_data()
    print(f"Loaded {len(trades):,} trades, {len(positions):,} positions")

    hedge_set = build_hedge_set(positions)
    cid_outcomes = defaultdict(set)
    for (cid, outcome), p in positions.items():
        if p.get("total_stake", 0) >= 100:
            cid_outcomes[cid].add(outcome)
    print(f"Hedged markets: {len(hedge_set)} of {len(cid_outcomes)} "
          f"({100*len(hedge_set)/max(1, len(cid_outcomes)):.1f}%)")

    print("\n" + "=" * 110)
    print(f"V7: RUNNING-AVG PRICE GUARD -- {PREFIX} / {SPORT}")
    print(f"  Trigger when cumulative stake >= threshold")
    print(f"  Skip if entry price exceeds his running weighted-avg by guard amount")
    print("=" * 110)
    print(f"\n{'thresh':>7} {'guard':>10} {'fired':>6} {'skip':>5} {'res':>5} "
          f"{'WR%':>5} {'PnL_net':>10} {'ROI%':>7} {'drift_avg':>10}")
    print('-' * 110)

    rows = []
    for threshold in [1000, 2000, 3000, 5000, 7500, 10000]:
        for guard_mode, guard_cents in [
            ('none', 0),
            ('asymm', 5),
            ('asymm', 3),
            ('asymm', 2),
            ('asymm', 1),
            ('asymm', 0.5),
            ('symm', 2),
        ]:
            r = simulate(trades, positions, threshold, guard_cents, guard_mode,
                         SPORT, hedge_set)
            guard_label = (f"{guard_mode}_{guard_cents}c"
                          if guard_mode != 'none' else 'none')
            print(f"{threshold:>7,} {guard_label:>10} {r['n_fired']:>6} "
                  f"{r['n_skipped']:>5} {r['resolved']:>5} "
                  f"{100*r['wr']:>5.1f} {r['pnl_net']:>+10,.0f} "
                  f"{100*r['roi_net']:>+7.2f} {r['drift_avg']:>+10.2f}c")
            rows.append({'threshold': threshold, 'guard': guard_label, **r})
        print()

    # Save
    with open(DATA_DIR.parent / "results" / f"backtest_v7_{PREFIX}_{SPORT}.json", "w") as f:
        json.dump(rows, f, indent=2)


if __name__ == "__main__":
    main()
