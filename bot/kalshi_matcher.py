"""
Match Polymarket signals to Kalshi tickers.

Wraps Poly-Monitor's matcher.py + kalshi.py. Adds outcome-translation logic
(Polymarket "Pittsburgh" -> Kalshi YES on the Pittsburgh side).

Caches Kalshi market list with TTL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

# Wire in the existing Poly-Monitor modules
sys.path.insert(0, "/home/pranav/Poly-Monitor")
from matcher import calculate_similarity, extract_entities  # noqa: E402
from kalshi import fetch_all_markets, build_keyword_index  # noqa: E402

# Structured matchers (high precision per-sport).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import matcher_mlb  # noqa: E402

logger = logging.getLogger("kalshi_matcher")

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "kalshi_markets.json"
CACHE_TTL_SECONDS = 600  # 10 minutes — markets and prices both move fast

MIN_SIMILARITY = 0.55


def normalize_outcome(text: str) -> str:
    return re.sub(r"[^\w]", "", (text or "").lower())


async def fetch_open_markets_async(series_prefixes: list[str] | None = None) -> list[dict]:
    """Fetch only sport markets via series_ticker prefix filter."""
    if not series_prefixes:
        return await fetch_all_markets(status="open")
    out = []
    for prefix in series_prefixes:
        markets = await _fetch_markets_by_series(prefix)
        out.extend(markets)
    return out


async def _fetch_markets_by_series(series_ticker: str) -> list[dict]:
    """Paginate /markets with series_ticker filter. Retries on 429."""
    import aiohttp
    base = "https://api.elections.kalshi.com/trade-api/v2"
    markets = []
    cursor = None
    async with aiohttp.ClientSession() as session:
        while True:
            params = {"limit": 1000, "status": "open",
                      "series_ticker": series_ticker}
            if cursor:
                params["cursor"] = cursor

            # Retry-with-backoff on 429
            for attempt in range(5):
                try:
                    async with session.get(f"{base}/markets", params=params) as r:
                        if r.status == 429:
                            wait = 2 ** (attempt + 1)
                            logger.info(f"  429 on {series_ticker}, sleeping {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        if r.status == 404:
                            return []  # series doesn't exist
                        r.raise_for_status()
                        data = await r.json()
                        break
                except aiohttp.ClientResponseError as e:
                    if attempt == 4:
                        logger.warning(f"  giving up on {series_ticker}: {e}")
                        return markets
                    wait = 2 ** (attempt + 1)
                    await asyncio.sleep(wait)
            else:
                break

            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)  # be polite between series
    return markets


# Kalshi series tickers — only the ones empirically confirmed to have markets.
# Extend as we discover more.
SPORT_TO_SERIES = {
    "mlb": ["KXMLBGAME"],
    "nhl": ["KXNHLGAME"],
    "nba": ["KXNBAGAME"],
    "nfl": ["KXNFLGAME"],
    "soccer": ["KXMENWORLDCUP", "KXWORLDCUPGAME"],
}


def get_kalshi_markets(refresh: bool = False,
                       sports: list[str] | None = None) -> list[dict]:
    """Cached, sport-filtered Kalshi market list.

    sports: list of sport keys to fetch (e.g., ['mlb', 'nhl', 'soccer']).
    Default: all keys in SPORT_TO_SERIES.
    """
    sports = sports or list(SPORT_TO_SERIES.keys())
    series = []
    for s in sports:
        series.extend(SPORT_TO_SERIES.get(s, []))

    cache_key = ",".join(sorted(series))
    cache_file = DATA_DIR / f"kalshi_markets_{abs(hash(cache_key)) % 10**10}.json"

    if not refresh and cache_file.exists():
        with open(cache_file) as f:
            cached = json.load(f)
        age = time.time() - cached.get("ts", 0)
        if age < CACHE_TTL_SECONDS:
            logger.debug(f"using cached Kalshi markets "
                         f"({len(cached['markets'])} markets, {age:.0f}s old)")
            return cached["markets"]

    logger.info(f"fetching Kalshi markets for series: {series}")
    loop = asyncio.new_event_loop()
    try:
        markets = loop.run_until_complete(fetch_open_markets_async(series))
    finally:
        loop.close()
    logger.info(f"fetched {len(markets)} sport Kalshi markets")
    with open(cache_file, "w") as f:
        json.dump({"ts": time.time(), "markets": markets,
                   "series": series}, f)
    return markets


def match_kalshi_ticker(
    poly_title: str,
    poly_outcome: str,
    poly_slug: str | None = None,
    kalshi_markets: list[dict] | None = None,
    min_score: float = MIN_SIMILARITY,
    game_start_time: str | None = None,
) -> dict | None:
    """
    Find the Kalshi (ticker, side) that best matches a Polymarket signal.

    poly_outcome is what we want to BUY YES on (e.g., "Pittsburgh"). The Kalshi
    market is typically asymmetric (one team is named in the ticker), so:
        - if the Kalshi YES title contains the outcome word, side='yes'
        - else, side='no'

    Returns: {ticker, event_ticker, side, score, kalshi_title, yes_bid, yes_ask}
             or None if no good match.
    """
    if kalshi_markets is None:
        kalshi_markets = get_kalshi_markets()

    # 1. Try structured matchers first (precise, no false positives).
    structured = matcher_mlb.match_mlb(
        poly_title, poly_outcome, kalshi_markets,
        game_start_time=game_start_time,
    )
    if structured:
        return structured

    # 2. Fall back to fuzzy matching for non-MLB / non-structured cases.
    query = f"{poly_title} {poly_outcome}"
    query_entities = extract_entities(query)

    # Score every market
    scored = []
    for m in kalshi_markets:
        title = m.get("title", "")
        if not title:
            continue
        # Quick prefilter: require at least one entity match to avoid scoring 50K markets
        title_lower = title.lower()
        if query_entities:
            overlap = sum(1 for e in query_entities if e.lower() in title_lower)
            if overlap == 0:
                continue
        score = calculate_similarity(query, title, query_entities)
        if score < min_score:
            continue
        scored.append({
            "ticker": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": title,
            "score": score,
            "yes_bid": (m.get("yes_bid", 0) or 0) / 100.0,
            "yes_ask": (m.get("yes_ask", 0) or 0) / 100.0,
            "no_bid": (m.get("no_bid", 0) or 0) / 100.0,
            "no_ask": (m.get("no_ask", 0) or 0) / 100.0,
            "volume": m.get("volume", 0),
            "status": m.get("status", "open"),
            "close_time": m.get("close_time"),
            "yes_sub_title": m.get("yes_sub_title", ""),
            "no_sub_title": m.get("no_sub_title", ""),
        })

    if not scored:
        return None
    scored.sort(key=lambda x: (x["score"], x["volume"]), reverse=True)
    best = scored[0]

    # Determine which side (yes or no) corresponds to our outcome
    out_norm = normalize_outcome(poly_outcome)
    title_norm = normalize_outcome(best["title"])
    yes_sub_norm = normalize_outcome(best.get("yes_sub_title", ""))
    no_sub_norm = normalize_outcome(best.get("no_sub_title", ""))

    side = None
    side_reason = ""
    if out_norm in {"yes", "y", "true"}:
        side = "yes"
        side_reason = "outcome=yes"
    elif out_norm in {"no", "n", "false"}:
        side = "no"
        side_reason = "outcome=no"
    elif yes_sub_norm and out_norm in yes_sub_norm:
        side = "yes"
        side_reason = f"outcome in yes_sub({yes_sub_norm})"
    elif no_sub_norm and out_norm in no_sub_norm:
        side = "no"
        side_reason = f"outcome in no_sub({no_sub_norm})"
    elif out_norm in title_norm:
        side = "yes"
        side_reason = "outcome in title"
    else:
        # We can't confidently map → skip
        return None

    if side == "yes":
        price_to_pay = best["yes_ask"] if best["yes_ask"] > 0 else None
    else:
        price_to_pay = best["no_ask"] if best["no_ask"] > 0 else None

    best["side"] = side
    best["side_reason"] = side_reason
    best["price_to_pay"] = price_to_pay
    return best


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    refresh = "--refresh" in sys.argv
    markets = get_kalshi_markets(refresh=refresh)
    print(f"Kalshi has {len(markets)} open markets")

    # Smoke test on a few sample signals
    samples = [
        ("Pittsburgh Pirates vs. Athletics", "Pittsburgh"),
        ("Will IR Iran win on 2026-06-15?", "No"),
        ("Minnesota Twins vs. Texas Rangers", "Minnesota Twins"),
        ("Will Brazil win on 2026-06-19?", "Yes"),
        ("Hurricanes vs. Golden Knights", "Golden Knights"),
    ]
    for title, out in samples:
        m = match_kalshi_ticker(title, out, kalshi_markets=markets)
        if m:
            print(f"\n[poly] {title} → {out}")
            print(f"  [kalshi] {m['ticker']} | side={m['side']} ({m['side_reason']})")
            print(f"  title: {m['title']}")
            print(f"  score={m['score']:.3f}, ask={m['price_to_pay']}")
        else:
            print(f"\n[poly] {title} → {out}")
            print(f"  NO MATCH")
