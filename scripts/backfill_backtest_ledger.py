"""One-time backfill: seed backtest ledger from existing queue.json signals."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import backtest_ledger, queue_manager

QUEUE = Path(__file__).resolve().parent.parent / "data" / "bot" / "queue.json"


def main():
    if not QUEUE.exists():
        print("no queue.json")
        return
    with open(QUEUE) as f:
        state = json.load(f)
    n = 0
    for key, rec in state.get("signals", {}).items():
        sig = rec["signal"]
        hint = rec.get("kalshi_hint")
        ts = rec.get("first_seen_at")
        emailed = bool(rec.get("emailed_at"))
        if backtest_ledger.record_opened(key, sig, hint, emailed=emailed, ts=ts):
            n += 1
            print(f"  backfilled: {sig['title'][:50]} -> {sig['outcome']}")
    print(f"backfilled {n} signals into backtest ledger")


if __name__ == "__main__":
    main()
