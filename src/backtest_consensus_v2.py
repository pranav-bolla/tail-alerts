"""
Consensus backtest v2 — pool-size-invariant metrics.

Problem with v1: "net >= 2 sharps" depends on pool size. With 28 sharps, that's
7% of the pool. With 100 sharps it's 2%. The findings don't generalize.

v2 tests four pool-invariant convergence metrics:

  1. AGREEMENT RATIO: consensus_n / (consensus_n + opposing_n)
     - Pool-invariant. "75% of participants agree."

  2. STAKE-WEIGHTED CONVICTION: consensus_stake / total_stake
     - Heavy stakes matter more. "85% of capital on one side."

  3. MINIMUM PARTICIPATION FLOOR: only consider markets with >= N sharps total
     - In a 100-sharp pool, more sharps will trade any given market. To
       simulate this, we need a minimum-participation floor.

  4. COMBINATIONS: e.g., "agreement_ratio >= 80% AND >= 3 participants"

We then find the combination that maximizes ROI while keeping sample size
practically useful (>= 100 historical bets).
"""
from __future__ import annotations

import json
import glob
from collections import defaultdict
from pathlib import Path
from statistics import mean

CACHE_DIR = Path("/home/pranav/tail-analysis/data/sharp_wallets")
OUT_FILE = (Path(__file__).resolve().parent.parent /
            "results" / "consensus_backtest_v2.json")
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


def load_winning_sharps():
    sharps = {}
    for f in glob.glob(str(CACHE_DIR / "0x*.json")):
        if "_clv" in f:
            continue
        with open(f) as fh:
            d = json.load(fh)
        if d.get("overall", {}).get("realized_pnl", 0) <= 0:
            continue
        if len(d.get("positions", [])) < 50:
            continue
        sharps[d["wallet"]] = {
            "name": d.get("display_name", "?"),
            "positions": d["positions"],
            "all_time_pnl": d["overall"]["realized_pnl"],
            "all_time_roi": d["overall"]["roi"],
        }
    return sharps


def build_markets(sharps):
    """For each market, gather all winning-sharp holdings on each outcome."""
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
            if stake < 500:
                continue
            sport = derive_sport(p.get("event_slug") or p.get("market_slug"))
            if not sport:
                continue
            market_holders[cid][outcome].append({
                "wallet": wallet, "name": info["name"], "stake": stake,
                "is_winner": is_winner,
                "entry_price": (p.get("avg_buy_price")
                                or p.get("first_entry_price")),
            })
            market_meta[cid] = {
                "sport": sport,
                "slug": p.get("event_slug") or p.get("market_slug"),
                "title": p.get("title"),
            }
    return market_holders, market_meta


def build_market_features(market_holders, market_meta):
    """For each market, compute features + outcome."""
    rows = []
    for cid, by_outcome in market_holders.items():
        if cid not in market_meta:
            continue
        meta = market_meta[cid]

        # Determine consensus = side with more sharps; if tie, side with more
        # stake.
        outcomes = list(by_outcome.items())
        if not outcomes:
            continue
        # Sort by n_holders, then by total_stake
        outcomes.sort(key=lambda x: (len(x[1]), sum(h["stake"] for h in x[1])),
                      reverse=True)
        consensus_out, cons_holders = outcomes[0]
        if len(outcomes) >= 2:
            opp_out, opp_holders = outcomes[1]
        else:
            opp_out, opp_holders = None, []

        consensus_n = len(cons_holders)
        opposing_n = len(opp_holders)
        consensus_stake = sum(h["stake"] for h in cons_holders)
        opposing_stake = sum(h["stake"] for h in opp_holders)
        total_n = consensus_n + opposing_n
        total_stake = consensus_stake + opposing_stake

        if total_n == 0:
            continue
        # Skip ties
        if opposing_n == consensus_n and opposing_stake >= consensus_stake:
            continue

        consensus_won = any(h["is_winner"] for h in cons_holders)
        avg_entry = mean([h["entry_price"] for h in cons_holders
                          if h.get("entry_price") and 0 < h["entry_price"] < 1]
                         or [0])
        if avg_entry <= 0 or avg_entry >= 1:
            continue

        # Execution model
        exec_price = min(0.97, avg_entry + KALSHI_SLIP_CENTS / 100)
        shares = STAKE / exec_price
        if consensus_won:
            pnl_net = (shares - STAKE) * (1 - KALSHI_FEE_PCT)
        else:
            pnl_net = -STAKE

        rows.append({
            "cid": cid,
            "sport": meta["sport"],
            "title": meta.get("title"),
            "consensus_n": consensus_n,
            "opposing_n": opposing_n,
            "total_n": total_n,
            "agreement_ratio": consensus_n / total_n,
            "consensus_stake": consensus_stake,
            "opposing_stake": opposing_stake,
            "stake_conviction": (consensus_stake / total_stake
                                 if total_stake else 1.0),
            "avg_entry": avg_entry,
            "exec_price": exec_price,
            "won": consensus_won,
            "pnl_net": pnl_net,
        })
    return rows


def evaluate(rows, predicate, label):
    subset = [r for r in rows if predicate(r)]
    if not subset:
        return None
    n = len(subset)
    wr = sum(1 for r in subset if r["won"]) / n
    pnl_net = sum(r["pnl_net"] for r in subset)
    cap = n * STAKE
    roi = pnl_net / cap
    avg_e = mean(r["avg_entry"] for r in subset)
    return {
        "label": label, "n": n, "wr": wr, "pnl_net": pnl_net,
        "capital": cap, "roi_net": roi, "avg_entry": avg_e,
    }


def print_table(results, title):
    print("\n" + "=" * 105)
    print(title)
    print("=" * 105)
    print(f"  {'rule':>48} {'n':>6} {'WR%':>6} {'avg_entry':>10} "
          f"{'PnL_net':>10} {'ROI%':>8}")
    print("-" * 105)
    for r in results:
        if r is None:
            continue
        print(f"  {r['label'][:48]:>48} {r['n']:>6} "
              f"{100*r['wr']:>6.1f} {r['avg_entry']:>10.3f} "
              f"{r['pnl_net']:>+10,.0f} {100*r['roi_net']:>+8.2f}")


def main():
    print("Loading winning sharps...")
    sharps = load_winning_sharps()
    print(f"  {len(sharps)} winning sharps")

    market_holders, market_meta = build_markets(sharps)
    rows = build_market_features(market_holders, market_meta)
    print(f"  {len(rows):,} testable markets")

    # First, baseline: by total_n (how many sharps participated)
    results = []
    for floor in [1, 2, 3, 5, 8]:
        results.append(evaluate(rows, lambda r, f=floor: r["total_n"] >= f,
                                f"total_n >= {floor}"))
    print_table(results, "By minimum-participation floor (any agreement)")

    # Agreement-ratio buckets
    results = []
    for ratio in [0.55, 0.60, 0.66, 0.75, 0.85, 1.00]:
        for floor in [1, 2, 3, 5]:
            results.append(evaluate(rows,
                lambda r, ra=ratio, fl=floor:
                    r["total_n"] >= fl and r["agreement_ratio"] >= ra,
                f"agreement >= {int(ratio*100)}%, n_total >= {floor}"))
    print_table(results,
                "By agreement ratio × participation floor (pool-invariant)")

    # Stake-weighted conviction
    results = []
    for ratio in [0.55, 0.66, 0.75, 0.85, 0.95]:
        for floor in [1, 2, 3]:
            results.append(evaluate(rows,
                lambda r, ra=ratio, fl=floor:
                    r["total_n"] >= fl and r["stake_conviction"] >= ra,
                f"stake_conv >= {int(ratio*100)}%, n_total >= {floor}"))
    print_table(results,
                "By stake-weighted conviction × participation floor")

    # The killer combo: both
    results = []
    for ar, sc, fl in [
        (0.66, 0.66, 2), (0.66, 0.66, 3), (0.66, 0.66, 5),
        (0.75, 0.75, 2), (0.75, 0.75, 3), (0.75, 0.75, 5),
        (0.85, 0.85, 2), (0.85, 0.85, 3), (0.85, 0.85, 5),
        (1.00, 1.00, 2), (1.00, 1.00, 3), (1.00, 1.00, 5),
    ]:
        results.append(evaluate(rows,
            lambda r, a=ar, s=sc, f=fl:
                r["total_n"] >= f and r["agreement_ratio"] >= a
                and r["stake_conviction"] >= s,
            f"agree+stake >= {int(ar*100)}% @ n_total >= {fl}"))
    print_table(results, "Agreement AND stake-conviction (compound filter)")

    # By sport at the best filter
    print("\n" + "=" * 105)
    print("By sport at agreement >= 75% AND total_n >= 2")
    print("=" * 105)
    by_sport_rows = [r for r in rows
                     if r["total_n"] >= 2 and r["agreement_ratio"] >= 0.75]
    by_sport = defaultdict(list)
    for r in by_sport_rows:
        by_sport[r["sport"]].append(r)
    print(f"  {'sport':>8} {'n':>5} {'WR%':>6} {'avg_entry':>10} "
          f"{'PnL_net':>10} {'ROI%':>8}")
    print("-" * 105)
    for sport, subset in sorted(by_sport.items(), key=lambda x: -len(x[1])):
        n = len(subset)
        wr = sum(1 for r in subset if r["won"]) / n
        pnl_net = sum(r["pnl_net"] for r in subset)
        cap = n * STAKE
        avg_e = mean(r["avg_entry"] for r in subset)
        print(f"  {sport:>8} {n:>5} {100*wr:>6.1f} {avg_e:>10.3f} "
              f"{pnl_net:>+10,.0f} {100*pnl_net/cap:>+8.2f}")

    # Final: what does the SHARP-POOL SCALING look like?
    # In our 28-sharp pool, observe: at each total_n, what % of pool
    # participated? In a 100-sharp pool, equivalent thresholds.
    print("\n" + "=" * 105)
    print("SHARP POOL SCALING TABLE")
    print("=" * 105)
    pool = len(sharps)
    print(f"  Backtest pool: {pool} winning sharps")
    print(f"  Production pool (target): 100 winning sharps")
    print()
    print(f"  {'backtest_n':>11} {'%_pool':>8} {'equiv_n_100pool':>17} "
          f"{'markets':>9} {'WR%':>6} {'ROI%':>8}")
    print("-" * 105)
    for floor in [1, 2, 3, 5, 8]:
        equiv_100 = round(floor * 100 / pool, 1)
        subset = [r for r in rows if r["total_n"] >= floor]
        n = len(subset)
        wr = sum(1 for r in subset if r["won"]) / n if n else 0
        pnl_net = sum(r["pnl_net"] for r in subset)
        cap = n * STAKE
        roi = pnl_net / cap if cap else 0
        print(f"  {floor:>11} {100*floor/pool:>7.1f}% {equiv_100:>17} "
              f"{n:>9} {100*wr:>6.1f} {100*roi:>+8.2f}")

    print(f"\n  IMPORTANT: The 'agreement ratio' metric IS pool-invariant.")
    print(f"  Use that for production, regardless of pool size.")

    with open(OUT_FILE, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nWrote {len(rows):,} markets to {OUT_FILE}")


if __name__ == "__main__":
    main()
