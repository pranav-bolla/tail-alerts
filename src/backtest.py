"""
Backtest the HomeRunHazard tail strategy against his closed-position history.

Strategy under test:
  For each historical position where:
    - sport == 'mlb'
    - is_pregame == True
    - total_stake >= TRIGGER_USD
  Simulate the bot entering at (first_entry_price + slippage) with BOT_STAKE,
  and compute realized PnL based on is_winner.

The position-level approximation is accurate because the trigger fires once
per (conditionId, outcome) anyway — the position record's first_entry_price
is the closest available proxy to "the price when HRH was already committed".

Outputs results/backtest_summary.json and prints a parameter-sweep table.
"""
from __future__ import annotations

import json
import itertools
from collections import defaultdict
from pathlib import Path
from typing import Iterable

ANALYZER_DATA = Path("/home/pranav/tail-analysis/data/sharp_wallets")
WALLET_FILE = ANALYZER_DATA / "0x5268527977f700f9bf9b6d5cd843859e4e70135d.json"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_positions() -> list[dict]:
    """Load HomeRunHazard's closed positions from the analyzer cache."""
    with open(WALLET_FILE) as f:
        data = json.load(f)
    return data["positions"]


def filter_positions(
    positions: list[dict],
    sports: Iterable[str] = ("mlb",),
    pregame_only: bool = False,
    min_stake: float = 0.0,
    require_entry_price: bool = True,
    require_is_winner: bool = True,
) -> list[dict]:
    """Apply the bot's eligibility filters to historical positions.

    Note: pregame_only defaults to False here because only 891 of 12,524
    positions have is_pregame set (analyzer ran Goldsky enrichment on the
    most recent batch only). The live bot will apply the pregame filter
    at execution time via game-start lookup.
    """
    sports_set = {s.lower() for s in sports}
    out = []
    for p in positions:
        if p.get("sport", "").lower() not in sports_set:
            continue
        if pregame_only and not p.get("is_pregame"):
            continue
        if p.get("total_stake", 0) < min_stake:
            continue
        if require_entry_price:
            fep = p.get("first_entry_price")
            if fep is None or fep <= 0 or fep >= 1:
                continue
        if require_is_winner and p.get("is_winner") is None:
            continue
        out.append(p)
    return out


def simulate_trade(
    position: dict,
    bot_stake: float,
    slippage: float,
    kalshi_fee_pct: float,
    price_guard: float | None = None,
) -> dict | None:
    """
    Simulate the bot entering one position.

    Returns None if the price guard blocks the trade (entry price too far
    from HRH's), otherwise a dict with realized PnL details.

    Payout model (Kalshi binary 0..1):
      shares = bot_stake / entry_price
      win  -> payout = shares * 1.0
      loss -> payout = 0
      pnl_gross = payout - bot_stake
      pnl_net   = pnl_gross - (kalshi_fee_pct * bot_stake on win, 0 on loss)
                  (Kalshi fees are roughly proportional to position size; this
                   is a conservative flat-rate approximation.)
    """
    entry_price = position["first_entry_price"] + slippage
    if entry_price <= 0 or entry_price >= 1:
        return None

    if price_guard is not None and slippage > price_guard:
        return None

    shares = bot_stake / entry_price
    is_winner = bool(position.get("is_winner"))

    if is_winner:
        payout = shares * 1.0
        pnl_gross = payout - bot_stake
        fee = kalshi_fee_pct * bot_stake
    else:
        payout = 0.0
        pnl_gross = -bot_stake
        fee = 0.0

    pnl_net = pnl_gross - fee

    return {
        "condition_id": position["condition_id"],
        "market_title": position.get("market_title"),
        "outcome": position.get("outcome"),
        "his_stake": position.get("total_stake"),
        "his_pnl": position.get("realized_pnl"),
        "entry_price": entry_price,
        "shares": shares,
        "is_winner": is_winner,
        "pnl_gross": pnl_gross,
        "pnl_net": pnl_net,
        "first_trade_ts": position.get("first_trade_ts"),
    }


def run_backtest(
    positions: list[dict],
    trigger_usd: float,
    bot_stake: float,
    slippage: float,
    kalshi_fee_pct: float,
) -> dict:
    """Run one parameter combo and return aggregate stats."""
    eligible = filter_positions(positions, min_stake=trigger_usd)

    trades = []
    for p in eligible:
        t = simulate_trade(p, bot_stake, slippage, kalshi_fee_pct)
        if t is not None:
            trades.append(t)

    if not trades:
        return {
            "trigger_usd": trigger_usd,
            "bot_stake": bot_stake,
            "slippage": slippage,
            "kalshi_fee_pct": kalshi_fee_pct,
            "trades": 0,
            "wins": 0,
            "win_rate": 0.0,
            "total_pnl_gross": 0.0,
            "total_pnl_net": 0.0,
            "total_capital_at_risk": 0.0,
            "roi_gross_pct": 0.0,
            "roi_net_pct": 0.0,
            "his_total_pnl_on_same_positions": 0.0,
        }

    wins = sum(1 for t in trades if t["is_winner"])
    total_pnl_gross = sum(t["pnl_gross"] for t in trades)
    total_pnl_net = sum(t["pnl_net"] for t in trades)
    total_capital = bot_stake * len(trades)
    his_total = sum(t["his_pnl"] or 0 for t in trades)

    return {
        "trigger_usd": trigger_usd,
        "bot_stake": bot_stake,
        "slippage": slippage,
        "kalshi_fee_pct": kalshi_fee_pct,
        "trades": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "total_pnl_gross": total_pnl_gross,
        "total_pnl_net": total_pnl_net,
        "total_capital_at_risk": total_capital,
        "roi_gross_pct": 100.0 * total_pnl_gross / total_capital,
        "roi_net_pct": 100.0 * total_pnl_net / total_capital,
        "his_total_pnl_on_same_positions": his_total,
    }


def sweep(positions: list[dict]) -> list[dict]:
    """Sweep over trigger sizes and slippage assumptions."""
    trigger_usds = [500, 1000, 2000, 3000, 4000, 5000, 7500, 10000, 15000]
    slippages = [0.00, 0.01, 0.02, 0.03, 0.04]
    bot_stake = 100.0
    kalshi_fee_pct = 0.02

    rows = []
    for trig, slip in itertools.product(trigger_usds, slippages):
        rows.append(
            run_backtest(positions, trig, bot_stake, slip, kalshi_fee_pct)
        )
    return rows


def print_table(rows: list[dict]) -> None:
    print(
        f"{'trigger':>8} {'slip':>5} {'trades':>7} {'WR%':>5} "
        f"{'PnL_net':>10} {'capital':>10} {'ROI_net%':>9} {'his_PnL':>11}"
    )
    print("-" * 80)
    for r in rows:
        print(
            f"{r['trigger_usd']:>8.0f} "
            f"{r['slippage']:>5.2f} "
            f"{r['trades']:>7d} "
            f"{100*r['win_rate']:>5.1f} "
            f"{r['total_pnl_net']:>10,.0f} "
            f"{r['total_capital_at_risk']:>10,.0f} "
            f"{r['roi_net_pct']:>9.2f} "
            f"{r['his_total_pnl_on_same_positions']:>11,.0f}"
        )


def print_summary(positions: list[dict]) -> None:
    """Sanity-check view of the eligible universe."""
    all_mlb = filter_positions(positions, pregame_only=False, min_stake=0)
    print(
        f"\nUniverse: {len(all_mlb):,} MLB positions with valid entry price "
        f"+ resolved outcome (out of {len(positions):,} total)"
    )

    sport_counts: dict[str, int] = defaultdict(int)
    for p in positions:
        sport_counts[p.get("sport", "?")] += 1
    print("\nSport breakdown (all positions):")
    for s, n in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print(f"  {s:>10}: {n:>6,}")

    buckets = [(0, 500), (500, 1000), (1000, 2000), (2000, 5000),
               (5000, 10000), (10000, 25000), (25000, 100000), (100000, float("inf"))]
    print("\nMLB stake distribution (his_PnL = his realized PnL on bucket):")
    for lo, hi in buckets:
        in_bucket = [p for p in all_mlb if lo <= p["total_stake"] < hi]
        if not in_bucket:
            continue
        wins = sum(1 for p in in_bucket if p["is_winner"])
        wr = 100 * wins / len(in_bucket)
        his_pnl = sum(p["realized_pnl"] or 0 for p in in_bucket)
        his_stake = sum(p["total_stake"] for p in in_bucket)
        his_roi = 100 * his_pnl / his_stake if his_stake else 0
        hi_str = f"{int(hi):,}" if hi != float("inf") else "inf"
        print(
            f"  ${int(lo):>6,}-${hi_str:>7}: n={len(in_bucket):>5} "
            f"WR={wr:>5.1f}%  his_ROI={his_roi:>+6.2f}%  "
            f"his_PnL=${his_pnl:>+10,.0f}"
        )


def main() -> None:
    positions = load_positions()
    print(f"Loaded {len(positions):,} closed positions for HomeRunHazard")

    print_summary(positions)

    print("\n" + "=" * 80)
    print("BACKTEST SWEEP (bot_stake=$100, kalshi_fee=2%)")
    print("=" * 80)
    rows = sweep(positions)
    print_table(rows)

    best = max(rows, key=lambda r: r["roi_net_pct"] if r["trades"] >= 50 else -1e9)
    print("\nBest combo (min 50 trades): "
          f"trigger=${best['trigger_usd']:,.0f}, slip={best['slippage']:.2f}c "
          f"-> ROI_net={best['roi_net_pct']:+.2f}% on {best['trades']} trades")

    out = RESULTS_DIR / "backtest_summary.json"
    with open(out, "w") as f:
        json.dump({"rows": rows, "best": best}, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
