"""
Structured MLB matcher: Polymarket signal -> Kalshi KXMLBGAME ticker.

Polymarket title format (moneyline):
    "Tampa Bay Rays vs. Los Angeles Dodgers"
    outcome = "Los Angeles Dodgers" (the team we bet to win)

Kalshi ticker format:
    KXMLBGAME-{YYMMMDD}{HHMM}{AWAY_ABBR}{HOME_ABBR}-{BET_ABBR}
    e.g. KXMLBGAME-26JUN182140LAAATH-LAA

We map full team names -> Kalshi abbreviations, then scan candidate tickers
for ones where (a) BET_ABBR matches the bet team and (b) the other team
appears in the game-id segment. Returns None if confidence is low (e.g.
spread bets, totals, futures — anything that's not a clean moneyline).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from bot import game_date

logger = logging.getLogger("matcher_mlb")

# Polymarket full name -> Kalshi MLB abbreviation
TEAM_TO_KALSHI = {
    "arizona diamondbacks": "AZ",
    "atlanta braves": "ATL",
    "baltimore orioles": "BAL",
    "boston red sox": "BOS",
    "chicago cubs": "CHC",
    "chicago white sox": "CWS",
    "cincinnati reds": "CIN",
    "cleveland guardians": "CLE",
    "colorado rockies": "COL",
    "detroit tigers": "DET",
    "houston astros": "HOU",
    "kansas city royals": "KC",
    "los angeles angels": "LAA",
    "los angeles dodgers": "LAD",
    "miami marlins": "MIA",
    "milwaukee brewers": "MIL",
    "minnesota twins": "MIN",
    "new york mets": "NYM",
    "new york yankees": "NYY",
    "oakland athletics": "ATH",
    "athletics": "ATH",
    "philadelphia phillies": "PHI",
    "pittsburgh pirates": "PIT",
    "san diego padres": "SD",
    "san francisco giants": "SF",
    "seattle mariners": "SEA",
    "st. louis cardinals": "STL",
    "st louis cardinals": "STL",
    "tampa bay rays": "TB",
    "texas rangers": "TEX",
    "toronto blue jays": "TOR",
    "washington nationals": "WSH",
}

# Reverse for sanity-check
KALSHI_TO_TEAM = {v: k for k, v in TEAM_TO_KALSHI.items()}

# Title patterns we DO handle (moneyline). Spreads, totals, futures excluded.
MONEYLINE_RE = re.compile(r"^(.+?)\s+(?:vs\.?|@)\s+(.+?)\s*$", re.IGNORECASE)


def _team_to_abbr(name: str) -> str | None:
    if not name:
        return None
    n = name.strip().lower()
    n = n.rstrip(".").strip()
    return TEAM_TO_KALSHI.get(n)


def _parse_moneyline_title(title: str) -> tuple[str, str] | None:
    """Parse 'Team A vs. Team B' or 'Team A @ Team B' -> (team_a, team_b)."""
    title = title.strip()
    if title.lower().startswith("spread:") or title.lower().startswith("total"):
        return None  # not a moneyline
    m = MONEYLINE_RE.match(title)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _parse_kalshi_ticker(ticker: str) -> dict | None:
    """KXMLBGAME-26JUN182140LAAATH-LAA -> {away, home, bet}"""
    if not ticker.startswith("KXMLBGAME-"):
        return None
    parts = ticker.split("-")
    if len(parts) != 3:
        return None
    game_id = parts[1]
    bet_abbr = parts[2].upper()
    # game_id = YYMMMDDHHMM + AWAY_ABBR + HOME_ABBR. Date prefix length: 9 chars
    # (2yy + 3mon + 2dd + 4time)... wait let me recount: 26JUN182140 = 11 chars
    if len(game_id) < 13:
        return None
    teams_part = game_id[11:]  # drop YYMMMDDHHMM
    # teams_part is AWAY+HOME concatenated. Both 2 or 3 chars each.
    # Try possible splits and check whether BET appears as a suffix.
    candidates = []
    for split in (2, 3):
        if split >= len(teams_part):
            continue
        away = teams_part[:split]
        home = teams_part[split:]
        if away in KALSHI_TO_TEAM and home in KALSHI_TO_TEAM:
            candidates.append((away, home))
    if not candidates:
        return None
    # Prefer the split that includes BET as either side
    for away, home in candidates:
        if bet_abbr in (away, home):
            return {"away": away, "home": home, "bet": bet_abbr,
                    "game_id": game_id}
    away, home = candidates[0]
    return {"away": away, "home": home, "bet": bet_abbr, "game_id": game_id}


def match_mlb(title: str, outcome: str, kalshi_markets: list[dict],
              game_start_time: str | None = None) -> dict | None:
    """
    Find the KXMLBGAME ticker corresponding to (title, outcome).
    Returns:
      {
        ticker, side='yes', price_to_pay (cents->dollars), title, score,
        away, home, bet_team, structured=True
      }
    or None if no confident match.
    """
    parsed_title = _parse_moneyline_title(title)
    if not parsed_title:
        return None
    team_a_name, team_b_name = parsed_title
    team_a = _team_to_abbr(team_a_name)
    team_b = _team_to_abbr(team_b_name)
    bet_abbr = _team_to_abbr(outcome)
    if not (team_a and team_b and bet_abbr):
        logger.debug(f"  mlb: couldn't map all teams in {title!r} / {outcome!r}")
        return None
    if bet_abbr not in (team_a, team_b):
        logger.debug(f"  mlb: outcome {outcome!r} not one of the two teams")
        return None

    teams_set = {team_a, team_b}
    target_et = game_date.parse_poly_game_start(game_start_time) if game_start_time else None

    candidates: list[tuple[dict, dict, datetime | None]] = []
    for m in kalshi_markets:
        ticker = m.get("ticker", "")
        if not ticker.startswith("KXMLBGAME-"):
            continue
        parsed = _parse_kalshi_ticker(ticker)
        if not parsed:
            continue
        if parsed["bet"] != bet_abbr:
            continue
        if {parsed["away"], parsed["home"]} != teams_set:
            continue
        k_dt = game_date.kalshi_ticker_datetime(ticker)
        candidates.append((m, parsed, k_dt))

    if not candidates:
        return None

    if target_et and any(c[2] for c in candidates):
        # Same teams can play 3x in a series — pick closest start time.
        def _delta(c):
            k_dt = c[2]
            if not k_dt:
                return float("inf")
            return abs((k_dt - target_et).total_seconds())

        market, parsed, _ = min(candidates, key=_delta)
    else:
        market, parsed, _ = candidates[0]
    # Pull ask price for YES (we want to BUY YES on the bet team's ticker).
    yes_ask = market.get("yes_ask")  # in cents
    yes_bid = market.get("yes_bid")
    price_to_pay = (yes_ask / 100.0) if isinstance(yes_ask, (int, float)) else None
    return {
        "ticker": market["ticker"],
        "title": market.get("title", ""),
        "side": "yes",
        "price_to_pay": price_to_pay,
        "yes_ask_cents": yes_ask,
        "yes_bid_cents": yes_bid,
        "score": 1.0,
        "structured": True,
        "matcher": "mlb",
        "away": parsed["away"],
        "home": parsed["home"],
        "bet_team": parsed["bet"],
    }
