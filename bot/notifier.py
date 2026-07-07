"""
Email notifier for new signals.

Sends a single digest email per cycle containing any signals that just fired
(status=pending, first time we've seen them). Quiet on cycles where nothing
new appears.

Setup (one-time, ~2 minutes):
  1. Gmail account: enable 2-Step Verification at
     https://myaccount.google.com/security
  2. Generate an "App password" at
     https://myaccount.google.com/apppasswords
     (Pick "Mail" and any device name. Copy the 16-char password.)
  3. Add to ~/tail-bot/.env :
       EMAIL_FROM=bollapranav05@gmail.com
       EMAIL_PASSWORD=<16-char-app-password-no-spaces>
       EMAIL_TO=bollapranav05@gmail.com
  4. Test:  python3 -m bot.notifier --test

The .env file is gitignored. If credentials aren't set, the bot logs a
warning and skips email — it keeps running normally.
"""
from __future__ import annotations

import argparse
import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from bot import game_date

logger = logging.getLogger("notifier")

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def _load_env() -> dict:
    """Load credentials from .env or process env. Returns dict, never raises."""
    cfg = {
        "EMAIL_FROM": os.environ.get("EMAIL_FROM"),
        "EMAIL_PASSWORD": os.environ.get("EMAIL_PASSWORD"),
        "EMAIL_TO": os.environ.get("EMAIL_TO"),
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in cfg and not cfg[k]:
                cfg[k] = v
    return cfg


def _is_configured(cfg: dict) -> bool:
    return all(cfg.get(k) for k in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"))


def _normalize_recipients(to_header: str) -> str:
    return ", ".join(addr.strip() for addr in to_header.split(",") if addr.strip())


def send_email(subject: str, body_text: str, body_html: str | None = None) -> bool:
    cfg = _load_env()
    if not _is_configured(cfg):
        logger.info("email not configured (set EMAIL_FROM/PASSWORD/TO in .env "
                    "to enable signal alerts)")
        return False

    msg = EmailMessage()
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = _normalize_recipients(cfg["EMAIL_TO"])
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(cfg["EMAIL_FROM"], cfg["EMAIL_PASSWORD"])
            s.send_message(msg)
        logger.info(f"email sent: {subject!r}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"email auth failed: {e}. Did you use a 16-char Gmail "
                     f"App Password (not your account password)?")
        return False
    except Exception as e:
        logger.exception(f"email send failed: {e}")
        return False


def _fmt_sharps_lines(sig: dict) -> list[str]:
    """One line per contributing sharp: name, stake, entry."""
    lines = []
    for h in sig.get("contributing_sharps", []):
        name = h.get("name", "?")
        stake = h.get("stake", 0)
        price = h.get("avg_price", 0)
        extra = ""
        if sig.get("signal_type") == "add" and h.get("delta"):
            extra = f" (+${h['delta']:,.0f} added)"
        lines.append(f"    • {name}: ${stake:,.0f} @ ${price:.2f}{extra}")
    return lines


def _fmt_sharps_html(sig: dict) -> str:
    rows = []
    for h in sig.get("contributing_sharps", []):
        name = h.get("name", "?")
        stake = h.get("stake", 0)
        price = h.get("avg_price", 0)
        extra = ""
        if sig.get("signal_type") == "add" and h.get("delta"):
            extra = f" &nbsp;(+${h['delta']:,.0f} added)"
        rows.append(
            f"<div style='padding:4px 0;border-bottom:1px solid #f3f4f6'>"
            f"<b style='color:#111827'>{name}</b>"
            f"<span style='color:#374151'> — ${stake:,.0f} @ ${price:.2f}{extra}</span>"
            f"</div>"
        )
    if not rows:
        return ""
    return (
        "<div style='margin-top:10px;padding:10px 12px;background:#f9fafb;"
        "border-radius:6px;border:1px solid #e5e7eb'>"
        "<div style='font-size:12px;font-weight:600;color:#374151;"
        "margin-bottom:6px;text-transform:uppercase;letter-spacing:0.4px'>"
        "Sharps</div>"
        + "".join(rows)
        + "</div>"
    )


def _fmt_signal_text(sig: dict, hint: dict | None) -> str:
    sport = sig["sport"].upper()
    stype = sig.get("signal_type", "v1")
    tag = {"thesis": "THESIS", "cluster": "CLUSTER", "add": "ADD"}.get(stype, "")
    game_line, slug = game_date.format_email_game_when(sig)
    lines = [f"[{sport}] {sig['title']}"]
    if tag:
        lines[0] = f"[{tag}] [{sport}] {sig['title']}"
    if game_line:
        lines.append(f"  {game_line}")
    if slug:
        lines.append(f"  Poly slug: {slug}")
    if sig.get("thesis_label") and stype in ("thesis", "cluster", "add"):
        lines.append(f"  Thesis: {sig['thesis_label']}")
    outcome = sig.get("action_outcome") or sig.get("outcome")
    lines.extend([
        f"  BUY: {outcome}",
        f"  Conviction: {sig['stake_conviction']:.0%} "
        f"({sig.get('consensus_n', 0)} eff. sharps, "
        f"${sig['consensus_stake']:,.0f} stake)",
        f"  Poly avg entry: ${sig['consensus_avg_entry']:.3f} | "
        f"Current: ${sig['cur_price']:.3f} | "
        f"Max Kalshi $: ${sig['max_kalshi_price']:.3f}",
    ])
    if stype == "add" and sig.get("delta"):
        lines.append(f"  Added: +${sig['delta']:,.0f} (was ${sig.get('prev_stake', 0):,.0f})")
    sharp_lines = _fmt_sharps_lines(sig)
    if sharp_lines:
        lines.append("  Sharps:")
        lines.extend(sharp_lines)
    return "\n".join(lines)


def _fmt_signal_html(sig: dict, hint: dict | None) -> str:
    sport = sig["sport"].upper()
    sport_color = {
        "MLB": "#1f6feb", "NHL": "#7c3aed",
        "SOCCER": "#16a34a", "NFL": "#ea580c", "NBA": "#f59e0b",
    }.get(sport, "#374151")

    game_line, slug = game_date.format_email_game_when(sig)
    when_html = ""
    if game_line:
        when_html += (
            f"<div style='font-size:13px;color:#4b5563;margin-bottom:4px'>"
            f"{game_line}</div>"
        )
    if slug:
        when_html += (
            f"<div style='font-size:12px;color:#6b7280;margin-bottom:6px'>"
            f"Poly slug: <code>{slug}</code></div>"
        )

    stype = sig.get("signal_type", "v1")
    tag_html = ""
    if stype == "thesis":
        tag_html = "<span style='background:#059669;color:white;padding:2px 8px;border-radius:4px;font-size:10px;margin-right:6px'>THESIS</span>"
    elif stype == "cluster":
        tag_html = "<span style='background:#2563eb;color:white;padding:2px 8px;border-radius:4px;font-size:10px;margin-right:6px'>CLUSTER</span>"
    elif stype == "add":
        tag_html = "<span style='background:#d97706;color:white;padding:2px 8px;border-radius:4px;font-size:10px;margin-right:6px'>ADD</span>"

    outcome = sig.get("action_outcome") or sig.get("outcome")
    thesis_line = ""
    if sig.get("thesis_label") and stype != "v1":
        thesis_line = f"<div style='font-size:13px;color:#374151;margin:4px 0'>{sig['thesis_label']}</div>"

    sharps_html = _fmt_sharps_html(sig)

    return f"""
<div style='border:1px solid #e5e7eb;border-radius:8px;padding:14px;
            margin-bottom:14px;background:#ffffff;font-family:-apple-system,
            BlinkMacSystemFont,sans-serif'>
  <div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>
    {tag_html}<span style='background:{sport_color};color:white;padding:3px 10px;
                 border-radius:4px;font-size:11px;font-weight:600;
                 letter-spacing:0.5px'>{sport}</span>
    <span style='font-size:15px;font-weight:600;color:#111827'>
      {sig['title']}</span>
  </div>
  {when_html}
  {thesis_line}
  <div style='font-size:18px;margin:8px 0;color:#065f46'>
    BUY <b>{outcome}</b>
  </div>
  <div style='font-size:13px;color:#374151'>
    <b>{sig['stake_conviction']:.0%}</b> conviction &nbsp;|&nbsp;
    {sig['consensus_n']} sharps &nbsp;|&nbsp;
    ${sig['consensus_stake']:,.0f} stake
  </div>
  <div style='font-size:13px;color:#374151;margin-top:4px'>
    Poly avg <b>${sig['consensus_avg_entry']:.3f}</b> &nbsp;|&nbsp;
    now <b>${sig['cur_price']:.3f}</b> &nbsp;|&nbsp;
    <span style='color:#b45309'>max bet <b>${sig['max_kalshi_price']:.3f}</b></span>
  </div>
  {sharps_html}
</div>
"""


def send_digest(new_signals: list[tuple[dict, dict | None]],
                label: str = "new") -> bool:
    """Send a single digest email for signals in this batch.

    label: used in subject line — 'new', 'reactivated', or 'catch-up'.
    """
    if not new_signals:
        return False

    n = len(new_signals)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[tail-bot] {n} {label} signal{'s' if n != 1 else ''} -- {now}"

    text_lines = [
        f"Tail-bot: {n} {label} signal{'s' if n != 1 else ''} at {now}.",
        "",
        "Place small ($10) test units on whichever look right.",
        "",
    ]
    for sig, hint in new_signals:
        text_lines.append(_fmt_signal_text(sig, hint))
        text_lines.append("")
    text_lines.append("---")
    text_lines.append("Full queue: python3 -m bot.status")

    html_body = f"""
<html><body style='background:#f9fafb;padding:20px;font-family:-apple-system,
       BlinkMacSystemFont,sans-serif'>
  <div style='max-width:680px;margin:0 auto'>
    <h2 style='color:#111827;margin-bottom:6px'>
      {n} {label} signal{'s' if n != 1 else ''}
    </h2>
    <div style='color:#6b7280;font-size:13px;margin-bottom:18px'>
      {now} &nbsp;|&nbsp; consensus-tail bot
    </div>
    {''.join(_fmt_signal_html(sig, hint) for sig, hint in new_signals)}
    <div style='font-size:12px;color:#6b7280;margin-top:18px;
                padding-top:14px;border-top:1px solid #e5e7eb'>
      Place $10 units on whichever you like. Full queue:
      <code>python3 -m bot.status</code>
    </div>
  </div>
</body></html>
"""
    return send_email(subject, "\n".join(text_lines), html_body)


def _test_mode():
    """Send a test email to verify setup."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    cfg = _load_env()
    if not _is_configured(cfg):
        print("\nEmail is NOT configured. To enable:")
        print(f"  1. Create {ENV_FILE} with these lines:")
        print("       EMAIL_FROM=bollapranav05@gmail.com")
        print("       EMAIL_PASSWORD=<16-char Gmail app password>")
        print("       EMAIL_TO=bollapranav05@gmail.com")
        print("  2. Generate the app password at")
        print("     https://myaccount.google.com/apppasswords")
        print("     (Requires 2-Step Verification on the Gmail account.)")
        print(f"  3. Re-run: python3 -m bot.notifier --test\n")
        return

    print(f"  EMAIL_FROM:  {cfg['EMAIL_FROM']}")
    print(f"  EMAIL_TO:    {cfg['EMAIL_TO']}")
    print(f"  EMAIL_PASS:  {'*' * (len(cfg['EMAIL_PASSWORD']) - 2) + cfg['EMAIL_PASSWORD'][-2:]}")
    print("  Sending test signal digest...")

    fake_sig = {
        "signal_type": "thesis",
        "cid": "0xtest",
        "outcome": "Yes",
        "action_outcome": "Yes",
        "title": "Will Argentina win on 2026-06-16",
        "sport": "soccer",
        "slug": "fifwc-arg-col-2026-06-16",
        "thesis_label": "Argentina Win (YES side)",
        "game_start_time": "2026-06-16 02:10:00+00",
        "consensus_n": 2.0,
        "consensus_stake": 1324858,
        "stake_conviction": 1.0,
        "consensus_avg_entry": 0.638,
        "cur_price": 0.825,
        "max_kalshi_price": 0.658,
        "contributing_sharps": [
            {"name": "surfandturf", "stake": 437846, "avg_price": 0.63},
            {"name": "Latina", "stake": 887012, "avg_price": 0.64},
        ],
    }
    fake_hint = {
        "ticker": "KXMLBGAME-26JUN171510TBLAD-LAD",
        "side": "yes",
        "structured": True,
        "score": 1.0,
    }
    ok = send_digest([(fake_sig, fake_hint)])
    print(f"  result: {'OK -- check inbox' if ok else 'FAILED'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="send a test digest to verify Gmail setup")
    args = ap.parse_args()
    if args.test:
        _test_mode()
    else:
        ap.print_help()
