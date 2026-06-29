"""
Source the top winning sharps from Polymarket leaderboards.

Returns a deduped, ranked list of wallets active in last N hours with positive
recent PnL. Output is a list of {wallet, name, week_pnl, month_pnl, last_active_ts}.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

LEADERBOARD_BASE = "https://lb-api.polymarket.com"
ACTIVITY_URL = "https://data-api.polymarket.com/activity"

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "sharp_pool.json"
CACHE_TTL_SECONDS = 3600  # refresh top sharps hourly


def fetch_leaderboard(period: str, sort: str = "profit") -> list[dict]:
    """period in {day, week, month, all}; sort in {profit, volume}"""
    try:
        r = requests.get(f"{LEADERBOARD_BASE}/{sort}",
                         params={"period": period}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[sharp_sourcer] leaderboard {period}/{sort} error: {e}", flush=True)
        return []


def fetch_last_active(wallet: str, timeout: int = 10) -> float | None:
    """Return the unix ts of the most recent trade for this wallet."""
    try:
        r = requests.get(ACTIVITY_URL,
                         params={"user": wallet, "limit": 5, "type": "TRADE"},
                         timeout=timeout)
        r.raise_for_status()
        trades = r.json()
        if trades:
            return max(t.get("timestamp", 0) for t in trades)
    except Exception:
        pass
    return None


def get_top_sharps(
    target_n: int = 100,
    min_week_pnl: float = 50_000,
    max_hours_idle: int = 72,
    refresh: bool = False,
) -> list[dict]:
    """
    Get the current top winning + active sharps.

    Strategy:
      1. Union top-200 from week and month profit leaderboards
      2. Drop any below min_week_pnl
      3. Drop any inactive > max_hours_idle
      4. Sort by max(week_pnl, month_pnl) desc and return top N

    Caches the result for CACHE_TTL_SECONDS to avoid hammering activity API.
    """
    # Try cache
    if not refresh and CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        age = time.time() - cached.get("generated_at_ts", 0)
        if age < CACHE_TTL_SECONDS:
            print(f"[sharp_sourcer] using cached pool ({len(cached['sharps'])} "
                  f"sharps, {age/60:.1f}m old)", flush=True)
            return cached["sharps"]

    print(f"[sharp_sourcer] refreshing top sharp pool (target n={target_n})...",
          flush=True)
    pool: dict[str, dict] = {}
    for period in ["week", "month"]:
        rows = fetch_leaderboard(period, "profit")
        for row in rows:
            w = row.get("proxyWallet")
            pnl = row.get("amount") or 0
            if not w or pnl < min_week_pnl:
                continue
            if w not in pool:
                pool[w] = {
                    "wallet": w,
                    "name": row.get("name") or row.get("pseudonym") or "?",
                    "week_pnl": None,
                    "month_pnl": None,
                }
            pool[w][f"{period}_pnl"] = pnl
        time.sleep(0.2)

    print(f"[sharp_sourcer]   {len(pool)} candidates after PnL filter", flush=True)

    # Activity check
    print(f"[sharp_sourcer]   checking activity (max {max_hours_idle}h idle)...",
          flush=True)
    now = time.time()
    active = []
    for w, info in pool.items():
        last = fetch_last_active(w)
        if last and (now - last) < max_hours_idle * 3600:
            info["last_active_ts"] = last
            info["hours_ago"] = (now - last) / 3600
            active.append(info)
        time.sleep(0.15)

    active.sort(
        key=lambda s: max(s.get("week_pnl") or 0, s.get("month_pnl") or 0),
        reverse=True,
    )
    active = active[:target_n]
    print(f"[sharp_sourcer]   {len(active)} active sharps after filter", flush=True)

    with open(CACHE_FILE, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "generated_at_ts": time.time(),
            "n": len(active),
            "sharps": active,
        }, f, indent=2)

    return active


if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv
    sharps = get_top_sharps(target_n=100, refresh=refresh)
    print(f"\nTop 20 of {len(sharps)} sharps:")
    print(f"{'name':>25} {'wallet':>12} {'week_PnL':>12} {'month_PnL':>12} {'hrs_ago':>8}")
    for s in sharps[:20]:
        wk = s.get("week_pnl") or 0
        mo = s.get("month_pnl") or 0
        print(f"{s['name'][:25]:>25} {s['wallet'][:10]+'..':>12} "
              f"${wk:>+11,.0f} ${mo:>+11,.0f} {s.get('hours_ago', 0):>8.1f}")
