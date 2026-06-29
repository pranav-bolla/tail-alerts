"""
One-shot migration: hydrate queue_manager state from the legacy
signal_queue.jsonl. After this runs, the bot's queue manager owns
the lifecycle. The legacy .jsonl/.md are renamed as backups.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import queue_manager  # noqa: E402

LEGACY_JSONL = queue_manager.DATA_DIR / "signal_queue.jsonl"
LEGACY_MD = queue_manager.DATA_DIR / "signal_queue.md"


def main():
    if not LEGACY_JSONL.exists():
        print("no legacy jsonl, nothing to migrate")
        return

    state = queue_manager.load_queue()
    migrated = 0
    with open(LEGACY_JSONL) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sig = rec.get("signal")
            hint = rec.get("kalshi_hint")
            ts = rec.get("ts")
            if not sig or not ts:
                continue
            key = queue_manager.signal_key(sig["cid"], sig["outcome"])
            if key in state["signals"]:
                continue
            state["signals"][key] = {
                "first_seen_at": ts,
                "last_seen_at": ts,
                "cycles_seen": 1,
                "status": queue_manager.STATUS_PENDING,
                "signal": sig,
                "kalshi_hint": hint,
                "resolution": None,
            }
            migrated += 1

    queue_manager.save_queue(state)
    queue_manager.regenerate_queue_md(state)
    print(f"migrated {migrated} signals into queue state")

    # rename legacy files
    if LEGACY_JSONL.exists():
        bak = LEGACY_JSONL.with_suffix(".jsonl.legacy")
        LEGACY_JSONL.rename(bak)
        print(f"  legacy jsonl -> {bak.name}")


if __name__ == "__main__":
    main()
