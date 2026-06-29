"""
Fully enrich HomeRunHazard's position history with first_trade_ts and
first_entry_price for ALL closed positions (not just the recent 891).

Strategy: paginate the Polymarket /activity endpoint once for the full
wallet history, then group trades by (conditionId, outcome) to derive
the first-trade timestamp and entry price per position. Much faster
than per-position lookups.

Output: data/hrh_enriched_positions.json
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests

import sys

# Defaults can be overridden via CLI args:
#   python enrich_history.py <wallet_addr> <out_prefix>
DEFAULT_WALLET = "0x5268527977f700f9bf9b6d5cd843859e4e70135d"
DEFAULT_PREFIX = "hrh"

WALLET = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WALLET
OUT_PREFIX = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PREFIX

ACTIVITY_URL = "https://data-api.polymarket.com/activity"
ANALYZER_FILE = Path(
    f"/home/pranav/tail-analysis/data/sharp_wallets/{WALLET}.json"
)
OUT_FILE = Path(__file__).resolve().parent.parent / "data" / f"{OUT_PREFIX}_enriched_positions.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = OUT_FILE.parent / f"{OUT_PREFIX}_enrich_progress.txt"
RAW_DUMP_FILE = OUT_FILE.parent / f"{OUT_PREFIX}_all_trades.json"

PAGE_SIZE = 500
MAX_PAGES = 2000  # safety cap
SLEEP_BETWEEN_PAGES = 0.25
HISTORY_FLOOR_TS = 1609459200  # 2021-01-01 (safety floor for the backwards walk)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def fetch_activity_page(end_ts: int | None = None) -> list[dict]:
    """Fetch a page of trades with timestamp <= end_ts (or latest if None)."""
    params = {
        "user": WALLET,
        "limit": PAGE_SIZE,
        "type": "TRADE",
    }
    if end_ts is not None:
        params["end"] = end_ts

    for attempt in range(5):
        try:
            r = requests.get(ACTIVITY_URL, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            wait = 2 ** attempt
            log(f"  fetch error end={end_ts} (attempt {attempt+1}): {e} -- sleep {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch end={end_ts} after retries")


def fetch_all_activity() -> list[dict]:
    """Walk backwards through trade history using timestamp pagination."""
    from datetime import datetime
    all_trades: list[dict] = []
    seen_tx: set[str] = set()
    end_ts: int | None = None

    for page in range(MAX_PAGES):
        trades = fetch_activity_page(end_ts)
        if not trades:
            log(f"Empty page at end={end_ts}, stopping")
            break

        new_in_page = 0
        oldest = float("inf")
        for t in trades:
            tx = t.get("transactionHash") or f"{t.get('timestamp')}-{t.get('conditionId')}"
            if tx in seen_tx:
                continue
            seen_tx.add(tx)
            all_trades.append(t)
            new_in_page += 1
            ts = t.get("timestamp", 0)
            if ts and ts < oldest:
                oldest = ts

        if new_in_page == 0:
            log(f"All {len(trades)} trades in page were duplicates, stopping")
            break

        oldest_dt = datetime.fromtimestamp(int(oldest)).strftime("%Y-%m-%d %H:%M") if oldest != float("inf") else "?"
        if page % 5 == 0:
            log(f"Page {page}: cumulative={len(all_trades):,} (+{new_in_page} new), oldest={oldest_dt}")

        # Only stop on genuinely-thin pages (< 5 new trades = mostly dedups at boundary)
        # and only after we've fetched at least one full page worth of progress
        if new_in_page < 5 and page > 0:
            log(f"Page returned only {new_in_page} new trades, history exhausted")
            break

        if oldest <= HISTORY_FLOOR_TS:
            log(f"Hit history floor at {oldest_dt}, stopping")
            break

        end_ts = int(oldest) - 1
        time.sleep(SLEEP_BETWEEN_PAGES)

    return all_trades


def derive_first_trade(trades: list[dict]) -> dict[tuple, dict]:
    """
    Group trades by (conditionId, outcome). For each group, find the
    earliest BUY trade — that gives us first_trade_ts and first_entry_price.
    """
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        side = (t.get("side") or "").upper()
        if side != "BUY":
            continue
        cid = t.get("conditionId")
        outcome = t.get("outcome")
        if not cid or not outcome:
            continue
        by_key[(cid, outcome)].append(t)

    first_trade_map: dict[tuple, dict] = {}
    for key, group in by_key.items():
        group.sort(key=lambda t: t.get("timestamp", 0))
        first = group[0]
        first_trade_map[key] = {
            "first_trade_ts": first.get("timestamp"),
            "first_entry_price": float(first.get("price", 0)) if first.get("price") else None,
            "trade_count": len(group),
            "total_usd": sum(float(t.get("usdcSize", 0) or 0) for t in group),
        }
    return first_trade_map


def main() -> None:
    PROGRESS_FILE.write_text("")
    log(f"Starting enrichment for {WALLET}")

    if RAW_DUMP_FILE.exists():
        log(f"Step 1: raw trades already exist at {RAW_DUMP_FILE}, loading")
        with open(RAW_DUMP_FILE) as f:
            trades = json.load(f)
        log(f"Loaded {len(trades):,} cached trades")
    else:
        log("Step 1: pulling full activity history (paginated)")
        trades = fetch_all_activity()
        log(f"Fetched {len(trades):,} total trades")
        with open(RAW_DUMP_FILE, "w") as f:
            json.dump(trades, f)
        log(f"Wrote raw trades to {RAW_DUMP_FILE}")

    log("Step 2: grouping by (conditionId, outcome) and deriving first trade")
    first_map = derive_first_trade(trades)
    log(f"Unique (conditionId, outcome) keys: {len(first_map):,}")

    log("Step 3: merging with analyzer's positions list")
    if ANALYZER_FILE.exists():
        with open(ANALYZER_FILE) as f:
            analyzer_data = json.load(f)
        positions = analyzer_data["positions"]
        log(f"Analyzer has {len(positions):,} closed positions")

        enriched = []
        matched = 0
        for p in positions:
            key = (p.get("condition_id"), p.get("outcome"))
            new_p = dict(p)
            if key in first_map:
                fm = first_map[key]
                if new_p.get("first_trade_ts") is None:
                    new_p["first_trade_ts"] = fm["first_trade_ts"]
                if new_p.get("first_entry_price") is None and fm.get("first_entry_price") is not None:
                    new_p["first_entry_price"] = fm["first_entry_price"]
                new_p["_enriched_trade_count"] = fm["trade_count"]
                new_p["_enriched_total_usd"] = fm["total_usd"]
                matched += 1
            enriched.append(new_p)
        log(f"Matched {matched:,} of {len(positions):,} positions")
    else:
        log(f"No analyzer file at {ANALYZER_FILE} — synthesizing positions from trades")
        # Build synthetic positions from raw trade history. For each
        # (conditionId, outcome) compute: stake, avg price, is_winner from final
        # price (curPrice), realized PnL. This matches the analyzer's schema
        # closely enough for downstream backtests.
        # Group SELLs and BUYs separately to get stake / exit
        by_key_all: dict[tuple, list[dict]] = defaultdict(list)
        for t in trades:
            cid = t.get("conditionId")
            outcome = t.get("outcome")
            if not cid or not outcome:
                continue
            by_key_all[(cid, outcome)].append(t)

        # Fetch position data for resolution status (curPrice / realizedPnl)
        log("  Fetching current position data for resolution status...")
        cur_positions = {}
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": WALLET, "sizeThreshold": 0, "limit": 500},
                timeout=30,
            )
            r.raise_for_status()
            for pos in r.json():
                cid = pos.get("conditionId")
                outcome = pos.get("outcome")
                if cid and outcome:
                    cur_positions[(cid, outcome)] = pos
            log(f"  Loaded {len(cur_positions)} current positions")
        except Exception as e:
            log(f"  Position fetch failed: {e}")

        enriched = []
        for key, tlist in by_key_all.items():
            cid, outcome = key
            buys = [t for t in tlist if (t.get("side") or "").upper() == "BUY"]
            sells = [t for t in tlist if (t.get("side") or "").upper() == "SELL"]
            if not buys:
                continue
            buys.sort(key=lambda t: t.get("timestamp", 0))
            first_b = buys[0]

            total_buy_usd = sum(float(b.get("usdcSize", 0) or 0) for b in buys)
            total_buy_shares = sum(float(b.get("size", 0) or 0) for b in buys)
            avg_price = total_buy_usd / total_buy_shares if total_buy_shares else 0

            total_sell_usd = sum(float(s.get("usdcSize", 0) or 0) for s in sells)
            total_sell_shares = sum(float(s.get("size", 0) or 0) for s in sells)
            net_shares = total_buy_shares - total_sell_shares

            cur_pos = cur_positions.get(key, {})
            cur_price = cur_pos.get("curPrice")
            realized_pnl = float(cur_pos.get("realizedPnl", 0) or 0)
            is_winner = None
            if cur_price == 1:
                is_winner = True
            elif cur_price == 0:
                is_winner = False
            elif realized_pnl != 0 and abs(net_shares) < 0.01:
                # Fully closed manually
                is_winner = realized_pnl > 0

            enriched.append({
                "condition_id": cid,
                "outcome": outcome,
                "event_slug": first_b.get("eventSlug"),
                "market_slug": first_b.get("slug"),
                "title": first_b.get("title"),
                "total_stake": total_buy_usd,
                "avg_buy_price": avg_price,
                "realized_pnl": realized_pnl,
                "is_winner": is_winner,
                "first_trade_ts": first_b.get("timestamp"),
                "first_entry_price": float(first_b.get("price", 0)) if first_b.get("price") else None,
                "n_buys": len(buys),
                "n_sells": len(sells),
                "_enriched_trade_count": len(buys),
                "_enriched_total_usd": total_buy_usd,
            })
        log(f"Synthesized {len(enriched):,} positions from trades")
        matched = len(enriched)
        positions = enriched

    out = {
        "wallet": WALLET,
        "positions": enriched,
        "trade_count_total": len(trades),
        "unique_market_outcome_keys": len(first_map),
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f)
    log(f"Wrote enriched positions to {OUT_FILE}")
    log("DONE")


if __name__ == "__main__":
    main()
