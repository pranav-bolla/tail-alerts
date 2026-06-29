"""
Write the signal review queue.

For each new signal that fires, append to:
  - data/bot/signal_queue.md  (human-readable, for phone/desktop review)
  - data/bot/signal_queue.jsonl  (machine-readable log)

Mark signals as 'fired' in state so we don't surface them twice.

This is the minimum-viable output until we wire up structured Kalshi matchers
per sport.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_MD = DATA_DIR / "signal_queue.md"
QUEUE_JSONL = DATA_DIR / "signal_queue.jsonl"


def init_queue_md_if_empty() -> None:
    if not QUEUE_MD.exists() or QUEUE_MD.stat().st_size == 0:
        QUEUE_MD.write_text(
            "# Consensus-Tail Signal Queue\n\n"
            "Each row is a market where ≥2 winning Polymarket sharps converged "
            "with ≥85% stake-weighted conviction.\n\n"
            "Place the bet on Kalshi manually if the price is within the "
            "max-kalshi-price column.\n\n"
            "---\n\n"
        )


def append_signal(signal: dict, kalshi_hint: dict | None = None) -> None:
    """signal: the dict from signal_filter.signal_to_dict()"""
    init_queue_md_if_empty()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sport = signal["sport"].upper()
    title = signal["title"]
    outcome = signal["outcome"]
    n = signal["consensus_n"]
    opp = signal["opposing_n"]
    conv = signal["stake_conviction"]
    avg = signal["consensus_avg_entry"]
    max_p = signal["max_kalshi_price"]
    cur = signal["cur_price"]
    stake = signal["consensus_stake"]

    sharps_str = ", ".join(
        f"{h['name']}(${h['stake']:,.0f}@{h['avg_price']:.2f})"
        for h in signal["contributing_sharps"][:5]
    )

    block = (
        f"\n## [{sport}] {title}\n"
        f"- **BUY**: `{outcome}` side\n"
        f"- **Polymarket consensus**: {n} sharps on this side vs {opp} opposing, "
        f"{conv:.0%} stake-weighted conviction\n"
        f"- **Avg entry (Polymarket)**: ${avg:.3f} | **Current**: ${cur:.3f} | "
        f"**Max acceptable Kalshi price**: ${max_p:.3f}\n"
        f"- **Total sharp stake**: ${stake:,.0f}\n"
        f"- **Sharps**: {sharps_str}\n"
    )

    if kalshi_hint:
        block += (
            f"- **Kalshi hint** (fuzzy match, verify manually!):\n"
            f"  - ticker: `{kalshi_hint.get('ticker', '?')}` side: `{kalshi_hint.get('side', '?')}`\n"
            f"  - title: {kalshi_hint.get('title', '?')[:80]}\n"
            f"  - match score: {kalshi_hint.get('score', 0):.2f}\n"
            f"  - current ask: ${kalshi_hint.get('price_to_pay') or 0:.3f}\n"
        )
    else:
        block += "- **Kalshi hint**: NONE (search manually for: "
        block += f"{title} / {outcome})\n"

    block += f"- _signal fired at {ts}, cid: `{signal['cid'][:12]}...`_\n"

    with open(QUEUE_MD, "a") as f:
        f.write(block)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "signal": signal,
        "kalshi_hint": kalshi_hint,
    }
    with open(QUEUE_JSONL, "a") as f:
        f.write(json.dumps(record) + "\n")
