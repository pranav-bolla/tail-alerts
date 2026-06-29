"""
Apply the consensus-tail rule to per-sharp open positions.

Input: dict[wallet] -> list[position]  (from Polymarket /positions API)
Output: list[Signal] where each signal passes:

  - market is OPEN (cur_price in [0.05, 0.95])
  - sport in {nhl, mlb, soccer} (the +EV sports per backtest)
  - at least 2 sharps participating in the market
  - stake_weighted_conviction >= 85% on the consensus side
  - exec price (consensus_avg + 2c) is still in a sane range

Signal includes:
  - condition_id, outcome, title, sport
  - consensus_n, opposing_n, stake_conviction
  - consensus_avg_entry, max_acceptable_kalshi_price
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Iterable

# Per-backtest, only these sports have +EV consensus signal
SPORT_WHITELIST = {"mlb", "nhl", "soccer"}
SPORT_OPTIONAL = {"nfl"}  # also +7% but seasonal; off in summer

KALSHI_SLIP_CENTS = 2.0

PRICE_GUARD_CENTS = 2.0  # max premium we'll pay vs consensus avg

MIN_OPEN_PRICE = 0.05
MAX_OPEN_PRICE = 0.95
MIN_SHARPS = 2
MIN_STAKE_CONVICTION = 0.85
MIN_INDIVIDUAL_STAKE = 1000  # only count holders with >= $1K in this market


def derive_sport(slug: str) -> str | None:
    s = (slug or "").lower()
    if s.startswith("fifwc") or "world-cup" in s or "fifa" in s:
        return "soccer"
    for tag in ["mlb", "nba", "nhl", "wnba", "wimbledon", "french-open",
                "tennis", "ufc", "nfl"]:
        if tag in s:
            return "soccer" if tag in ("epl",) else tag
    if any(tag in s for tag in ["soccer", "epl", "premier", "champions-league"]):
        return "soccer"
    return None


@dataclass
class Signal:
    cid: str
    outcome: str
    title: str
    sport: str
    slug: str
    consensus_n: int
    opposing_n: int
    consensus_stake: float
    opposing_stake: float
    stake_conviction: float
    consensus_avg_entry: float
    consensus_max_entry: float
    cur_price: float
    max_kalshi_price: float
    game_start_time: str | None
    contributing_sharps: list[dict]
    opposing_sharps: list[dict]


def build_signals(positions_by_wallet: dict, sharp_meta: dict,
                  allow_optional_sports: bool = False) -> list[Signal]:
    """
    sharp_meta: wallet -> {name, week_pnl, month_pnl}
    positions_by_wallet: wallet -> list of positions
    """
    allowed_sports = SPORT_WHITELIST | (SPORT_OPTIONAL if allow_optional_sports else set())

    # Aggregate by (cid, outcome)
    holdings = defaultdict(list)
    for wallet, positions in positions_by_wallet.items():
        for p in positions:
            cid = p.get("conditionId")
            outcome = p.get("outcome")
            if not cid or not outcome:
                continue
            try:
                stake = float(p.get("totalBought", 0)) * float(p.get("avgPrice", 0))
            except (TypeError, ValueError):
                continue
            if stake < MIN_INDIVIDUAL_STAKE:
                continue
            slug = p.get("eventSlug") or p.get("slug") or ""
            sport = derive_sport(slug)
            if sport not in allowed_sports:
                continue
            try:
                cur = float(p.get("curPrice", 0) or 0)
            except (TypeError, ValueError):
                cur = 0
            if not (MIN_OPEN_PRICE <= cur <= MAX_OPEN_PRICE):
                continue
            try:
                avg = float(p.get("avgPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not (0 < avg < 1):
                continue
            holdings[(cid, outcome)].append({
                "wallet": wallet,
                "name": sharp_meta.get(wallet, {}).get("name", "?"),
                "stake": stake,
                "avg_price": avg,
                "cur_price": cur,
                "title": p.get("title"),
                "slug": slug,
                "sport": sport,
            })

    # For each market, group both outcomes together
    by_market = defaultdict(dict)
    for (cid, outcome), holders in holdings.items():
        by_market[cid][outcome] = holders

    signals = []
    for cid, by_outcome in by_market.items():
        # Identify consensus outcome (most sharps) and opposing
        sides = sorted(by_outcome.items(),
                       key=lambda kv: (len(kv[1]),
                                       sum(h["stake"] for h in kv[1])),
                       reverse=True)
        consensus_out, cons_holders = sides[0]
        if len(sides) >= 2:
            opp_out, opp_holders = sides[1]
        else:
            opp_out, opp_holders = None, []

        consensus_n = len(cons_holders)
        opposing_n = len(opp_holders)
        consensus_stake = sum(h["stake"] for h in cons_holders)
        opposing_stake = sum(h["stake"] for h in opp_holders)
        total_stake = consensus_stake + opposing_stake
        if total_stake == 0:
            continue
        stake_conviction = consensus_stake / total_stake

        if consensus_n < MIN_SHARPS:
            continue
        if stake_conviction < MIN_STAKE_CONVICTION:
            continue

        # Weighted avg entry
        avg_entry = (sum(h["avg_price"] * h["stake"] for h in cons_holders)
                     / consensus_stake)
        max_entry = max(h["avg_price"] for h in cons_holders)
        cur_avg = (sum(h["cur_price"] for h in cons_holders) / consensus_n)
        max_kalshi_price = min(0.97, avg_entry + PRICE_GUARD_CENTS / 100)

        signals.append(Signal(
            cid=cid,
            outcome=consensus_out,
            title=cons_holders[0]["title"] or "?",
            sport=cons_holders[0]["sport"],
            slug=cons_holders[0]["slug"],
            consensus_n=consensus_n,
            opposing_n=opposing_n,
            consensus_stake=consensus_stake,
            opposing_stake=opposing_stake,
            stake_conviction=stake_conviction,
            consensus_avg_entry=avg_entry,
            consensus_max_entry=max_entry,
            cur_price=cur_avg,
            max_kalshi_price=max_kalshi_price,
            game_start_time=None,
            contributing_sharps=[{
                "name": h["name"], "wallet": h["wallet"][:10] + "...",
                "stake": round(h["stake"], 0),
                "avg_price": round(h["avg_price"], 3),
            } for h in cons_holders],
            opposing_sharps=[{
                "name": h["name"], "wallet": h["wallet"][:10] + "...",
                "stake": round(h["stake"], 0),
                "avg_price": round(h["avg_price"], 3),
            } for h in opp_holders],
        ))

    signals.sort(key=lambda s: (s.stake_conviction, s.consensus_stake), reverse=True)
    return signals


def signal_to_dict(s: Signal) -> dict:
    return asdict(s)


if __name__ == "__main__":
    # Smoke test using the previous run's data
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "consensus_picks.json"
    if not p.exists():
        print("No consensus_picks.json — run consensus_tail.py first")
        raise SystemExit(1)
    with open(p) as f:
        data = json.load(f)
    # Reconstruct positions_by_wallet from the picks
    positions_by_wallet = defaultdict(list)
    sharp_meta = {}
    for pick in data["picks"]:
        for s in pick["sharps"]:
            sharp_meta[s.get("wallet", s.get("name"))] = {"name": s["name"]}
            positions_by_wallet[s.get("wallet", s.get("name"))].append({
                "conditionId": pick["condition_id"],
                "outcome": pick["outcome"],
                "totalBought": s["stake"] / max(s["avg_price"], 0.01),
                "avgPrice": s["avg_price"],
                "curPrice": s.get("cur_price", 0.5),
                "title": pick["title"],
                "eventSlug": pick["slug"],
            })
    signals = build_signals(positions_by_wallet, sharp_meta)
    print(f"\nGenerated {len(signals)} signals from {len(data['picks'])} picks")
    for s in signals[:10]:
        print(f"\n[{s.sport:>6}] {s.title[:50]} | {s.outcome}")
        print(f"  {s.consensus_n} vs {s.opposing_n} sharps, "
              f"stake_conv={s.stake_conviction:.1%}")
        print(f"  consensus_avg={s.consensus_avg_entry:.3f}, "
              f"cur={s.cur_price:.3f}, max_kalshi={s.max_kalshi_price:.3f}")
        print(f"  sharps: " + ", ".join(
            f"{h['name']}(${h['stake']:,.0f})" for h in s.contributing_sharps[:5]))
