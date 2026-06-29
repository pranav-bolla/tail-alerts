"""
Persistent bot state.

Tracks:
  - signals_fired: set of (condition_id, outcome) we've already acted on (dedup)
  - open_bets: list of bets placed, with kalshi ticker, side, count, price, ts
  - daily_pnl: PnL booked today (resets at UTC midnight)
  - settled_bets: history (rolling 1000)
  - kill_switch_active: bool — set to True if daily loss limit hit

Atomic writes via tmpfile + rename.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "bot_state.json"
JOURNAL_FILE = DATA_DIR / "bot_journal.jsonl"


DEFAULT_STATE = {
    "signals_fired": [],  # list of [cid, outcome]
    "open_bets": [],
    "settled_bets": [],
    "daily_pnl": 0.0,
    "daily_pnl_date": None,
    "kill_switch_active": False,
    "started_at": None,
    "last_run_at": None,
    "stats": {
        "signals_seen": 0,
        "signals_fired": 0,
        "matches_failed": 0,
        "price_guard_failed": 0,
        "bets_placed": 0,
    },
}


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    if not STATE_FILE.exists():
        s = dict(DEFAULT_STATE)
        s["started_at"] = datetime.now(timezone.utc).isoformat()
        s["daily_pnl_date"] = utc_date()
        save_state(s)
        return s
    with open(STATE_FILE) as f:
        s = json.load(f)
    # Migrate any missing keys
    for k, v in DEFAULT_STATE.items():
        if k not in s:
            s[k] = v
    # Daily PnL reset
    if s.get("daily_pnl_date") != utc_date():
        s["daily_pnl"] = 0.0
        s["daily_pnl_date"] = utc_date()
        s["kill_switch_active"] = False  # daily reset of kill switch
    return s


def save_state(state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def already_fired(state: dict, cid: str, outcome: str) -> bool:
    return [cid, outcome] in state["signals_fired"]


def mark_fired(state: dict, cid: str, outcome: str) -> None:
    if not already_fired(state, cid, outcome):
        state["signals_fired"].append([cid, outcome])


def record_bet(state: dict, bet: dict) -> None:
    state["open_bets"].append(bet)
    state["stats"]["bets_placed"] += 1


def settle_bet(state: dict, bet: dict, pnl: float) -> None:
    state["open_bets"] = [b for b in state["open_bets"] if b["bet_id"] != bet["bet_id"]]
    bet["settled_pnl"] = pnl
    bet["settled_at"] = datetime.now(timezone.utc).isoformat()
    state["settled_bets"].append(bet)
    state["settled_bets"] = state["settled_bets"][-1000:]
    state["daily_pnl"] += pnl


def journal(event: str, **kwargs) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **kwargs,
    }
    with open(JOURNAL_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def check_kill_switch(state: dict, max_daily_loss: float) -> bool:
    """Return True if we should stop trading for the day."""
    if state["kill_switch_active"]:
        return True
    if state["daily_pnl"] <= -abs(max_daily_loss):
        state["kill_switch_active"] = True
        journal("KILL_SWITCH_TRIGGERED",
                daily_pnl=state["daily_pnl"],
                max_daily_loss=max_daily_loss)
        return True
    return False


if __name__ == "__main__":
    s = load_state()
    print(json.dumps(s, indent=2, default=str)[:2000])
