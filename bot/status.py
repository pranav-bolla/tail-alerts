"""
Quick CLI to check the bot's current queue + lifetime stats.

    python -m bot.status              # show current queue + stats
    python -m bot.status --history    # also dump recent history events
    python -m bot.status --winrate    # show win rate of resolved signals
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bot.queue_manager import (
    QUEUE_STATE, HISTORY_JSONL,
    STATUS_PENDING, STATUS_ACTIVE, STATUS_STALE, STATUS_RESOLVED,
)
from bot import game_date

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"


def load_state() -> dict:
    if not QUEUE_STATE.exists():
        return {"signals": {}}
    with open(QUEUE_STATE) as f:
        return json.load(f)


def fmt_age(iso_str: str) -> str:
    try:
        ts = datetime.fromisoformat(iso_str)
        delta = datetime.now(timezone.utc) - ts
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m"
        if mins < 24 * 60:
            return f"{mins // 60}h{mins % 60}m"
        return f"{mins // (24 * 60)}d{(mins % (24*60)) // 60}h"
    except Exception:
        return "?"


def print_queue(state: dict) -> None:
    sigs = state.get("signals", {})
    counts = defaultdict(int)
    for rec in sigs.values():
        counts[rec["status"]] += 1

    actionable = [(k, r) for k, r in sigs.items()
                  if r["status"] in (STATUS_PENDING, STATUS_ACTIVE)]
    actionable.sort(
        key=lambda kv: (kv[1]["signal"]["stake_conviction"],
                        kv[1]["signal"]["consensus_stake"]),
        reverse=True,
    )

    print(f"{BOLD}=== CONSENSUS-TAIL QUEUE ==={RESET}")
    print(
        f"  {GREEN}{counts[STATUS_PENDING] + counts[STATUS_ACTIVE]} actionable{RESET}"
        f"  ({counts[STATUS_PENDING]} pending, {counts[STATUS_ACTIVE]} active)"
        f"  |  {YELLOW}{counts[STATUS_STALE]} stale{RESET}"
        f"  |  {BLUE}{counts[STATUS_RESOLVED]} resolved{RESET}"
    )
    print()

    if not actionable:
        print(f"{YELLOW}No actionable signals right now.{RESET}\n")
        return

    print(f"{BOLD}--- ACTIONABLE NOW ---{RESET}")
    for i, (key, rec) in enumerate(actionable, 1):
        sig = rec["signal"]
        hint = rec.get("kalshi_hint")
        tag = "NEW" if rec["status"] == STATUS_PENDING else "ACT"
        tag_color = GREEN if tag == "NEW" else BLUE
        sport = sig["sport"].upper()
        title = sig["title"]
        if len(title) > 60:
            title = title[:57] + "..."

        line1 = (
            f"{i:>2}. {tag_color}[{tag}]{RESET} {BOLD}{sport:>5}{RESET}  "
            f"{title}  {GREEN}-> {sig['outcome']}{RESET}"
        )
        line2 = (
            f"      conv={sig['stake_conviction']:.0%}  n={sig['consensus_n']}  "
            f"poly_avg=${sig['consensus_avg_entry']:.2f}  "
            f"now=${sig['cur_price']:.2f}  "
            f"{YELLOW}max=${sig['max_kalshi_price']:.2f}{RESET}  "
            f"stake=${sig['consensus_stake']:,.0f}  "
            f"age={fmt_age(rec['first_seen_at'])}"
        )
        print(line1)
        when = game_date.format_game_when(sig, hint)
        if when:
            print(f"      {when}")
        print(line2)
        if hint:
            ticker = hint.get("ticker", "?")
            side = hint.get("side", "?")
            score = hint.get("score", 0)
            tag = "EXACT" if hint.get("structured") else f"fuzzy {score:.2f}"
            print(f"      kalshi: {BLUE}{ticker}{RESET}/{side} ({tag})")
        else:
            print(f"      kalshi: {RED}no match{RESET} (manual search needed)")
    print()


def print_winrate(state: dict) -> None:
    sigs = state.get("signals", {})
    resolved = [rec for rec in sigs.values()
                if rec["status"] == STATUS_RESOLVED and rec.get("resolution")]
    by_sport = defaultdict(lambda: {"won": 0, "lost": 0, "unknown": 0})

    for rec in resolved:
        sport = rec["signal"]["sport"]
        won = rec["resolution"].get("won")
        if won is True:
            by_sport[sport]["won"] += 1
        elif won is False:
            by_sport[sport]["lost"] += 1
        else:
            by_sport[sport]["unknown"] += 1

    if not resolved:
        print(f"{YELLOW}No resolved signals yet.{RESET}\n")
        return

    print(f"{BOLD}--- WIN RATE BY SPORT ({len(resolved)} resolved) ---{RESET}")
    overall_w = overall_l = 0
    for sport, c in sorted(by_sport.items()):
        n = c["won"] + c["lost"]
        wr = c["won"] / n if n else 0
        overall_w += c["won"]
        overall_l += c["lost"]
        print(f"  {sport:>10}: {c['won']}-{c['lost']}  ({wr:.0%})"
              + (f"  ({c['unknown']} unknown)" if c["unknown"] else ""))
    nt = overall_w + overall_l
    if nt:
        print(f"  {BOLD}{'OVERALL':>10}: {overall_w}-{overall_l}  "
              f"({overall_w/nt:.0%}){RESET}")
    print()


def print_history(n: int = 20) -> None:
    if not HISTORY_JSONL.exists():
        print(f"{YELLOW}No history yet.{RESET}\n")
        return
    with open(HISTORY_JSONL) as f:
        lines = f.readlines()
    print(f"{BOLD}--- RECENT EVENTS (last {n}) ---{RESET}")
    for line in lines[-n:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = rec.get("ts", "")[:19].replace("T", " ")
        ev = rec.get("event", "?")
        cid = (rec.get("cid") or "")[:10]
        out = rec.get("outcome") or ""
        sport = rec.get("sport") or ""
        won = rec.get("won")
        won_str = ""
        if won is True:
            won_str = f"  {GREEN}WON{RESET}"
        elif won is False:
            won_str = f"  {RED}LOST{RESET}"
        print(f"  {ts}  {ev:<22}  {sport:<8}  {cid}.../{out}{won_str}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--winrate", action="store_true",
                    help="show win rate of resolved signals")
    ap.add_argument("--history", type=int, nargs="?", const=20, default=0,
                    help="show last N history events (default 20)")
    args = ap.parse_args()

    state = load_state()
    print_queue(state)
    if args.winrate:
        print_winrate(state)
    if args.history:
        print_history(args.history)


if __name__ == "__main__":
    main()
