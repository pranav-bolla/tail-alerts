"""
Immutable ledger for manual-strategy backtesting.

Every signal is recorded once at first fire (signal_opened) with a full
snapshot of prices, conviction, and sharps. When Polymarket settles the
market we append signal_resolved with won/lost.

Run the report when ready:
    python -m bot.backtest_report
    python -m bot.backtest_report --stake 10 --entry max_kalshi

Ledger: data/bot/backtest_ledger.jsonl
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILE = Path(__file__).resolve().parent.parent / "data" / "bot" / "backtest_ledger.jsonl"
OPENED_KEYS_FILE = Path(__file__).resolve().parent.parent / "data" / "bot" / "backtest_opened_keys.json"


def _append(rec: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _load_opened_keys() -> set[str]:
    if not OPENED_KEYS_FILE.exists():
        return set()
    with open(OPENED_KEYS_FILE) as f:
        return set(json.load(f))


def _save_opened_keys(keys: set[str]) -> None:
    tmp = OPENED_KEYS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(keys), f, indent=2)
    tmp.replace(OPENED_KEYS_FILE)


def record_opened(signal_key: str, signal: dict, kalshi_hint: dict | None,
                  emailed: bool = False, ts: str | None = None) -> bool:
    """Write immutable snapshot the first time a signal fires. Returns True if new."""
    keys = _load_opened_keys()
    if signal_key in keys:
        return False

    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    _append({
        "event": "signal_opened",
        "ts": ts,
        "signal_key": signal_key,
        "cid": signal["cid"],
        "outcome": signal.get("action_outcome") or signal["outcome"],
        "title": signal["title"],
        "sport": signal["sport"],
        "slug": signal.get("slug"),
        "game_start_time": signal.get("game_start_time"),
        "consensus_n": signal["consensus_n"],
        "opposing_n": signal.get("opposing_n"),
        "stake_conviction": signal["stake_conviction"],
        "consensus_stake": signal["consensus_stake"],
        "opposing_stake": signal.get("opposing_stake"),
        "consensus_avg_entry": signal["consensus_avg_entry"],
        "consensus_max_entry": signal.get("consensus_max_entry"),
        "cur_price": signal["cur_price"],
        "max_kalshi_price": signal["max_kalshi_price"],
        "contributing_sharps": signal.get("contributing_sharps", []),
        "opposing_sharps": signal.get("opposing_sharps", []),
        "signal_type": signal.get("signal_type"),
        "thesis_label": signal.get("thesis_label"),
        "kalshi_hint": kalshi_hint,
        "emailed": emailed,
    })
    keys.add(signal_key)
    _save_opened_keys(keys)
    return True


def record_resolved(signal_key: str, cid: str, outcome: str,
                    winning_outcome: str | None, won: bool | None) -> None:
    _append({
        "event": "signal_resolved",
        "ts": datetime.now(timezone.utc).isoformat(),
        "signal_key": signal_key,
        "cid": cid,
        "outcome": outcome,
        "winning_outcome": winning_outcome,
        "won": won,
    })


def load_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    out = []
    with open(LEDGER_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out
