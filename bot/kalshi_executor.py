"""
Kalshi order executor — paper mode and live mode.

In paper mode: log what we WOULD place; mark in state.
In live mode: actually call KalshiBot's kalshi_client_rsa.place_order().

We always go through a single execute() entrypoint with a mode flag, so the
bot loop is unaware of paper-vs-live.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/pranav/KalshiBot")

logger = logging.getLogger("kalshi_executor")


def _make_bet_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def execute_bet(
    *,
    signal: dict,
    kalshi_match: dict,
    stake_dollars: float,
    mode: str = "paper",
    kalshi_client=None,
) -> dict | None:
    """
    Place (or simulate) a Kalshi bet.

    signal: a dict with consensus_avg_entry, title, sport, outcome
    kalshi_match: dict with ticker, side, price_to_pay (in dollars), event_ticker
    stake_dollars: how much $ to bet (we convert to integer contract count)
    mode: 'paper' = log only; 'live' = actually place order

    Returns a bet dict (or None on failure) with all info needed to settle later.
    """
    ticker = kalshi_match["ticker"]
    side = kalshi_match["side"]
    price = kalshi_match.get("price_to_pay")

    if not price or price <= 0 or price >= 1:
        logger.warning(f"bad price for {ticker}: {price}")
        return None

    # Kalshi prices are integer cents 1..99
    price_cents = max(1, min(99, int(round(price * 100))))

    # Contract count: each contract costs price_cents/100 dollars.
    # Stake $10 at 0.50 -> 20 contracts. Round DOWN.
    count = max(1, int(stake_dollars * 100 / price_cents))
    actual_stake = count * price_cents / 100

    bet_id = _make_bet_id()
    bet = {
        "bet_id": bet_id,
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "ticker": ticker,
        "event_ticker": kalshi_match.get("event_ticker"),
        "side": side,
        "price_cents": price_cents,
        "price_dollars": price_cents / 100,
        "count": count,
        "stake_actual": actual_stake,
        "stake_target": stake_dollars,
        "polymarket_cid": signal["cid"],
        "polymarket_outcome": signal["outcome"],
        "title_poly": signal["title"],
        "title_kalshi": kalshi_match.get("title"),
        "sport": signal["sport"],
        "consensus_n": signal["consensus_n"],
        "stake_conviction": signal["stake_conviction"],
        "consensus_avg_entry": signal["consensus_avg_entry"],
        "match_score": kalshi_match.get("score"),
    }

    if mode == "paper":
        bet["status"] = "paper"
        logger.info(f"[PAPER] would buy {count} {ticker}/{side} @ {price_cents}c "
                    f"= ${actual_stake:.2f} | {signal['title'][:50]}")
        return bet

    # LIVE mode
    if kalshi_client is None:
        logger.error("live mode but no kalshi_client provided")
        return None

    try:
        if side == "yes":
            response = kalshi_client.place_order(
                ticker=ticker, side="yes", action="buy",
                count=count, yes_price=price_cents,
            )
        else:
            response = kalshi_client.place_order(
                ticker=ticker, side="no", action="buy",
                count=count, no_price=price_cents,
            )
        bet["status"] = "live"
        bet["kalshi_response"] = response
        order_id = response.get("order", {}).get("order_id")
        bet["kalshi_order_id"] = order_id
        logger.info(f"[LIVE] placed order {order_id}: {count} {ticker}/{side} "
                    f"@ {price_cents}c = ${actual_stake:.2f}")
        return bet
    except Exception as e:
        logger.exception(f"order placement failed for {ticker}: {e}")
        bet["status"] = "failed"
        bet["error"] = str(e)
        return bet
