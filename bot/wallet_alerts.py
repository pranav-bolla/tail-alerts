"""
Email when a Polymarket wallet places a new trade.

Configure watches in .env (see .env.example):
  WALLET_ALERTS=matanovik:0x39d3...,otheruser:0xabc...

Run:
    python3 -m bot.wallet_alerts --one-shot          # smoke test (no email on seed)
    python3 -m bot.wallet_alerts --one-shot --email  # poll once, email if new
    python3 -m bot.wallet_alerts                     # poll every 90s forever
    python3 -m bot.wallet_alerts --test-email        # send sample email
    python3 -m bot.wallet_alerts --username foo --wallet 0x...  # override env
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot import notifier

TRADES_URL = "https://data-api.polymarket.com/trades"
POSITIONS_URL = "https://data-api.polymarket.com/positions"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

DEFAULT_WALLET = "0x39d3c773be30fcc73161fc6768f46d563a779ef0"
DEFAULT_USERNAME = "matanovik"

logger = logging.getLogger("wallet_alerts")


def _env_get(key: str) -> str | None:
    """Read a key from process env or .env file."""
    val = os.environ.get(key)
    if val:
        return val.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return None


def parse_watch(entry: str) -> dict | None:
    """Parse 'username:0xwallet' from a WALLET_ALERTS entry."""
    entry = entry.strip()
    if not entry:
        return None
    if ":" not in entry:
        logger.warning(f"invalid watch entry (need username:wallet): {entry!r}")
        return None
    username, wallet = entry.split(":", 1)
    username = username.strip().lstrip("@")
    wallet = wallet.strip()
    if not username or not wallet.startswith("0x"):
        logger.warning(f"invalid watch entry: {entry!r}")
        return None
    return {"username": username, "wallet": wallet}


def load_watches() -> list[dict]:
    """Load wallet watches from WALLET_ALERTS or legacy single-user env vars."""
    watches: list[dict] = []
    raw = _env_get("WALLET_ALERTS")
    if raw:
        for part in raw.split(","):
            watch = parse_watch(part)
            if watch:
                watches.append(watch)

    if not watches:
        username = (_env_get("WALLET_ALERT_USERNAME") or DEFAULT_USERNAME).lstrip("@")
        wallet = _env_get("WALLET_ALERT_WALLET") or DEFAULT_WALLET
        watches.append({"username": username, "wallet": wallet})

    return watches


def resolve_watches(args) -> list[dict]:
    """CLI --username/--wallet override env when either is explicitly set."""
    if args.username is not None or args.wallet is not None:
        return [{
            "username": (args.username or DEFAULT_USERNAME).lstrip("@"),
            "wallet": args.wallet or DEFAULT_WALLET,
        }]
    return load_watches()


def state_path(username: str) -> Path:
    safe = username.lower().replace("@", "")
    return DATA_DIR / f"wallet_alert_{safe}.json"


def load_state(username: str) -> dict:
    p = state_path(username)
    if not p.exists():
        return {"seen_tx": [], "username": username, "initialized": False}
    with open(p) as f:
        return json.load(f)


def save_state(state: dict, username: str) -> None:
    p = state_path(username)
    tmp = p.with_suffix(".json.tmp")
    # keep last 5000 tx hashes
    seen = state.get("seen_tx", [])
    if len(seen) > 5000:
        state["seen_tx"] = seen[-5000:]
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def fetch_recent_trades(wallet: str, limit: int = 50) -> list[dict]:
    try:
        r = requests.get(
            TRADES_URL,
            params={"user": wallet, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"trades fetch failed: {e}")
        return []


def fetch_open_positions(wallet: str, limit: int = 500) -> list[dict]:
    """Return unresolved positions (curPrice > 0, size > 0), largest first."""
    try:
        r = requests.get(
            POSITIONS_URL,
            params={"user": wallet, "sizeThreshold": 0, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        positions = r.json()
    except Exception as e:
        logger.warning(f"positions fetch failed: {e}")
        return []

    open_pos = []
    for p in positions:
        try:
            size = float(p.get("size") or 0)
            cur = float(p.get("curPrice") or 0)
        except (TypeError, ValueError):
            continue
        if size > 0 and cur > 0:
            open_pos.append(p)

    open_pos.sort(
        key=lambda p: float(p.get("currentValue") or 0),
        reverse=True,
    )
    return open_pos


def trade_key(t: dict) -> str:
    tx = t.get("transactionHash")
    if tx:
        return tx
    return (
        f"{t.get('timestamp')}::{t.get('conditionId')}::"
        f"{t.get('outcome')}::{t.get('side')}::{t.get('size')}::{t.get('price')}"
    )


def fmt_trade_text(username: str, t: dict) -> str:
    side = (t.get("side") or "?").upper()
    outcome = t.get("outcome") or "?"
    title = t.get("title") or "?"
    price = float(t.get("price") or 0)
    size = float(t.get("size") or 0)
    stake = price * size
    slug = t.get("eventSlug") or t.get("slug") or ""
    ts = t.get("timestamp")
    when = ""
    if ts:
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except (TypeError, ValueError, OSError):
            pass
    lines = [
        f"@{username} — {side} {outcome}",
        f"  {title}",
        f"  ${stake:,.0f} ({size:,.0f} shares @ ${price:.3f})",
    ]
    if when:
        lines.append(f"  {when}")
    if slug:
        lines.append(f"  https://polymarket.com/event/{slug}")
    return "\n".join(lines)


def fmt_trade_html(username: str, t: dict) -> str:
    side = (t.get("side") or "?").upper()
    outcome = t.get("outcome") or "?"
    title = t.get("title") or "?"
    price = float(t.get("price") or 0)
    size = float(t.get("size") or 0)
    stake = price * size
    slug = t.get("eventSlug") or t.get("slug") or ""
    side_color = "#065f46" if side == "BUY" else "#b45309"
    link = f"https://polymarket.com/event/{slug}" if slug else ""
    link_html = (
        f"<div style='margin-top:6px;font-size:12px'>"
        f"<a href='{link}'>{slug}</a></div>"
        if link else ""
    )
    return f"""
<div style='border:1px solid #e5e7eb;border-radius:8px;padding:14px;
            margin-bottom:12px;background:#fff;font-family:sans-serif'>
  <div style='font-size:12px;color:#6b7280;margin-bottom:4px'>@{username}</div>
  <div style='font-size:16px;font-weight:600;color:{side_color}'>
    {side} <span style='color:#111827'>{outcome}</span>
  </div>
  <div style='font-size:14px;color:#374151;margin:6px 0'>{title}</div>
  <div style='font-size:13px;color:#374151'>
    <b>${stake:,.0f}</b> &nbsp;({size:,.0f} sh @ ${price:.3f})
  </div>
  {link_html}
</div>
"""


def fmt_position_text(p: dict) -> str:
    outcome = p.get("outcome") or "?"
    title = p.get("title") or "?"
    try:
        size = float(p.get("size") or 0)
        avg = float(p.get("avgPrice") or 0)
        cur = float(p.get("curPrice") or 0)
        value = float(p.get("currentValue") or size * cur)
        pnl = float(p.get("cashPnl") or 0)
    except (TypeError, ValueError):
        size = avg = cur = value = pnl = 0
    slug = p.get("eventSlug") or p.get("slug") or ""
    lines = [
        f"  {outcome} — {title}",
        f"    ${value:,.0f} value ({size:,.0f} sh @ avg ${avg:.3f}, now ${cur:.3f})",
    ]
    if pnl:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"    PnL: {sign}${pnl:,.0f}")
    if slug:
        lines.append(f"    https://polymarket.com/event/{slug}")
    return "\n".join(lines)


def fmt_position_html(p: dict) -> str:
    outcome = p.get("outcome") or "?"
    title = p.get("title") or "?"
    try:
        size = float(p.get("size") or 0)
        avg = float(p.get("avgPrice") or 0)
        cur = float(p.get("curPrice") or 0)
        value = float(p.get("currentValue") or size * cur)
        pnl = float(p.get("cashPnl") or 0)
    except (TypeError, ValueError):
        size = avg = cur = value = pnl = 0
    slug = p.get("eventSlug") or p.get("slug") or ""
    pnl_color = "#065f46" if pnl >= 0 else "#b45309"
    pnl_html = ""
    if pnl:
        sign = "+" if pnl >= 0 else ""
        pnl_html = (
            f"<div style='font-size:12px;color:{pnl_color};margin-top:4px'>"
            f"PnL: {sign}${pnl:,.0f}</div>"
        )
    link_html = ""
    if slug:
        link_html = (
            f"<div style='margin-top:6px;font-size:12px'>"
            f"<a href='https://polymarket.com/event/{slug}'>{slug}</a></div>"
        )
    return f"""
<div style='border:1px solid #e5e7eb;border-radius:6px;padding:10px 12px;
            margin-bottom:8px;background:#fff;font-family:sans-serif'>
  <div style='font-size:14px;font-weight:600;color:#111827'>
    {outcome} <span style='font-weight:400;color:#374151'>— {title}</span>
  </div>
  <div style='font-size:13px;color:#374151;margin-top:4px'>
    <b>${value:,.0f}</b> value &nbsp;({size:,.0f} sh @ ${avg:.3f} → ${cur:.3f})
  </div>
  {pnl_html}
  {link_html}
</div>
"""


def _fmt_positions_section_text(username: str, positions: list[dict]) -> list[str]:
    lines = [
        f"@{username} open positions ({len(positions)}):",
        "",
    ]
    if positions:
        for p in positions:
            lines.append(fmt_position_text(p))
            lines.append("")
    else:
        lines.append("  (none)")
        lines.append("")
    return lines


def _fmt_positions_section_html(username: str, positions: list[dict]) -> str:
    if positions:
        body = "".join(fmt_position_html(p) for p in positions)
    else:
        body = (
            "<div style='color:#6b7280;font-size:13px;padding:8px 0'>"
            "(none)</div>"
        )
    return f"""
<div style='margin-top:24px;padding-top:20px;border-top:2px solid #e5e7eb'>
  <h3 style='color:#111827;margin:0 0 12px;font-size:16px'>
    Open positions ({len(positions)})
  </h3>
  {body}
</div>
"""


def send_trade_digest(
    username: str,
    trades: list[dict],
    *,
    wallet: str | None = None,
    positions: list[dict] | None = None,
) -> bool:
    if not trades:
        return False
    n = len(trades)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = (
        f"[tail-bot] @{username}: {n} new trade{'s' if n != 1 else ''} — {now}"
    )
    if positions is None and wallet:
        positions = fetch_open_positions(wallet)

    text_lines = [
        f"@{username} placed {n} new trade{'s' if n != 1 else ''} at {now}.",
        "",
    ]
    for t in trades:
        text_lines.append(fmt_trade_text(username, t))
        text_lines.append("")

    if positions is not None:
        text_lines.append("---")
        text_lines.append("")
        text_lines.extend(_fmt_positions_section_text(username, positions))

    positions_html = ""
    if positions is not None:
        positions_html = _fmt_positions_section_html(username, positions)

    html = f"""
<html><body style='background:#f9fafb;padding:20px;font-family:sans-serif'>
  <h2>@{username}: {n} new trade{'s' if n != 1 else ''}</h2>
  <div style='color:#6b7280;font-size:13px;margin-bottom:16px'>{now}</div>
  {''.join(fmt_trade_html(username, t) for t in trades)}
  {positions_html}
</body></html>
"""
    return notifier.send_email(subject, "\n".join(text_lines), html)


def poll_once(
    wallet: str,
    username: str,
    *,
    send_email: bool = True,
    seed_only: bool = False,
) -> dict:
    """Fetch trades, return new ones since last poll. Optionally email."""
    state = load_state(username)
    seen = set(state.get("seen_tx", []))
    trades = fetch_recent_trades(wallet)
    if not trades:
        return {"new": 0, "emailed": False}

    # API returns newest first
    new_trades = []
    for t in trades:
        key = trade_key(t)
        if key not in seen:
            new_trades.append(t)

    if not state.get("initialized"):
        # First run: record current trades, don't alert (avoid spam on startup)
        for t in trades:
            seen.add(trade_key(t))
        state["seen_tx"] = sorted(seen)
        state["initialized"] = True
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state, username)
        logger.info(f"seeded {len(trades)} recent trades for @{username} (no email)")
        return {"new": 0, "seeded": len(trades), "emailed": False}

    if seed_only:
        for t in new_trades:
            seen.add(trade_key(t))
        state["seen_tx"] = sorted(seen)
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state, username)
        return {"new": len(new_trades), "emailed": False}

    if not new_trades:
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state, username)
        return {"new": 0, "emailed": False}

    # Oldest first in email
    new_trades.sort(key=lambda x: int(x.get("timestamp") or 0))
    emailed = False
    if send_email:
        emailed = send_trade_digest(username, new_trades, wallet=wallet)

    for t in new_trades:
        seen.add(trade_key(t))
    state["seen_tx"] = sorted(seen)
    state["last_poll"] = datetime.now(timezone.utc).isoformat()
    save_state(state, username)

    return {"new": len(new_trades), "emailed": emailed}


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"wallet_alerts_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
        force=True,
    )


def log_startup_status(watches: list[dict], poll_seconds: int) -> None:
    """Log config summary to stdout (visible in Railway deploy logs)."""
    from bot.notifier import _load_env, _is_configured

    cfg = _load_env()
    logger.info("=== wallet_alerts startup ===")
    logger.info("email configured: %s", _is_configured(cfg))
    if _is_configured(cfg):
        logger.info("email from: %s -> to: %s", cfg["EMAIL_FROM"], cfg["EMAIL_TO"])
    else:
        missing = [
            k for k in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO")
            if not cfg.get(k)
        ]
        logger.error("missing email env vars: %s", ", ".join(missing))

    for watch in watches:
        state = load_state(watch["username"])
        logger.info(
            "@%s wallet=%s... initialized=%s seen_tx=%d",
            watch["username"],
            watch["wallet"][:10],
            state.get("initialized"),
            len(state.get("seen_tx", [])),
        )
    logger.info(
        "polling every %ss — alerts only for NEW trades after seed",
        poll_seconds,
    )


def main():
    ap = argparse.ArgumentParser(description="Email on new Polymarket trades for a wallet")
    ap.add_argument("--wallet", default=None,
                    help="override env — single wallet to watch")
    ap.add_argument("--username", default=None,
                    help="override env — single username to watch")
    ap.add_argument("--poll-seconds", type=int, default=90,
                    help="seconds between polls (default 90)")
    ap.add_argument("--one-shot", action="store_true")
    ap.add_argument("--email", action="store_true",
                    help="with --one-shot, send email if new trades (after seed)")
    ap.add_argument("--reset", action="store_true",
                    help="clear seen state and re-seed on next run")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    setup_logging(log_dir)

    watches = resolve_watches(args)
    if not watches:
        logger.error("no wallet watches configured — set WALLET_ALERTS in .env")
        sys.exit(1)

    if args.reset:
        for watch in watches:
            p = state_path(watch["username"])
            if p.exists():
                p.unlink()
                logger.info(f"cleared state for @{watch['username']}")

    if args.test_email:
        watch = watches[0]
        sample = fetch_recent_trades(watch["wallet"], limit=1)
        if not sample:
            print("no trades found for test email")
            sys.exit(1)
        ok = send_trade_digest(
            watch["username"], sample, wallet=watch["wallet"],
        )
        print("test email:", "OK" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    labels = ", ".join(
        f"@{w['username']} ({w['wallet'][:10]}...)" for w in watches
    )
    logger.info(f"watching {len(watches)} wallet(s): {labels}")
    log_startup_status(watches, args.poll_seconds)

    if args.one_shot:
        for watch in watches:
            result = poll_once(
                watch["wallet"], watch["username"],
                send_email=args.email,
            )
            logger.info(f"  @{watch['username']}: {result}")
        return

    while True:
        try:
            for watch in watches:
                result = poll_once(
                    watch["wallet"], watch["username"], send_email=True,
                )
                if result.get("new"):
                    logger.info(
                        f"  @{watch['username']}: {result['new']} new trade(s)"
                        + (" [emailed]" if result.get("emailed") else "")
                    )
        except Exception as e:
            logger.exception(f"poll error: {e}")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
