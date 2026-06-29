"""
Track position stake over time and detect conviction adds (V2 signal type B).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.signal_filter import derive_sport
from bot.sharp_screener import _stake

SNAPSHOT_FILE = Path(__file__).resolve().parent.parent / "data" / "bot" / "stake_snapshots.json"

MIN_ADD_ABSOLUTE = 5_000
MIN_ADD_PCT = 0.50
MIN_TOTAL_AFTER = 3_000


@dataclass
class AddSignal:
    wallet: str
    name: str
    cid: str
    outcome: str
    title: str
    sport: str
    slug: str
    prev_stake: float
    new_stake: float
    delta: float
    delta_pct: float
    avg_price: float
    cur_price: float

    def to_v2_signal_dict(self) -> dict:
        max_k = min(0.97, self.avg_price + 0.02)
        return {
            "signal_type": "add",
            "key": f"add::{self.wallet}::{self.cid}::{self.outcome}",
            "sport": self.sport,
            "title": self.title,
            "slug": self.slug,
            "thesis_label": f"ADD: {self.name} +${self.delta:,.0f} (now ${self.new_stake:,.0f})",
            "action_outcome": self.outcome,
            "cid": self.cid,
            "consensus_n": 1,
            "raw_sharp_count": 1,
            "consensus_stake": self.new_stake,
            "stake_conviction": 1.0,
            "consensus_avg_entry": self.avg_price,
            "cur_price": self.cur_price,
            "max_kalshi_price": max_k,
            "contributing_sharps": [{
                "name": self.name,
                "wallet": self.wallet[:10] + "...",
                "stake": round(self.new_stake, 0),
                "avg_price": round(self.avg_price, 3),
                "delta": round(self.delta, 0),
            }],
            "outcome": self.outcome,
            "delta": self.delta,
            "prev_stake": self.prev_stake,
        }


def _position_key(wallet: str, cid: str, outcome: str) -> str:
    return f"{wallet}::{cid}::{outcome}"


def load_snapshots() -> dict:
    if not SNAPSHOT_FILE.exists():
        return {"positions": {}, "updated_at": None}
    with open(SNAPSHOT_FILE) as f:
        return json.load(f)


def save_snapshots(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def detect_adds(
    positions_by_wallet: dict[str, list[dict]],
    sharp_meta: dict[str, dict],
    eligible_wallets: set[str],
) -> list[AddSignal]:
    snap = load_snapshots()
    prev: dict[str, float] = snap.get("positions", {})
    current: dict[str, float] = {}
    adds: list[AddSignal] = []

    for wallet, positions in positions_by_wallet.items():
        if wallet not in eligible_wallets:
            continue
        name = sharp_meta.get(wallet, {}).get("name", "?")
        for p in positions:
            slug = p.get("eventSlug") or p.get("slug") or ""
            sport = derive_sport(slug)
            if not sport:
                continue
            cid = p.get("conditionId")
            outcome = p.get("outcome")
            if not cid or not outcome:
                continue
            stake = _stake(p)
            pk = _position_key(wallet, cid, outcome)
            current[pk] = stake

            old = prev.get(pk, 0.0)
            if old <= 0:
                continue
            delta = stake - old
            if delta < MIN_ADD_ABSOLUTE and delta / old < MIN_ADD_PCT:
                continue
            if stake < MIN_TOTAL_AFTER:
                continue
            try:
                avg = float(p.get("avgPrice", 0) or 0)
                cur = float(p.get("curPrice", 0) or 0)
            except (TypeError, ValueError):
                avg, cur = 0.0, 0.0

            adds.append(AddSignal(
                wallet=wallet,
                name=name,
                cid=cid,
                outcome=outcome,
                title=p.get("title") or "?",
                sport=sport,
                slug=slug,
                prev_stake=old,
                new_stake=stake,
                delta=delta,
                delta_pct=delta / old if old else 0,
                avg_price=avg,
                cur_price=cur,
            ))

    snap["positions"] = current
    save_snapshots(snap)
    adds.sort(key=lambda a: a.delta, reverse=True)
    return adds
