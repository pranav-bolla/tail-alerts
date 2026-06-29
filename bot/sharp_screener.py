"""
Screen sharps from open positions for copy-trading eligibility.

Flags market makers (both-side holders, low directional rate, huge books)
and assigns correlation clusters for independence scoring.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict

from bot.signal_filter import derive_sport

MIN_DIRECTIONAL_RATE = 0.70
MAX_OPEN_SPORTS_POSITIONS = 120  # MM-style book size
MIN_HEDGE_STAKE = 500  # $ stake to count as meaningful hedge

# Wallets in same cluster are partially correlated (not independent votes).
CORRELATED_CLUSTERS: list[frozenset[str]] = [
    frozenset({"swisstony", "RN1", "GamblingIsAllYouNeed"}),
]

# Hard exclude until re-screened (case-insensitive name match).
HARD_EXCLUDE_NAMES: frozenset[str] = frozenset()


def _stake(p: dict) -> float:
    try:
        return float(p.get("totalBought", 0) or 0) * float(p.get("avgPrice", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


def cluster_for_name(name: str) -> frozenset[str] | None:
    n = _norm_name(name)
    for cluster in CORRELATED_CLUSTERS:
        if n in {c.lower() for c in cluster}:
            return cluster
    return None


def effective_independence_count(names: list[str]) -> float:
    """1.0 for first wallet in a cluster, +0.25 for each additional from same cluster."""
    seen_clusters: dict[frozenset, int] = {}
    score = 0.0
    for name in names:
        cluster = cluster_for_name(name)
        if cluster is None:
            score += 1.0
        else:
            n = seen_clusters.get(cluster, 0)
            score += 1.0 if n == 0 else 0.25
            seen_clusters[cluster] = n + 1
    return score


@dataclass
class SharpProfile:
    wallet: str
    name: str
    eligible: bool
    directional_rate: float
    is_market_maker: bool
    sports_positions: int
    same_market_hedges: int
    both_side_win_markets: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _is_both_side_win_market(positions: list[dict]) -> bool:
    """Yes and No both held on same team-win binary (e.g. Norway win Yes + No)."""
    yes_stake = no_stake = 0.0
    for p in positions:
        o = (p.get("outcome") or "").lower()
        s = _stake(p)
        if o == "yes":
            yes_stake += s
        elif o == "no":
            no_stake += s
    return yes_stake >= MIN_HEDGE_STAKE and no_stake >= MIN_HEDGE_STAKE


def screen_wallet(wallet: str, name: str, positions: list[dict]) -> SharpProfile:
    sports = []
    for p in positions:
        slug = p.get("eventSlug") or p.get("slug") or ""
        if derive_sport(slug):
            sports.append(p)

    by_cid: dict[str, list[dict]] = defaultdict(list)
    for p in sports:
        cid = p.get("conditionId")
        if cid:
            by_cid[cid].append(p)

    same_market_hedges = 0
    both_side_win = 0
    directional_rows = 0

    for cid, ps in by_cid.items():
        outcomes = {p.get("outcome") for p in ps}
        if len(outcomes) > 1 and sum(_stake(p) for p in ps) >= MIN_HEDGE_STAKE:
            same_market_hedges += 1
        elif _is_both_side_win_market(ps):
            both_side_win += 1
        else:
            directional_rows += len(ps)

    total = len(sports) or 1
    directional_rate = directional_rows / total

    reasons = []
    if _norm_name(name) in {n.lower() for n in HARD_EXCLUDE_NAMES}:
        reasons.append("hard_exclude")

    is_mm = False
    if directional_rate < MIN_DIRECTIONAL_RATE:
        is_mm = True
        reasons.append(f"directional_rate={directional_rate:.0%}<{MIN_DIRECTIONAL_RATE:.0%}")
    if same_market_hedges >= 2:
        is_mm = True
        reasons.append(f"same_market_hedges={same_market_hedges}")
    if both_side_win >= 1:
        is_mm = True
        reasons.append(f"both_side_win_markets={both_side_win}")
    if len(sports) > MAX_OPEN_SPORTS_POSITIONS:
        is_mm = True
        reasons.append(f"huge_book={len(sports)} positions")

    eligible = not is_mm and not reasons

    return SharpProfile(
        wallet=wallet,
        name=name,
        eligible=eligible,
        directional_rate=directional_rate,
        is_market_maker=is_mm,
        sports_positions=len(sports),
        same_market_hedges=same_market_hedges,
        both_side_win_markets=both_side_win,
        reason="; ".join(reasons) if reasons else "ok",
    )


def screen_pool(
    positions_by_wallet: dict[str, list[dict]],
    sharp_meta: dict[str, dict],
) -> dict[str, SharpProfile]:
    profiles = {}
    for wallet, positions in positions_by_wallet.items():
        meta = sharp_meta.get(wallet, {})
        name = meta.get("name", "?")
        profiles[wallet] = screen_wallet(wallet, name, positions)
    return profiles


def eligible_wallets(profiles: dict[str, SharpProfile]) -> set[str]:
    return {w for w, p in profiles.items() if p.eligible}
