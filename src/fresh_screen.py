"""
Fresh wallet screen — find NEW directional sharps from Polymarket leaderboards.

Pulls multiple leaderboards (weekly/monthly/all-time PnL + volume), dedupes
to a candidate pool of ~100 wallets, then fast-screens each one based on:

  - Active (traded in last 3 days)
  - Directional (≤30% of big positions hedged with opposite outcome)
  - Big-bet WR (≥55% on positions ≥$2K)
  - In-season sport (MLB / tennis / soccer / NHL playoffs)
  - Positive ROI on directional positions

Output: data/fresh_screen.json with ranked candidates.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = DATA_DIR / "fresh_screen.json"
PROGRESS_FILE = DATA_DIR / "fresh_screen_progress.txt"

LEADERBOARD_BASE = "https://lb-api.polymarket.com"
POSITIONS_URL = "https://data-api.polymarket.com/positions"
ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def fetch_leaderboard(period: str, sort: str) -> list[dict]:
    """period: day/week/month/all, sort: profit/volume"""
    url = f"{LEADERBOARD_BASE}/{sort}"
    try:
        r = requests.get(url, params={"period": period}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"leaderboard {period}/{sort} error: {e}")
        return []


def fetch_positions(wallet: str) -> list[dict]:
    """All positions (no size threshold) — gives us closed + open."""
    try:
        r = requests.get(
            POSITIONS_URL,
            params={"user": wallet, "sizeThreshold": 0, "limit": 500},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  positions error for {wallet[:10]}: {e}")
        return []


def fetch_recent_activity(wallet: str) -> list[dict]:
    """Last 100 trades."""
    try:
        r = requests.get(
            ACTIVITY_URL,
            params={"user": wallet, "limit": 100, "type": "TRADE"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  activity error for {wallet[:10]}: {e}")
        return []


def derive_sport(slug: str) -> str:
    s = (slug or "").lower()
    for tag in ["mlb", "nba", "nhl", "wnba", "tennis", "ufc", "ncaaf", "ncaab", "soccer", "nfl"]:
        if tag in s:
            return tag
    return "other"


IN_SEASON_2026_06 = {"mlb", "tennis", "soccer", "nhl", "wnba"}


def screen_wallet(wallet: str, name: str) -> dict | None:
    """Fast screen: returns metrics dict or None if disqualified."""

    # Quick activity check
    trades = fetch_recent_activity(wallet)
    if not trades:
        return None
    last_ts = max(t.get("timestamp", 0) for t in trades)
    days_since = (time.time() - last_ts) / 86400
    if days_since > 3:
        return {"wallet": wallet, "name": name, "active": False,
                "days_since_last": round(days_since, 1)}

    # Recent sport mix
    sport_count: Counter = Counter()
    for t in trades:
        sport_count[derive_sport(t.get("eventSlug"))] += 1

    in_season_pct = sum(n for s, n in sport_count.items() if s in IN_SEASON_2026_06) / len(trades)

    # Positions for directional + WR analysis
    positions = fetch_positions(wallet)
    if not positions:
        return None

    by_cid = defaultdict(list)
    for p in positions:
        cid = p.get("conditionId")
        if not cid:
            continue
        by_cid[cid].append(p)

    # Directional analysis: % of markets where only one outcome is held
    directional_markets = sum(1 for plist in by_cid.values() if len(plist) == 1)
    total_markets = len(by_cid)
    directional_rate = directional_markets / total_markets if total_markets else 0

    # Big-bet WR (≥$2K stake)
    big_positions = []
    for plist in by_cid.values():
        if len(plist) == 1:  # only count directional
            p = plist[0]
            try:
                stake = float(p.get("totalBought", 0) or 0) * float(p.get("avgPrice", 0) or 0)
            except (TypeError, ValueError):
                stake = 0
            if stake >= 2000:
                # Determine outcome
                cur = p.get("curPrice")
                is_winner = None
                if cur is not None:
                    if cur == 1:
                        is_winner = True
                    elif cur == 0:
                        is_winner = False
                if is_winner is None:
                    realized = float(p.get("realizedPnl", 0) or 0)
                    if realized != 0:
                        is_winner = realized > 0
                big_positions.append({
                    "stake": stake,
                    "is_winner": is_winner,
                    "realized_pnl": float(p.get("realizedPnl", 0) or 0),
                    "title": p.get("title"),
                })

    resolved_big = [p for p in big_positions if p["is_winner"] is not None]
    big_wr = (sum(1 for p in resolved_big if p["is_winner"]) / len(resolved_big)
              if resolved_big else 0)
    big_pnl = sum(p["realized_pnl"] for p in big_positions)
    big_stake = sum(p["stake"] for p in big_positions)
    big_roi = big_pnl / big_stake if big_stake > 0 else 0

    return {
        "wallet": wallet,
        "name": name,
        "active": True,
        "days_since_last": round(days_since, 2),
        "last_trade": datetime.fromtimestamp(last_ts).isoformat(timespec="minutes"),
        "trades_100": len(trades),
        "sport_mix_100": dict(sport_count.most_common(5)),
        "in_season_pct": round(in_season_pct * 100, 1),
        "positions_total": len(positions),
        "unique_markets": total_markets,
        "directional_rate": round(directional_rate * 100, 1),
        "big_n": len(big_positions),
        "big_resolved_n": len(resolved_big),
        "big_wr": round(big_wr * 100, 1),
        "big_roi": round(big_roi * 100, 2),
        "big_pnl": round(big_pnl, 0),
        "big_stake": round(big_stake, 0),
    }


def score(r: dict) -> float:
    """Composite score: WR × ROI × log(sample) × directional × in_season."""
    import math
    if not r.get("active") or r.get("big_resolved_n", 0) < 5:
        return -1e9
    wr = (r.get("big_wr") or 0) / 100
    roi = max(0, (r.get("big_roi") or 0) / 100)
    n = r.get("big_resolved_n") or 1
    direct = (r.get("directional_rate") or 0) / 100
    season = (r.get("in_season_pct") or 0) / 100
    return wr * roi * math.log(n + 1) * direct * (0.5 + 0.5 * season)


def main() -> None:
    PROGRESS_FILE.write_text("")
    log("Starting fresh wallet screen")

    log("\nStep 1: Sourcing candidates from leaderboards...")
    candidates: dict[str, str] = {}
    for period, sort in [
        ("day", "profit"),
        ("week", "profit"),
        ("month", "profit"),
        ("week", "volume"),
        ("month", "volume"),
        ("all", "profit"),
    ]:
        rows = fetch_leaderboard(period, sort)
        new_count = 0
        for row in rows:
            wallet = row.get("proxyWallet") or row.get("wallet")
            name = row.get("name") or row.get("pseudonym") or "?"
            if wallet and wallet not in candidates:
                candidates[wallet] = name
                new_count += 1
        log(f"  {period}/{sort}: pulled {len(rows)}, new candidates +{new_count}")
        time.sleep(0.3)

    # Also include previously-known good wallets we want to re-evaluate
    log(f"\nTotal unique candidates after dedup: {len(candidates)}")

    log("\nStep 2: Screening each candidate (this takes ~30s per wallet)...")
    results = []
    for i, (wallet, name) in enumerate(candidates.items(), 1):
        log(f"  [{i}/{len(candidates)}] {name[:25]:>25} ({wallet[:10]}...)")
        r = screen_wallet(wallet, name)
        if r is not None:
            results.append(r)
        time.sleep(0.3)
        # Save incrementally
        if i % 10 == 0:
            with open(OUT_FILE, "w") as f:
                json.dump({"results": results}, f, indent=2)

    log(f"\nScreened {len(results)} candidates")

    # Rank
    qualified = [r for r in results
                 if r.get("active") and r.get("big_resolved_n", 0) >= 5]
    qualified.sort(key=score, reverse=True)

    log(f"\nQualified (active + n≥5 big resolved): {len(qualified)}")
    log("\n" + "=" * 140)
    log("TOP 25 FRESH DIRECTIONAL SHARPS")
    log("=" * 140)

    hdr = (f"{'name':>22} {'last':>12} {'dir%':>5} {'big_n':>6} {'big_WR%':>7} "
           f"{'big_ROI%':>8} {'big_PnL':>10} {'big_stake':>11} {'in_season%':>10} "
           f"{'sport_mix':>30}")
    log(hdr)
    log("-" * 140)
    for r in qualified[:25]:
        sm = ",".join(f"{s}:{n}" for s, n in (r.get("sport_mix_100") or {}).items())
        log(f"{(r['name'] or '?')[:22]:>22} {r['last_trade'][5:16]:>12} "
            f"{r['directional_rate']:>5.0f} {r['big_n']:>6} {r['big_wr']:>7.1f} "
            f"{r['big_roi']:>+8.2f} {r['big_pnl']:>+10,.0f} {r['big_stake']:>11,.0f} "
            f"{r['in_season_pct']:>10.1f} {sm[:30]:>30}")

    with open(OUT_FILE, "w") as f:
        json.dump({"results": results, "qualified_ranked": qualified[:50]}, f, indent=2)
    log(f"\nWrote {OUT_FILE}")
    log("DONE")


if __name__ == "__main__":
    main()
