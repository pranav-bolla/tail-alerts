"""
Live signal queue manager.

Each signal goes through a lifecycle:

  pending   → just fired this cycle, written to queue.md
  active    → still consensus, market still open, still actionable
  stale     → consensus weakened (sharps exited) but market still open
  expired   → market closed/about to close before user could act
  resolved  → market settled; we record won/lost for backtest verification

State is persisted to bot/queue.json. The queue.md is REGENERATED each cycle
showing currently-actionable signals (pending + active) sorted by recency.

A separate signal_history.jsonl is appended to with every transition.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot import notifier
from bot import game_date
from bot import backtest_ledger

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_STATE = DATA_DIR / "queue.json"
QUEUE_MD = DATA_DIR / "signal_queue.md"
HISTORY_JSONL = DATA_DIR / "signal_history.jsonl"
LEGACY_QUEUE_BACKUP = DATA_DIR / "signal_queue_legacy.md"

# Lifecycle transitions
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_EXPIRED = "expired"
STATUS_RESOLVED = "resolved"


def load_queue() -> dict:
    if not QUEUE_STATE.exists():
        return {"signals": {}, "version": 1}
    with open(QUEUE_STATE) as f:
        return json.load(f)


def save_queue(state: dict) -> None:
    tmp = QUEUE_STATE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, QUEUE_STATE)


def signal_key(cid: str, outcome: str, alt_key: str | None = None) -> str:
    if alt_key:
        return alt_key
    return f"{cid}::{outcome}"


def _history(event: str, **kwargs) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    with open(HISTORY_JSONL, "a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_market_resolution(cid: str) -> dict | None:
    """Check if a Polymarket condition has resolved. Returns
    {resolved: bool, winning_outcome: str, cur_yes_price: float}.
    """
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_ids": cid}, timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        if not markets:
            return None
        m = markets[0]
        is_resolved = bool(m.get("closed") or m.get("resolved"))
        # Get the winning outcome (Polymarket-style)
        winning_outcome = None
        outcomes = m.get("outcomes")
        outcome_prices = m.get("outcomePrices")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                pass
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                pass
        if outcomes and outcome_prices:
            for o, p in zip(outcomes, outcome_prices):
                try:
                    if float(p) >= 0.99:
                        winning_outcome = o
                        break
                except (TypeError, ValueError):
                    pass
        return {
            "resolved": is_resolved,
            "winning_outcome": winning_outcome,
            "end_date": m.get("endDate"),
            "active": m.get("active"),
            "closed": m.get("closed"),
        }
    except Exception:
        return None


def upsert_signal(state: dict, signal_dict: dict, kalshi_hint: dict | None) -> str:
    """Insert or update a signal. Returns its current status."""
    key = signal_key(
        signal_dict.get("cid") or "",
        signal_dict.get("outcome") or "",
        alt_key=signal_dict.get("key"),
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    if key not in state["signals"]:
        state["signals"][key] = {
            "first_seen_at": now_iso,
            "last_seen_at": now_iso,
            "cycles_seen": 1,
            "status": STATUS_PENDING,
            "signal": signal_dict,
            "kalshi_hint": kalshi_hint,
            "resolution": None,
            "emailed_at": None,
        }
        _history("signal_fired",
                 cid=signal_dict["cid"], outcome=signal_dict["outcome"],
                 sport=signal_dict["sport"],
                 stake_conviction=signal_dict["stake_conviction"],
                 consensus_n=signal_dict["consensus_n"],
                 avg_entry=signal_dict["consensus_avg_entry"])
        return STATUS_PENDING

    rec = state["signals"][key]
    rec["last_seen_at"] = now_iso
    rec["cycles_seen"] += 1
    rec["signal"] = signal_dict  # refresh latest metrics
    rec["kalshi_hint"] = kalshi_hint
    if rec["status"] == STATUS_PENDING and rec["cycles_seen"] >= 2:
        rec["status"] = STATUS_ACTIVE
        _history("signal_active", cid=signal_dict["cid"],
                 outcome=signal_dict["outcome"])
    elif rec["status"] == STATUS_STALE:
        rec["status"] = STATUS_ACTIVE  # re-emerged
        _history("signal_reactivated", cid=signal_dict["cid"],
                 outcome=signal_dict["outcome"])
    return rec["status"]


def age_unrefreshed_signals(state: dict, currently_firing: set,
                            stale_grace_cycles: int = 1) -> int:
    """Mark signals that didn't fire this cycle as STALE. Returns count."""
    n_marked = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for key, rec in state["signals"].items():
        if key in currently_firing:
            continue
        if rec["status"] in (STATUS_RESOLVED, STATUS_EXPIRED):
            continue
        if rec["status"] == STATUS_STALE:
            continue
        if rec["status"] == STATUS_PENDING:
            # Pending and didn't refresh -> went stale immediately
            rec["status"] = STATUS_STALE
            rec["went_stale_at"] = now_iso
            _history("signal_stale", cid=rec["signal"]["cid"],
                     outcome=rec["signal"]["outcome"],
                     reason="pending->stale (no_refresh)")
            n_marked += 1
        elif rec["status"] == STATUS_ACTIVE:
            rec["status"] = STATUS_STALE
            rec["went_stale_at"] = now_iso
            _history("signal_stale", cid=rec["signal"]["cid"],
                     outcome=rec["signal"]["outcome"],
                     reason="active->stale (sharps_exited)")
            n_marked += 1
    return n_marked


def check_resolutions(state: dict, max_per_cycle: int = 50) -> int:
    """Look up resolution for any signal not yet resolved. Throttled per cycle."""
    n_resolved = 0
    checked = 0
    for key, rec in state["signals"].items():
        if checked >= max_per_cycle:
            break
        if rec["status"] == STATUS_RESOLVED:
            continue
        cid = rec["signal"]["cid"]
        res = fetch_market_resolution(cid)
        checked += 1
        if not res:
            continue
        if res["resolved"] or res.get("closed"):
            won = (
                res["winning_outcome"] == (
                    rec["signal"].get("action_outcome") or rec["signal"]["outcome"]
                )
            ) if res["winning_outcome"] else None
            rec["status"] = STATUS_RESOLVED
            rec["resolution"] = {
                "winning_outcome": res["winning_outcome"],
                "won": won,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
            _history("signal_resolved",
                     cid=cid, outcome=rec["signal"]["outcome"],
                     winning_outcome=res["winning_outcome"], won=won)
            backtest_ledger.record_resolved(
                key, cid, rec["signal"]["outcome"],
                res["winning_outcome"], won,
            )
            n_resolved += 1
    return n_resolved


def prune_old_signals(state: dict, max_age_days: int = 14) -> int:
    """Drop resolved/stale signals older than N days."""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    drops = []
    for key, rec in state["signals"].items():
        if rec["status"] not in (STATUS_RESOLVED, STATUS_STALE, STATUS_EXPIRED):
            continue
        try:
            first_ts = datetime.fromisoformat(rec["first_seen_at"]).timestamp()
        except Exception:
            continue
        if first_ts < cutoff:
            drops.append(key)
    for key in drops:
        del state["signals"][key]
    return len(drops)


def regenerate_queue_md(state: dict) -> None:
    """Rewrite the queue.md to show currently-actionable signals."""
    active_or_pending = [
        (key, rec) for key, rec in state["signals"].items()
        if rec["status"] in (STATUS_PENDING, STATUS_ACTIVE)
    ]
    active_or_pending.sort(
        key=lambda kv: (kv[1]["signal"]["stake_conviction"],
                        kv[1]["signal"]["consensus_stake"]),
        reverse=True,
    )

    stats = defaultdict(int)
    for rec in state["signals"].values():
        stats[rec["status"]] += 1
    won = sum(1 for rec in state["signals"].values()
              if rec.get("resolution", {}) and rec["resolution"].get("won") is True)
    lost = sum(1 for rec in state["signals"].values()
               if rec.get("resolution", {}) and rec["resolution"].get("won") is False)

    lines = [
        "# Consensus-Tail Signal Queue",
        "",
        f"_Regenerated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._  ",
        f"_Bot version: signal-v2, polling every 10 min._",
        "",
        "## Stats",
        "",
        f"- **Actionable now**: {stats[STATUS_PENDING] + stats[STATUS_ACTIVE]} "
        f"({stats[STATUS_PENDING]} pending, {stats[STATUS_ACTIVE]} active)",
        f"- **Stale** (sharps exited): {stats[STATUS_STALE]}",
        f"- **Resolved**: {stats[STATUS_RESOLVED]} ({won} won / {lost} lost"
        + (f" → {100*won/max(1,won+lost):.0f}% WR)" if (won + lost) else ")"),
        "",
        "Each row below: ≥2 winning Polymarket sharps converged with ≥85% "
        "stake-weighted conviction. **Bet on Kalshi only if the Kalshi ask is "
        "within `Max Kalshi $`.**",
        "",
        "---",
        "",
    ]

    if not active_or_pending:
        lines.append("_No actionable signals right now. Bot is polling every 10 min._")
    else:
        for key, rec in active_or_pending:
            sig = rec["signal"]
            hint = rec.get("kalshi_hint")
            first = datetime.fromisoformat(rec["first_seen_at"])
            age_min = (datetime.now(timezone.utc) - first).total_seconds() / 60
            sharps_str = ", ".join(
                f"{h['name']}(${h['stake']:,.0f}@{h['avg_price']:.2f})"
                for h in sig.get("contributing_sharps", [])[:5]
            )
            status_emoji = "🆕" if rec["status"] == STATUS_PENDING else "✅"
            stype = sig.get("signal_type", "v1")
            tag = {"thesis": "THESIS", "cluster": "CLUSTER", "add": "ADD"}.get(stype, "")
            title_line = f"## {status_emoji} [{sig['sport'].upper()}] {sig['title']}"
            if tag:
                title_line = f"## {status_emoji} [{tag}] [{sig['sport'].upper()}] {sig['title']}"
            block = [title_line, ""]
            when = game_date.format_game_when(sig, hint)
            if when:
                block.append(f"- **{when}**")
            if sig.get("thesis_label") and stype != "v1":
                block.append(f"- **Thesis**: {sig['thesis_label']}")
            outcome = sig.get("action_outcome") or sig.get("outcome")
            n_sharps = sig.get("consensus_n", 0)
            opposing = sig.get("opposing_n")
            conv_line = (
                f"- **Polymarket consensus**: {n_sharps:.1f} eff. sharps"
                if stype != "v1"
                else f"- **Polymarket consensus**: {n_sharps} sharps vs "
                     f"{opposing} opposing"
            )
            block.extend([
                f"- **BUY**: `{outcome}` side",
                conv_line + f", **{sig['stake_conviction']:.0%}** "
                f"stake-weighted conviction",
                f"- **Polymarket avg entry**: ${sig['consensus_avg_entry']:.3f} | "
                f"**Current**: ${sig['cur_price']:.3f} | "
                f"**Max Kalshi $**: ${sig['max_kalshi_price']:.3f}",
                f"- **Total sharp stake**: ${sig['consensus_stake']:,.0f}",
                f"- **Sharps**: {sharps_str}",
            ])
            if hint:
                tag = "EXACT match" if hint.get("structured") else "fuzzy, verify!"
                block.append(
                    f"- **Kalshi hint** ({tag}): "
                    f"`{hint.get('ticker')}` side `{hint.get('side')}` — "
                    f"\"{(hint.get('title') or '')[:60]}\" "
                    f"(score {hint.get('score', 0):.2f})"
                )
            else:
                block.append(
                    f"- **Kalshi hint**: NONE — search manually for "
                    f"\"{sig['title'][:50]}\" / {sig['outcome']}"
                )
            block.append(
                f"- _first fired {age_min:.0f} min ago, "
                f"refreshed in {rec['cycles_seen']} cycles, "
                f"cid `{sig['cid'][:12]}...`_"
            )
            lines.extend(block)
            lines.append("")

    QUEUE_MD.write_text("\n".join(lines))


def send_catchup_emails(state: dict | None = None) -> int:
    """Email all actionable signals never emailed before. Returns count emailed."""
    if state is None:
        state = load_queue()
    batch = []
    keys = []
    for key, rec in state["signals"].items():
        if rec["status"] not in (STATUS_PENDING, STATUS_ACTIVE):
            continue
        if rec.get("emailed_at"):
            continue
        batch.append((rec["signal"], rec.get("kalshi_hint")))
        keys.append(key)
    if not batch:
        return 0
    ok = notifier.send_digest(batch, label="catch-up")
    if ok:
        now_iso = datetime.now(timezone.utc).isoformat()
        for key in keys:
            state["signals"][key]["emailed_at"] = now_iso
        save_queue(state)
    return len(batch) if ok else 0


def reconcile(state: dict, currently_firing_signals: dict,
              run_resolution_check: bool = True,
              send_email: bool = True) -> dict:
    """
    Main reconciliation entrypoint, call once per cycle.

    currently_firing_signals: dict[signal_key] -> (signal_dict, kalshi_hint)

    Returns summary dict {pending_new, active, stale, resolved_this_cycle}.
    """
    # Upsert all currently firing. Track signals to email this cycle.
    pending_new = 0
    active_refreshed = 0
    to_email = []  # (sig_dict, hint) tuples
    for key, (sig_dict, hint) in currently_firing_signals.items():
        prev = state["signals"].get(key, {}).get("status")
        status_after = upsert_signal(state, sig_dict, hint)
        if prev is None:
            pending_new += 1
            to_email.append((key, sig_dict, hint))
        elif prev == STATUS_STALE:
            # Sharps re-converged — worth alerting again
            to_email.append((key, sig_dict, hint))
        elif status_after == STATUS_ACTIVE:
            active_refreshed += 1

    # Age out signals that didn't refresh
    went_stale = age_unrefreshed_signals(state, set(currently_firing_signals.keys()))

    # Check resolutions on a slice of unresolved signals
    resolved_now = check_resolutions(state) if run_resolution_check else 0

    # Prune
    pruned = prune_old_signals(state, max_age_days=14)

    save_queue(state)
    regenerate_queue_md(state)

    # Email digest for new / reactivated signals
    emailed = False
    email_count = 0
    if send_email and to_email:
        emailed = notifier.send_digest([(s, h) for _, s, h in to_email])
        email_count = len(to_email)
        if emailed:
            now_iso = datetime.now(timezone.utc).isoformat()
            for key, _, _ in to_email:
                if key in state["signals"]:
                    state["signals"][key]["emailed_at"] = now_iso
            save_queue(state)

    # Backtest ledger: immutable snapshot on first fire only
    emailed_keys = {k for k, _, _ in to_email} if emailed else set()
    for key, (sig_dict, hint) in currently_firing_signals.items():
        backtest_ledger.record_opened(
            key, sig_dict, hint,
            emailed=key in emailed_keys,
        )

    return {
        "new": pending_new, "refreshed": active_refreshed,
        "went_stale": went_stale, "resolved": resolved_now,
        "pruned": pruned, "emailed": emailed, "email_count": len(to_email),
    }
