"""
Backtest the consensus strategy on historical resolved sport markets.

Method:
  1. Load all cached sharp wallets (from tail-analysis/data/sharp_wallets)
  2. Filter to wallets with positive all-time ROI (true winners)
  3. For each historical RESOLVED sport market:
       - Count how many sharps held the YES outcome
       - Count how many held NO
       - Net = yes_count - no_count (positive = consensus on YES)
       - The "consensus side" is the side with more sharps
       - Did consensus side win the market?
  4. Bucket by (net_sharps, sport) → win rate + ROI + EV
  5. Apply Kalshi execution model: enter at consensus avg + 2c, hold to settle

This gives us empirical EV per consensus tier.
"""
from __future__ import annotations

import json
import glob
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

CACHE_DIR = Path("/home/pranav/tail-analysis/data/sharp_wallets")
OUT_FILE = Path(__file__).resolve().parent.parent / "results" / "consensus_backtest.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

KALSHI_SLIP_CENTS = 2.0
KALSHI_FEE_PCT = 0.07
STAKE = 100.0

SPORTS = {"mlb": ["mlb"], "nba": ["nba"], "nhl": ["nhl"],
          "soccer": ["soccer", "epl", "fifwc", "world-cup", "premier"],
          "tennis": ["tennis", "wimbledon", "french-open"],
          "ufc": ["ufc"], "wnba": ["wnba"], "nfl": ["nfl"]}


def derive_sport(slug: str) -> str | None:
    s = (slug or "").lower()
    if s.startswith("fifwc") or "world-cup" in s:
        return "soccer"
    for sport, tags in SPORTS.items():
        for tag in tags:
            if tag in s:
                return sport
    return None


def load_winning_sharps() -> dict:
    """Return wallet → list of resolved positions for winning sharps only."""
    sharps = {}
    for f in glob.glob(str(CACHE_DIR / "0x*.json")):
        if "_clv" in f:
            continue
        with open(f) as fh:
            d = json.load(fh)
        if d.get("overall", {}).get("realized_pnl", 0) <= 0:
            continue  # only winners
        # Need >= 50 closed positions for stable signal
        if len(d.get("positions", [])) < 50:
            continue
        sharps[d["wallet"]] = {
            "name": d.get("display_name", "?"),
            "positions": d["positions"],
            "all_time_pnl": d["overall"]["realized_pnl"],
            "all_time_roi": d["overall"]["roi"],
        }
    return sharps


def main():
    print("Loading winning sharps from analyzer cache...")
    sharps = load_winning_sharps()
    print(f"  Loaded {len(sharps)} winning sharps with >=50 closed positions")

    # Aggregate: market → (outcome → list of sharps holding it)
    market_holders = defaultdict(lambda: defaultdict(list))
    market_meta = {}

    for wallet, info in sharps.items():
        for p in info["positions"]:
            cid = p.get("condition_id")
            outcome = p.get("outcome")
            is_winner = p.get("is_winner")
            if not cid or not outcome or is_winner is None:
                continue
            stake = p.get("total_stake", 0)
            if stake < 500:  # skip noise
                continue
            sport = derive_sport(p.get("event_slug") or p.get("market_slug"))
            if not sport:
                continue
            market_holders[cid][outcome].append({
                "wallet": wallet,
                "name": info["name"],
                "stake": stake,
                "is_winner": is_winner,
                "entry_price": p.get("avg_buy_price") or p.get("first_entry_price"),
                "realized_pnl": p.get("realized_pnl", 0),
            })
            market_meta[cid] = {
                "sport": sport,
                "slug": p.get("event_slug") or p.get("market_slug"),
                "title": p.get("title"),
            }

    print(f"  Aggregated {len(market_holders):,} unique sport markets")

    # Per-market analysis
    rows = []
    for cid, by_outcome in market_holders.items():
        if cid not in market_meta:
            continue
        meta = market_meta[cid]

        # Determine the consensus outcome (most sharps holding) and the
        # outcome that actually won (any holder with is_winner=True on that side)
        outcomes_list = list(by_outcome.items())
        if len(outcomes_list) == 0:
            continue

        # Sharp count per outcome
        outcome_n = {out: len(holders) for out, holders in by_outcome.items()}
        outcome_stake = {out: sum(h["stake"] for h in holders)
                         for out, holders in by_outcome.items()}
        outcome_won = {out: any(h["is_winner"] for h in holders)
                       for out, holders in by_outcome.items()}

        if len(outcomes_list) == 1:
            # Pure consensus - all sharps on same side
            out_name = outcomes_list[0][0]
            consensus_out = out_name
            consensus_n = outcome_n[out_name]
            opposing_n = 0
            consensus_won = outcome_won[out_name]
        else:
            # Compare two sides
            sorted_outs = sorted(outcomes_list, key=lambda x: len(x[1]), reverse=True)
            consensus_out, consensus_holders = sorted_outs[0]
            opposing_out, opposing_holders = sorted_outs[1]
            consensus_n = outcome_n[consensus_out]
            opposing_n = outcome_n[opposing_out]
            if consensus_n == opposing_n:
                continue  # tie, skip
            consensus_won = outcome_won[consensus_out]

        net = consensus_n - opposing_n
        consensus_holders = by_outcome[consensus_out]

        avg_entry = mean([h["entry_price"] for h in consensus_holders
                          if h.get("entry_price")] or [0])
        if avg_entry <= 0 or avg_entry >= 1:
            continue

        # Execution: buy at min(avg + slip, 0.97)
        exec_price = min(0.97, avg_entry + KALSHI_SLIP_CENTS / 100)
        shares = STAKE / exec_price
        if consensus_won:
            pnl_gross = shares - STAKE
            pnl_net = pnl_gross * (1 - KALSHI_FEE_PCT)
        else:
            pnl_gross = -STAKE
            pnl_net = -STAKE

        rows.append({
            "cid": cid,
            "sport": meta["sport"],
            "title": meta["title"],
            "consensus_outcome": consensus_out,
            "consensus_n": consensus_n,
            "opposing_n": opposing_n,
            "net": net,
            "avg_entry": avg_entry,
            "exec_price": exec_price,
            "won": consensus_won,
            "pnl_gross": pnl_gross,
            "pnl_net": pnl_net,
            "total_sharp_stake": sum(outcome_stake.values()),
        })

    print(f"\n  {len(rows):,} backtestable consensus markets")

    # Bucket by net sharp count
    print("\n" + "=" * 105)
    print("CONSENSUS BACKTEST -- by net sharp count (any sport)")
    print("=" * 105)
    print(f"  {'tier':>14} {'markets':>8} {'WR%':>6} {'avg_entry':>10} "
          f"{'PnL_gross':>10} {'PnL_net':>10} {'capital':>10} {'ROI_net%':>9}")
    print("-" * 105)

    buckets = [
        ("net >= 1", lambda r: r["net"] >= 1),
        ("net >= 2", lambda r: r["net"] >= 2),
        ("net >= 3", lambda r: r["net"] >= 3),
        ("net >= 5", lambda r: r["net"] >= 5),
        ("net >= 8", lambda r: r["net"] >= 8),
        ("net >= 12", lambda r: r["net"] >= 12),
        ("pure (no opp)", lambda r: r["opposing_n"] == 0 and r["consensus_n"] >= 2),
        ("pure (>=3)", lambda r: r["opposing_n"] == 0 and r["consensus_n"] >= 3),
        ("pure (>=5)", lambda r: r["opposing_n"] == 0 and r["consensus_n"] >= 5),
    ]

    for name, filt in buckets:
        subset = [r for r in rows if filt(r)]
        if not subset:
            continue
        n = len(subset)
        wr = sum(1 for r in subset if r["won"]) / n
        pnl_g = sum(r["pnl_gross"] for r in subset)
        pnl_n = sum(r["pnl_net"] for r in subset)
        cap = n * STAKE
        roi = pnl_n / cap
        avg_e = mean(r["avg_entry"] for r in subset)
        print(f"  {name:>14} {n:>8} {100*wr:>6.1f} {avg_e:>10.3f} "
              f"{pnl_g:>+10,.0f} {pnl_n:>+10,.0f} {cap:>10,.0f} {100*roi:>+9.2f}")

    # Bucket by sport
    print("\n" + "=" * 105)
    print("CONSENSUS BACKTEST -- by sport (net >= 2)")
    print("=" * 105)
    print(f"  {'sport':>8} {'markets':>8} {'WR%':>6} {'avg_entry':>10} "
          f"{'PnL_net':>10} {'ROI_net%':>9}")
    print("-" * 105)
    by_sport = defaultdict(list)
    for r in rows:
        if r["net"] >= 2:
            by_sport[r["sport"]].append(r)
    for sport, subset in sorted(by_sport.items(), key=lambda x: -len(x[1])):
        n = len(subset)
        wr = sum(1 for r in subset if r["won"]) / n
        pnl_n = sum(r["pnl_net"] for r in subset)
        cap = n * STAKE
        roi = pnl_n / cap
        avg_e = mean(r["avg_entry"] for r in subset)
        print(f"  {sport:>8} {n:>8} {100*wr:>6.1f} {avg_e:>10.3f} "
              f"{pnl_n:>+10,.0f} {100*roi:>+9.2f}")

    # Distribution of payouts at net >= 3
    print("\n" + "=" * 105)
    print("DISTRIBUTION OF PAYOUTS at net >= 3 (the hot zone)")
    print("=" * 105)
    subset = [r for r in rows if r["net"] >= 3]
    if subset:
        pnls = sorted([r["pnl_net"] for r in subset])
        n = len(pnls)
        print(f"  total: {n} bets, sum_pnl_net = ${sum(pnls):+,.0f}")
        print(f"  min loss: ${pnls[0]:+,.0f}")
        print(f"  median:   ${pnls[n//2]:+,.0f}")
        print(f"  p90 win:  ${pnls[int(0.9*n)]:+,.0f}")
        print(f"  max win:  ${pnls[-1]:+,.0f}")
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        print(f"  wins: {len(wins)} avg=${mean(wins):+,.2f}")
        print(f"  losses: {len(losses)} avg=${mean(losses):+,.2f}" if losses else "  no losses")

    # Save raw
    with open(OUT_FILE, "w") as f:
        json.dump({
            "n_sharps": len(sharps),
            "n_markets": len(rows),
            "trades": rows,
        }, f, indent=2)
    print(f"\nWrote raw trades to {OUT_FILE}")


if __name__ == "__main__":
    main()
