"""
Check recent activity for candidate wallets.

For each promising wallet from the screen, hit Polymarket's activity
endpoint to find:
  - timestamp of most recent trade
  - count of trades in last 7 days
  - recent sport mix (active in current season?)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

import requests

WALLET_DIR = Path("/home/pranav/tail-analysis/data/sharp_wallets")
ACTIVITY_URL = "https://data-api.polymarket.com/activity"

# Top candidates from the screen (lowest fills + high WR)
CANDIDATES = [
    # (wallet_addr, expected_name)
    ("0xb786b8b6335e77dfad19928313e97753039cb18d", "MLB 23% sharp"),
    # ilovecircle, geniusMC etc — need to look up from the analyzer files
]


def load_all_wallet_metadata() -> list[tuple[str, str]]:
    """Get (wallet_addr, display_name) for all analyzed wallets."""
    out = []
    for p in sorted(WALLET_DIR.glob("*.json")):
        if "_clv" in p.name:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            wallet = d.get("wallet")
            name = d.get("display_name", "?")
            if wallet:
                out.append((wallet, name))
        except Exception:
            pass
    return out


def fetch_recent_activity(wallet: str, limit: int = 500) -> list[dict]:
    """Fetch the most recent N trades for a wallet."""
    try:
        r = requests.get(
            ACTIVITY_URL,
            params={"user": wallet, "limit": limit, "type": "TRADE"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  error: {e}")
        return []


def derive_sport(slug: str) -> str:
    s = (slug or "").lower()
    for tag in ["mlb", "nba", "nhl", "nfl", "wnba", "tennis", "ufc", "ncaaf", "ncaab", "soccer"]:
        if tag in s:
            return tag
    return "other"


def analyze(wallet: str, name: str) -> dict:
    trades = fetch_recent_activity(wallet, limit=500)
    if not trades:
        return {"wallet": wallet, "name": name, "active": False, "last_trade": None}

    timestamps = [t.get("timestamp", 0) for t in trades]
    now = time.time()
    last_ts = max(timestamps)
    days_since = (now - last_ts) / 86400

    # Trades in last 7 days
    seven_d_ago = now - 7 * 86400
    last_7d = [t for t in trades if t.get("timestamp", 0) >= seven_d_ago]
    last_30d = [t for t in trades if t.get("timestamp", 0) >= now - 30 * 86400]

    # Recent sport mix (last 30 days)
    sport_count: Counter = Counter()
    for t in last_30d:
        sport_count[derive_sport(t.get("eventSlug"))] += 1

    # Average usd size in recent trades
    usd_sizes = [float(t.get("usdcSize", 0) or 0) for t in last_30d]
    avg_size = sum(usd_sizes) / len(usd_sizes) if usd_sizes else 0
    max_size = max(usd_sizes) if usd_sizes else 0

    return {
        "wallet": wallet,
        "name": name,
        "active": days_since < 3,
        "last_trade": datetime.fromtimestamp(last_ts).isoformat(timespec="minutes"),
        "days_since_last": round(days_since, 2),
        "trades_7d": len(last_7d),
        "trades_30d": len(last_30d),
        "avg_usd_30d": round(avg_size, 0),
        "max_usd_30d": round(max_size, 0),
        "top_sports_30d": dict(sport_count.most_common(3)),
    }


def main() -> None:
    wallets = load_all_wallet_metadata()
    print(f"Checking activity for {len(wallets)} wallets...\n")

    # Load the screen data so we have WR / fills
    screen_data: dict[str, dict] = {}
    for w, n in wallets:
        path = WALLET_DIR / f"{w}.json"
        try:
            with open(path) as f:
                d = json.load(f)
            positions = d.get("positions", [])
            big = [p for p in positions
                   if p.get("total_stake", 0) >= 5000
                   and p.get("is_winner") is not None
                   and p.get("first_entry_price") and 0 < p.get("first_entry_price", 0) < 1]
            big_n = len(big)
            big_wr = sum(1 for p in big if p["is_winner"]) / big_n if big_n else None
            big_fills = None
            if big:
                fills = [p.get("total_fills") for p in big if p.get("total_fills")]
                if fills:
                    fills.sort()
                    big_fills = fills[len(fills)//2]
            screen_data[w] = {
                "big_n": big_n,
                "big_wr": big_wr,
                "big_fills": big_fills,
                "overall_roi": d.get("overall", {}).get("roi"),
            }
        except Exception:
            pass

    results = []
    for w, n in wallets:
        print(f"  Checking {n} ({w[:10]}...)")
        info = analyze(w, n)
        info.update(screen_data.get(w, {}))
        results.append(info)
        time.sleep(0.4)

    # Filter to active + has meaningful sample
    active = [r for r in results
              if r["active"] and (r.get("big_n") or 0) >= 30]

    # Rank by composite: WR × log(sample) / fills
    import math
    def score(r):
        wr = r.get("big_wr") or 0
        n = r.get("big_n") or 0
        fills = max(1, r.get("big_fills") or 99)
        return wr * math.log(max(n, 1) + 1) / math.sqrt(fills)

    active.sort(key=score, reverse=True)

    print("\n" + "=" * 130)
    print("ACTIVE SHARPS (traded in last 3 days) ranked by snapshot-bot suitability")
    print("score = WR * log(n) / sqrt(fills)")
    print("=" * 130)
    hdr = (f"{'name':>22} {'last_trade':>17} {'7d':>4} {'30d':>5} "
           f"{'avg_$':>7} {'max_$':>8} {'big_n':>6} {'big_WR%':>7} "
           f"{'big_fills':>9} {'ROI%':>5} {'sports_30d':>30}")
    print(hdr)
    print("-" * 140)
    for r in active:
        wr = (r.get("big_wr") or 0) * 100
        roi = (r.get("overall_roi") or 0) * 100
        sports = ", ".join(f"{s}:{n}" for s, n in (r.get("top_sports_30d") or {}).items())
        print(f"{(r['name'] or '?')[:22]:>22} {r['last_trade'][:16]:>17} "
              f"{r['trades_7d']:>4} {r['trades_30d']:>5} "
              f"${r['avg_usd_30d']:>6,.0f} ${r['max_usd_30d']:>7,.0f} "
              f"{r.get('big_n') or 0:>6} {wr:>7.1f} "
              f"{r.get('big_fills') or 0:>9} {roi:>5.1f} "
              f"{sports[:30]:>30}")

    # Also save
    out_file = Path(__file__).resolve().parent.parent / "data" / "wallet_activity_check.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {out_file}")


if __name__ == "__main__":
    main()
