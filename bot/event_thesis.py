"""
Event-level thesis builder (V2 signal type A + C).

Groups directional-sharp positions by event_slug, buckets by market type,
and emits at most ONE thesis per game plus optional strict ML clusters.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict

from bot.signal_filter import derive_sport, KALSHI_SLIP_CENTS, MIN_OPEN_PRICE, MAX_OPEN_PRICE
from bot.sharp_screener import effective_independence_count, _stake

MIN_GAME_STAKE = 15_000
MIN_INDIVIDUAL_STAKE = 3_000
MIN_INDEPENDENCE = 2.0  # effective independent sharps
MIN_BUCKET_CONVICTION = 0.85
SPLIT_BOOK_GAP = 0.30  # suppress if top 2 buckets within 30%

PROP_PREFIXES = ("spread:", "o/u", "1st half", "both teams", "leading at halftime")

WIN_RE = re.compile(r"^will (.+?) win on \d{4}-\d{2}-\d{2}", re.I)
DRAW_RE = re.compile(r"end in a draw", re.I)
MLB_RE = re.compile(r"^(.+?)\s+(?:vs\.?|@)\s+(.+?)\s*$", re.I)


@dataclass
class V2Signal:
    signal_type: str  # thesis | cluster | add
    key: str
    sport: str
    title: str
    slug: str
    thesis_label: str
    action_outcome: str | None
    cid: str | None
    consensus_n: float  # effective independence
    raw_sharp_count: int
    consensus_stake: float
    stake_conviction: float
    consensus_avg_entry: float
    cur_price: float
    max_kalshi_price: float
    game_start_time: str | None = None
    contributing_sharps: list[dict] = field(default_factory=list)
    buckets: dict | None = None

    def to_signal_dict(self) -> dict:
        """Shape compatible with queue_manager / backtest ledger."""
        d = asdict(self)
        d["outcome"] = self.action_outcome or self.thesis_label
        return d


def _is_prop_title(title: str) -> bool:
    t = (title or "").lower()
    if any(t.startswith(p) for p in PROP_PREFIXES):
        return True
    if " o/u " in t or ": o/u" in t or "over/under" in t:
        return True
    return False


def _classify_bucket(title: str, outcome: str) -> tuple[str, str] | None:
    """Return (bucket_key, direction) e.g. ('iraq_win', 'no')."""
    title = title or ""
    outcome_l = (outcome or "").lower()

    if _is_prop_title(title):
        return None

    if DRAW_RE.search(title):
        return "draw", "yes" if outcome_l == "yes" else "no"

    m = WIN_RE.match(title.strip())
    if m:
        team = re.sub(r"[^\w\s]", "", m.group(1).strip().lower())
        team = re.sub(r"\s+", "_", team)[:30]
        return f"{team}_win", "yes" if outcome_l == "yes" else "no"

    m2 = MLB_RE.match(title.strip())
    if m2 and not _is_prop_title(title):
        team = re.sub(r"[^\w\s]", "", outcome.strip().lower())
        team = re.sub(r"\s+", "_", team)[:30]
        return f"ml_{team}", "yes"

    return None


def _bucket_label(bucket: str, direction: str) -> str:
    parts = bucket.replace("_", " ").title()
    if direction == "yes":
        return f"{parts} (YES side)"
    return f"NOT {parts} (NO side)"


def _aggregate_event(
    slug: str,
    rows: list[dict],
) -> list[V2Signal]:
    """Build thesis signals for one event_slug."""
    if not rows:
        return []

    sport = rows[0].get("sport", "?")
    title = rows[0].get("title", slug)

    # bucket -> direction -> list of holder rows
    bucket_stake: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    bucket_holders: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    bucket_meta: dict[str, dict] = {}

    for row in rows:
        b = _classify_bucket(row["title"], row["outcome"])
        if not b:
            continue
        bucket, direction = b
        stake = row["stake"]
        if stake < MIN_INDIVIDUAL_STAKE:
            continue
        bucket_stake[bucket][direction] += stake
        bucket_holders[bucket][direction].append(row)
        if bucket not in bucket_meta:
            bucket_meta[bucket] = {
                "title": row["title"],
                "cid": row["cid"],
                "outcome": row["outcome"],
                "cur_price": row["cur_price"],
                "avg_price": row["avg_price"],
            }

    if not bucket_stake:
        return []

    # Score each bucket+direction combination
    candidates = []
    total_game_stake = sum(
        s for dirs in bucket_stake.values() for s in dirs.values()
    )

    for bucket, dirs in bucket_stake.items():
        for direction, stake in dirs.items():
            holders = bucket_holders[bucket][direction]
            names = [h["name"] for h in holders]
            indep = effective_independence_count(names)
            if indep < MIN_INDEPENDENCE:
                continue
            if stake < MIN_GAME_STAKE * 0.5:
                continue
            conviction = stake / total_game_stake if total_game_stake else 0
            if conviction < MIN_BUCKET_CONVICTION:
                continue
            meta = bucket_meta[bucket]
            avg_entry = sum(h["avg_price"] * h["stake"] for h in holders) / stake
            cur = sum(h["cur_price"] for h in holders) / len(holders)
            candidates.append({
                "bucket": bucket,
                "direction": direction,
                "stake": stake,
                "conviction": conviction,
                "indep": indep,
                "holders": holders,
                "meta": meta,
                "avg_entry": avg_entry,
                "cur": cur,
            })

    if not candidates:
        return []

    candidates.sort(key=lambda c: (c["stake"], c["indep"]), reverse=True)
    top = candidates[0]

    # Split book check
    if len(candidates) >= 2:
        second = candidates[1]
        gap = abs(top["stake"] - second["stake"]) / max(top["stake"], 1)
        if gap < SPLIT_BOOK_GAP:
            return []  # ambiguous — no signal

    label = _bucket_label(top["bucket"], top["direction"])
    meta = top["meta"]
    action = meta["outcome"] if top["direction"] == ("yes" if meta["outcome"].lower() in ("yes",) else "no") else meta["outcome"]

    # For NO-side thesis, action_outcome is still the Polymarket outcome they'd buy
    action_outcome = meta["outcome"]
    max_k = min(0.97, top["avg_entry"] + KALSHI_SLIP_CENTS / 100)

    sig = V2Signal(
        signal_type="thesis",
        key=f"{slug}::thesis::{top['bucket']}_{top['direction']}",
        sport=sport,
        title=title.split("?")[0] if "?" in title else title,
        slug=slug,
        thesis_label=label,
        action_outcome=action_outcome,
        cid=meta["cid"],
        consensus_n=top["indep"],
        raw_sharp_count=len(top["holders"]),
        consensus_stake=top["stake"],
        stake_conviction=top["conviction"],
        consensus_avg_entry=top["avg_entry"],
        cur_price=top["cur"],
        max_kalshi_price=max_k,
        contributing_sharps=[{
            "name": h["name"],
            "wallet": h["wallet"][:10] + "...",
            "stake": round(h["stake"], 0),
            "avg_price": round(h["avg_price"], 3),
        } for h in top["holders"]],
        buckets={b: dict(d) for b, d in bucket_stake.items()},
    )
    return [sig]


def _build_ml_clusters(
    slug: str,
    rows: list[dict],
) -> list[V2Signal]:
    """Strict moneyline cluster (type C) — one per clean ML market."""
    by_cid: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if _is_prop_title(row["title"]):
            continue
        if WIN_RE.match(row["title"]) or DRAW_RE.search(row["title"]):
            continue
        if not MLB_RE.match(row["title"]):
            continue
        by_cid[row["cid"]].append(row)

    out = []
    for cid, holders_raw in by_cid.items():
        by_outcome: dict[str, list] = defaultdict(list)
        for h in holders_raw:
            by_outcome[h["outcome"]].append(h)

        if len(by_outcome) != 1:
            continue  # sharps split on same ML market

        outcome, holders = next(iter(by_outcome.items()))
        stake = sum(h["stake"] for h in holders)
        names = [h["name"] for h in holders]
        indep = effective_independence_count(names)
        if indep < 3.0:
            continue
        if stake < 25_000:
            continue
        if any(h["stake"] < 5_000 for h in holders):
            continue

        avg = sum(h["avg_price"] * h["stake"] for h in holders) / stake
        cur = sum(h["cur_price"] for h in holders) / len(holders)
        title = holders[0]["title"]

        out.append(V2Signal(
            signal_type="cluster",
            key=f"{cid}::{outcome}",
            sport=holders[0]["sport"],
            title=title,
            slug=slug,
            thesis_label=f"ML cluster: {outcome}",
            action_outcome=outcome,
            cid=cid,
            consensus_n=indep,
            raw_sharp_count=len(holders),
            consensus_stake=stake,
            stake_conviction=1.0,
            consensus_avg_entry=avg,
            cur_price=cur,
            max_kalshi_price=min(0.97, avg + KALSHI_SLIP_CENTS / 100),
            contributing_sharps=[{
                "name": h["name"],
                "wallet": h["wallet"][:10] + "...",
                "stake": round(h["stake"], 0),
                "avg_price": round(h["avg_price"], 3),
            } for h in holders],
        ))
    return out


def build_event_signals(
    positions_by_wallet: dict[str, list[dict]],
    sharp_meta: dict[str, dict],
    eligible_wallets: set[str],
) -> list[V2Signal]:
    """Build all V2 thesis + cluster signals from directional-only sharps."""
    rows_by_slug: dict[str, list[dict]] = defaultdict(list)

    for wallet, positions in positions_by_wallet.items():
        if wallet not in eligible_wallets:
            continue
        name = sharp_meta.get(wallet, {}).get("name", "?")
        for p in positions:
            slug = p.get("eventSlug") or p.get("slug") or ""
            sport = derive_sport(slug)
            if not sport:
                continue
            try:
                cur = float(p.get("curPrice", 0) or 0)
                avg = float(p.get("avgPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not (MIN_OPEN_PRICE <= cur <= MAX_OPEN_PRICE):
                continue
            stake = _stake(p)
            if stake < MIN_INDIVIDUAL_STAKE:
                continue
            rows_by_slug[slug].append({
                "wallet": wallet,
                "name": name,
                "cid": p.get("conditionId"),
                "outcome": p.get("outcome"),
                "title": p.get("title") or "?",
                "slug": slug,
                "sport": sport,
                "stake": stake,
                "avg_price": avg,
                "cur_price": cur,
            })

    signals: list[V2Signal] = []
    seen_keys: set[str] = set()

    for slug, rows in rows_by_slug.items():
        for sig in _aggregate_event(slug, rows):
            if sig.key not in seen_keys:
                signals.append(sig)
                seen_keys.add(sig.key)
        for sig in _build_ml_clusters(slug, rows):
            if sig.key not in seen_keys:
                signals.append(sig)
                seen_keys.add(sig.key)

    signals.sort(key=lambda s: (s.stake_conviction, s.consensus_stake), reverse=True)
    return signals
