"""
Screen all analyzed wallets for snapshot-bot copyability.

For each wallet, compute the metrics that matter for our copy strategy:
  - median fills per position (low = single big bets, ideal for our bot)
  - WR & ROI overall + on big positions
  - sport mix
  - recent activity

The wallet we want is: SHARP, SINGLE-SHOT (low fills), ACTIVE, IN-SEASON SPORT.
"""
from __future__ import annotations

import json
import glob
import statistics
from collections import Counter
from pathlib import Path

WALLET_DIR = Path("/home/pranav/tail-analysis/data/sharp_wallets")


def screen_wallet(path: Path) -> dict | None:
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return None

    positions = d.get("positions", [])
    if not positions:
        return None

    overall = d.get("overall", {})
    fills = d.get("fills", {})
    direct = d.get("directionality", {})
    pregame = d.get("pregame_live", {})
    verdict = d.get("verdict", {})

    # Sport mix
    sport_count: Counter = Counter()
    for p in positions:
        sport_count[(p.get("sport") or "unknown").lower()] += 1
    top_sport = sport_count.most_common(1)[0] if sport_count else ("none", 0)
    top_sport_pct = top_sport[1] / len(positions)

    # Median fills (KEY METRIC for snapshot bot copyability)
    median_fills = fills.get("median")
    mean_fills = fills.get("mean")

    # WR & ROI on big positions (≥$5K stake) — only count if we have data
    big_pos = [p for p in positions
               if p.get("total_stake", 0) >= 5000
               and p.get("is_winner") is not None
               and p.get("first_entry_price") and 0 < p.get("first_entry_price", 0) < 1]
    big_n = len(big_pos)
    big_wr = sum(1 for p in big_pos if p["is_winner"]) / big_n if big_n else None

    # Fills only on big positions
    big_fills_median = None
    if big_pos:
        bfs = [p.get("total_fills") for p in big_pos if p.get("total_fills")]
        if bfs:
            big_fills_median = statistics.median(bfs)

    return {
        "wallet": d.get("wallet"),
        "name": d.get("display_name"),
        "n_positions": len(positions),
        "overall_wr": overall.get("win_rate"),
        "overall_roi": overall.get("roi"),
        "total_stake": overall.get("total_stake"),
        "realized_pnl": overall.get("realized_pnl"),
        "median_fills_all": median_fills,
        "mean_fills_all": mean_fills,
        "directional_rate": direct.get("directional_rate"),
        "pregame_rate": pregame.get("pregame_rate"),
        "top_sport": top_sport[0],
        "top_sport_pct": top_sport_pct,
        "n_big_5k": big_n,
        "big_5k_wr": big_wr,
        "big_5k_fills_median": big_fills_median,
        "verdict_score": verdict.get("score"),
    }


def main() -> None:
    paths = sorted(p for p in WALLET_DIR.glob("*.json")
                   if "_clv" not in p.name and "_progress" not in p.name)
    print(f"Screening {len(paths)} wallets...\n")

    rows = []
    for p in paths:
        r = screen_wallet(p)
        if r is not None:
            rows.append(r)

    # Sort by big-5k WR descending (only consider wallets with meaningful sample)
    qualified = [r for r in rows if r["n_big_5k"] and r["n_big_5k"] >= 20 and r["big_5k_wr"]]
    qualified.sort(key=lambda r: r["big_5k_wr"], reverse=True)

    print("=" * 130)
    print(f"WALLETS RANKED BY BIG-POSITION WIN RATE (n≥20 big positions)")
    print("=" * 130)
    hdr = f"{'name':>22} {'sport':>8} {'spct%':>6} {'big_n':>6} {'big_WR%':>7} {'big_fills':>9} {'all_fills':>9} {'dir%':>5} {'preg%':>6} {'ROI%':>5} {'PnL$K':>7}"
    print(hdr)
    print("-" * 130)
    for r in qualified:
        sport_pct = (r["top_sport_pct"] or 0) * 100
        wr = (r["big_5k_wr"] or 0) * 100
        dir_pct = (r["directional_rate"] or 0) * 100
        preg = (r["pregame_rate"] or 0) * 100
        roi = (r["overall_roi"] or 0) * 100
        pnl_k = (r["realized_pnl"] or 0) / 1000
        print(f"{(r['name'] or '?')[:22]:>22} {(r['top_sport'] or '?')[:8]:>8} "
              f"{sport_pct:>6.1f} {r['n_big_5k']:>6} {wr:>7.1f} "
              f"{(r['big_5k_fills_median'] or 0):>9.0f} "
              f"{(r['median_fills_all'] or 0):>9.0f} "
              f"{dir_pct:>5.0f} {preg:>6.0f} {roi:>5.1f} {pnl_k:>+7,.0f}")

    # Also: callout wallets with LOW fill counts (best for snapshot bot)
    print("\n" + "=" * 130)
    print("WALLETS RANKED BY LOW MEDIAN FILLS (single-shot bettors — IDEAL for snapshot bot)")
    print("Lower fills = he places ONE big bet per market = easy to detect + no price drift")
    print("=" * 130)
    single_shot = [r for r in rows if r["median_fills_all"] is not None]
    single_shot.sort(key=lambda r: r["median_fills_all"])
    print(hdr)
    print("-" * 130)
    for r in single_shot[:15]:
        sport_pct = (r["top_sport_pct"] or 0) * 100
        wr = (r["big_5k_wr"] or 0) * 100 if r["big_5k_wr"] else 0
        big_n = r["n_big_5k"] or 0
        dir_pct = (r["directional_rate"] or 0) * 100
        preg = (r["pregame_rate"] or 0) * 100
        roi = (r["overall_roi"] or 0) * 100
        pnl_k = (r["realized_pnl"] or 0) / 1000
        print(f"{(r['name'] or '?')[:22]:>22} {(r['top_sport'] or '?')[:8]:>8} "
              f"{sport_pct:>6.1f} {big_n:>6} {wr:>7.1f} "
              f"{(r['big_5k_fills_median'] or 0):>9.0f} "
              f"{(r['median_fills_all'] or 0):>9.0f} "
              f"{dir_pct:>5.0f} {preg:>6.0f} {roi:>5.1f} {pnl_k:>+7,.0f}")


if __name__ == "__main__":
    main()
