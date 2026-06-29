"""
Consensus-tail strategy.

Idea: aggregate the open positions of the top N winning sharps. Score each
(market, outcome) by:
  - number of sharps holding it
  - sum of their stake-weighted conviction
  - quality of those sharps (recent PnL)

Output: ranked list of "consensus picks" — markets where multiple winning
sharps agree on the same outcome. These are higher-conviction, lower-variance
signals than any single trader's bet.

Why this works:
  - One sharp could be lucky on a single market
  - 5+ sharps converging on same outcome = market mispricing
  - Even if some sharps are degen, the consensus filters out noise

The trade-off vs single-wallet tailing:
  - Slower (we wait for convergence)
  - But each signal is much higher confidence
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = DATA_DIR / "consensus_picks.json"
PROGRESS_FILE = DATA_DIR / "consensus_progress.txt"

LEADERBOARD_BASE = "https://lb-api.polymarket.com"
POSITIONS_URL = "https://data-api.polymarket.com/positions"
ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def get_top_sharps(min_pnl: float = 100_000) -> list[dict]:
    """Pull top winners from week leaderboard."""
    sharps = {}
    for period in ["week", "month"]:
        try:
            r = requests.get(f"{LEADERBOARD_BASE}/profit",
                             params={"period": period}, timeout=15)
            for row in r.json():
                w = row.get("proxyWallet")
                pnl = row.get("amount", 0)
                if not w or pnl < min_pnl:
                    continue
                if w not in sharps:
                    sharps[w] = {"name": row.get("name") or "?",
                                 "week_pnl": None, "month_pnl": None}
                sharps[w][f"{period}_pnl"] = pnl
        except Exception as e:
            log(f"  leaderboard {period} error: {e}")
        time.sleep(0.3)
    return [{"wallet": w, **info} for w, info in sharps.items()]


def fetch_positions(wallet: str, min_size: float = 1000) -> list[dict]:
    try:
        r = requests.get(POSITIONS_URL,
                         params={"user": wallet, "sizeThreshold": min_size,
                                 "limit": 200},
                         timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def fetch_last_active(wallet: str) -> float | None:
    try:
        r = requests.get(ACTIVITY_URL,
                         params={"user": wallet, "limit": 5, "type": "TRADE"},
                         timeout=10)
        trades = r.json()
        if trades:
            return max(t.get("timestamp", 0) for t in trades)
    except Exception:
        return None
    return None


def derive_sport(slug: str) -> str:
    s = (slug or "").lower()
    if s.startswith("fifwc") or "world-cup" in s:
        return "wc2026"
    for tag in ["mlb", "nba", "nhl", "wnba", "wimbledon", "french-open",
                "tennis", "ufc", "nfl", "epl"]:
        if tag in s:
            return tag
    return "other"


def main() -> None:
    PROGRESS_FILE.write_text("")
    log("=== Consensus-tail strategy ===")
    log("Step 1: get top winning sharps (week or month >= $100K)")

    sharps = get_top_sharps(min_pnl=100_000)
    log(f"  Found {len(sharps)} winning sharps")

    # Filter to recently active (last 48 hours)
    log("Step 2: filter to active in last 48h")
    now = time.time()
    active_sharps = []
    for s in sharps:
        last = fetch_last_active(s["wallet"])
        if last and (now - last) < 48 * 3600:
            s["last_trade_ts"] = last
            s["hours_ago"] = (now - last) / 3600
            active_sharps.append(s)
        time.sleep(0.15)
    log(f"  {len(active_sharps)} are active in last 48h")

    # Sort by best of week/month PnL
    active_sharps.sort(key=lambda s: max(s.get("week_pnl") or 0, s.get("month_pnl") or 0),
                       reverse=True)
    active_sharps = active_sharps[:50]  # cap at top 50
    log(f"  Using top {len(active_sharps)} sharps")

    log("Step 3: fetch open positions per sharp")
    sharp_positions = {}
    for s in active_sharps:
        positions = fetch_positions(s["wallet"], min_size=1000)
        sharp_positions[s["wallet"]] = positions
        time.sleep(0.15)
    total_pos = sum(len(p) for p in sharp_positions.values())
    log(f"  Pulled {total_pos:,} total positions")

    log("Step 4: aggregate by (market, outcome) → consensus picks")
    consensus = defaultdict(lambda: {
        "n_sharps": 0, "sharps": [], "total_stake": 0,
        "avg_avg_price": 0, "min_avg": 1.0, "max_avg": 0.0,
        "title": None, "outcome": None, "sport": None, "slug": None,
    })

    for s in active_sharps:
        for p in sharp_positions.get(s["wallet"], []):
            cid = p.get("conditionId")
            outcome = p.get("outcome")
            if not cid or not outcome:
                continue
            try:
                stake = float(p.get("totalBought", 0)) * float(p.get("avgPrice", 0))
            except (TypeError, ValueError):
                continue
            if stake < 1000:
                continue
            key = (cid, outcome)
            c = consensus[key]
            c["n_sharps"] += 1
            c["sharps"].append({
                "name": s["name"], "wallet": s["wallet"],
                "stake": stake, "avg_price": float(p.get("avgPrice", 0)),
                "cur_price": float(p.get("curPrice", 0)),
                "cash_pnl": float(p.get("cashPnl", 0)),
                "week_pnl": s.get("week_pnl"),
            })
            c["total_stake"] += stake
            c["min_avg"] = min(c["min_avg"], float(p.get("avgPrice", 0)))
            c["max_avg"] = max(c["max_avg"], float(p.get("avgPrice", 0)))
            c["title"] = p.get("title") or c["title"]
            c["outcome"] = outcome
            slug = p.get("eventSlug") or ""
            c["sport"] = derive_sport(slug)
            c["slug"] = slug

    # Compute avg of avg prices
    for c in consensus.values():
        if c["n_sharps"]:
            c["avg_avg_price"] = sum(s["avg_price"] for s in c["sharps"]) / c["n_sharps"]

    # Ranking
    picks = sorted(consensus.items(),
                   key=lambda kv: (kv[1]["n_sharps"], kv[1]["total_stake"]),
                   reverse=True)

    log(f"  {len(picks)} unique (market, outcome) keys")
    log(f"  Picks with >=2 sharps: {sum(1 for _, c in picks if c['n_sharps'] >= 2)}")
    log(f"  Picks with >=3 sharps: {sum(1 for _, c in picks if c['n_sharps'] >= 3)}")
    log(f"  Picks with >=5 sharps: {sum(1 for _, c in picks if c['n_sharps'] >= 5)}")

    log("\n" + "=" * 130)
    log("TOP CONSENSUS PICKS (>=2 sharps, sorted by # of sharps then stake)")
    log("=" * 130)
    log(f"{'sport':>8} {'n':>3} {'$total':>10} {'avg_$':>8} {'cur_$':>6} "
        f"{'unr_PnL':>10} {'outcome':>10}  {'title':>60}")
    log('-' * 130)
    for (cid, out), c in picks[:30]:
        if c["n_sharps"] < 2:
            break
        unr_pnl = sum(s["cash_pnl"] for s in c["sharps"])
        # Approximate cur price as avg cur across sharps
        cur_avg = (sum(s["cur_price"] for s in c["sharps"]) / c["n_sharps"]
                   if c["n_sharps"] else 0)
        log(f"{c['sport']:>8} {c['n_sharps']:>3} ${c['total_stake']:>9,.0f} "
            f"{c['avg_avg_price']:>8.3f} {cur_avg:>6.3f} "
            f"${unr_pnl:>+9,.0f} {(c['outcome'] or '?')[:10]:>10}  "
            f"{(c['title'] or '?')[:60]:>60}")

    # Save
    out = []
    for (cid, out_name), c in picks[:100]:
        out.append({
            "condition_id": cid, "outcome": out_name,
            "title": c["title"], "sport": c["sport"], "slug": c["slug"],
            "n_sharps": c["n_sharps"],
            "total_stake": c["total_stake"],
            "avg_entry_price": c["avg_avg_price"],
            "min_entry_price": c["min_avg"], "max_entry_price": c["max_avg"],
            "sharps": c["sharps"],
        })
    with open(OUT_FILE, "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(),
                   "n_sharps_total": len(active_sharps),
                   "picks": out}, f, indent=2)
    log(f"\nWrote {OUT_FILE}")


if __name__ == "__main__":
    main()
