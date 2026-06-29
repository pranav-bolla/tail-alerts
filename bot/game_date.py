"""Extract human-readable game date/time from Polymarket slugs and Kalshi tickers."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

SLUG_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%b %-d, %Y") if dt.day < 10 else dt.strftime("%b %d, %Y")


def _fmt_time_et(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p ET")


def from_poly_slug(slug: str) -> str | None:
    """mlb-tb-lad-2026-06-15 -> 'Jun 15, 2026'

    For MLB this is the *series/event* anchor date, NOT always first pitch.
    Prefer game_start_time when available.
    """
    if not slug:
        return None
    m = SLUG_DATE_RE.search(slug)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d")
        return _fmt_date(dt)
    except ValueError:
        return m.group(1)


def parse_poly_game_start(iso_str: str) -> datetime | None:
    """Polymarket gameStartTime -> timezone-aware datetime (ET for display)."""
    if not iso_str:
        return None
    try:
        s = iso_str.strip().replace("Z", "+00:00")
        if s.endswith("+00"):
            s = s[:-3] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ET)
    except (ValueError, TypeError):
        return None


def from_poly_game_start(iso_str: str) -> str | None:
    dt = parse_poly_game_start(iso_str)
    if not dt:
        return None
    return f"{_fmt_date(dt)} {_fmt_time_et(dt)}"


def kalshi_ticker_datetime(ticker: str) -> datetime | None:
    """Parse Kalshi KXMLBGAME ticker datetime (stored as Eastern time)."""
    if not ticker or "-" not in ticker:
        return None
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    game_id = parts[1]
    if len(game_id) < 11:
        return None
    yy = int(game_id[0:2])
    mon = game_id[2:5].upper()
    dd = int(game_id[5:7])
    hhmm = game_id[7:11]
    if mon not in MONTHS:
        return None
    try:
        hour = int(hhmm[0:2])
        minute = int(hhmm[2:4])
    except ValueError:
        hour, minute = 0, 0
    return datetime(2000 + yy, MONTHS[mon], dd, hour, minute, tzinfo=ET)


def from_kalshi_ticker(ticker: str) -> str | None:
    """KXMLBGAME-26JUN171510TBLAD-LAD -> 'Jun 17, 2026 3:10 PM ET'"""
    dt = kalshi_ticker_datetime(ticker)
    if not dt:
        return None
    if dt.hour or dt.minute:
        return f"{_fmt_date(dt)} {_fmt_time_et(dt)}"
    return _fmt_date(dt)


def fetch_game_start_times(cids: list[str]) -> dict[str, str]:
    """Batch-fetch Polymarket gameStartTime for condition IDs."""
    out: dict[str, str] = {}
    for cid in cids:
        if not cid or cid in out:
            continue
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": cid},
                timeout=10,
            )
            r.raise_for_status()
            markets = r.json()
            if markets:
                gst = markets[0].get("gameStartTime")
                if gst:
                    out[cid] = gst
        except Exception:
            pass
    return out


def format_email_game_when(sig: dict) -> tuple[str | None, str | None]:
    """Game info for emails — Polymarket only (no Kalshi).

    Returns (game_line, slug_line) e.g. ('Game: Jun 15, 2026 10:10 PM ET', 'mlb-tb-lad-2026-06-15').
    """
    sport = (sig.get("sport") or "").lower()
    game_line = None

    poly_start = from_poly_game_start(sig.get("game_start_time") or "")
    if poly_start:
        game_line = f"Game: {poly_start}"
    elif sport == "soccer":
        poly = from_poly_slug(sig.get("slug") or "")
        if poly:
            game_line = f"Game: {poly}"

    slug_line = sig.get("slug") or None
    return game_line, slug_line


def format_game_when(sig: dict, hint: dict | None) -> str | None:
    """Best available game date line for display."""
    sport = (sig.get("sport") or "").lower()

    if sport == "mlb":
        # Slug date is a series anchor — don't show it as game time.
        kalshi = from_kalshi_ticker((hint or {}).get("ticker") or "")
        poly_start = from_poly_game_start(sig.get("game_start_time") or "")
        if kalshi:
            return f"Game: {kalshi}"
        if poly_start:
            return f"Game: {poly_start}"
        return None

    if sport in ("nhl", "nba", "nfl"):
        kalshi = from_kalshi_ticker((hint or {}).get("ticker") or "")
        poly_start = from_poly_game_start(sig.get("game_start_time") or "")
        if kalshi:
            return f"Game: {kalshi}"
        if poly_start:
            return f"Game: {poly_start}"
        poly = from_poly_slug(sig.get("slug") or "")
        if poly:
            return f"Game: {poly}"
        return None

    # Soccer: slug date matches "Will X win on YYYY-MM-DD?" in the title.
    poly = from_poly_slug(sig.get("slug") or "")
    if poly:
        return f"Game: {poly}"
    return None
