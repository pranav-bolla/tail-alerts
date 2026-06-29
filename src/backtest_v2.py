"""
Backtest v2 — uses the fully-enriched position history from enrich_history.py.

Unlike v1, this uses ALL positions where we now have first_trade_ts and
first_entry_price (not just the recent 891). Should give a much larger
backtest sample.

Strategy: same as v1 — trigger on cumulative size threshold, simulate the
bot entering at (first_entry_price + slippage), compute realized PnL
based on the position's is_winner flag.
"""
from __future__ import annotations

import json
import itertools
from datetime import datetime
from collections import defaultdict
from pathlib import Path

ENRICHED_FILE = Path(__file__).resolve().parent.parent / "data" / "hrh_enriched_positions.json"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_positions() -> list[dict]:
    with open(ENRICHED_FILE) as f:
        data = json.load(f)
    return data["positions"]


def is_usable(p: dict) -> bool:
    """Position has enough data for the backtest."""
    if p.get("sport", "").lower() != "mlb":
        return False
    fep = p.get("first_entry_price")
    if fep is None or fep <= 0 or fep >= 1:
        return False
    if p.get("is_winner") is None:
        return False
    return True


def simulate_trade(p: dict, bot_stake: float, slippage: float, kalshi_fee_pct: float) -> dict | None:
    entry = p["first_entry_price"] + slippage
    if entry <= 0 or entry >= 1:
        return None

    shares = bot_stake / entry
    if p["is_winner"]:
        pnl_gross = shares * 1.0 - bot_stake
        fee = kalshi_fee_pct * bot_stake
    else:
        pnl_gross = -bot_stake
        fee = 0.0

    return {
        "pnl_gross": pnl_gross,
        "pnl_net": pnl_gross - fee,
        "is_winner": p["is_winner"],
        "first_trade_ts": p.get("first_trade_ts"),
        "stake": p["total_stake"],
        "his_pnl": p.get("realized_pnl", 0),
    }


def run_sweep(positions: list[dict]) -> list[dict]:
    usable = [p for p in positions if is_usable(p)]
    print(f"\nUsable MLB positions: {len(usable):,}")

    trigger_usds = [500, 1000, 2000, 3000, 4000, 5000, 7500, 10000, 15000, 20000]
    slippages = [0.00, 0.01, 0.02, 0.03, 0.04]
    bot_stake = 100.0
    kalshi_fee_pct = 0.02

    rows = []
    for trig, slip in itertools.product(trigger_usds, slippages):
        cohort = [p for p in usable if p["total_stake"] >= trig]
        trades = [t for p in cohort if (t := simulate_trade(p, bot_stake, slip, kalshi_fee_pct))]
        if not trades:
            continue
        wins = sum(1 for t in trades if t["is_winner"])
        pnl_net = sum(t["pnl_net"] for t in trades)
        capital = bot_stake * len(trades)
        his_total = sum(t["his_pnl"] for t in trades)
        rows.append({
            "trigger_usd": trig,
            "slippage": slip,
            "trades": len(trades),
            "wins": wins,
            "win_rate": wins / len(trades),
            "total_pnl_net": pnl_net,
            "total_capital": capital,
            "roi_net_pct": 100.0 * pnl_net / capital,
            "his_total_pnl": his_total,
        })
    return rows


def print_table(rows: list[dict]) -> None:
    print(f"\n{'trigger':>8} {'slip':>5} {'trades':>7} {'WR%':>5} "
          f"{'PnL_net':>12} {'capital':>12} {'ROI_net%':>9} {'his_PnL':>12}")
    print("-" * 90)
    for r in rows:
        print(f"{r['trigger_usd']:>8.0f} {r['slippage']:>5.2f} {r['trades']:>7d} "
              f"{100*r['win_rate']:>5.1f} {r['total_pnl_net']:>+12,.0f} "
              f"{r['total_capital']:>12,.0f} {r['roi_net_pct']:>+9.2f} "
              f"{r['his_total_pnl']:>+12,.0f}")


def print_monthly(positions: list[dict], min_stake: float = 5000) -> None:
    """Walk-forward view: per-month results at chosen trigger."""
    usable = [p for p in positions if is_usable(p) and p["total_stake"] >= min_stake and p.get("first_trade_ts")]
    by_month: dict[str, list[dict]] = defaultdict(list)
    for p in usable:
        dt = datetime.fromtimestamp(p["first_trade_ts"])
        by_month[f"{dt.year}-{dt.month:02d}"].append(p)

    print(f"\n=== Monthly walk-forward at trigger=${min_stake:,.0f}, slip=2c, $100 stake ===")
    print(f"{'month':>10} {'n':>4} {'WR%':>5} {'bot_PnL':>10} {'his_PnL':>12} {'cum_bot':>10}")
    print("-" * 60)
    cumulative = 0.0
    for month in sorted(by_month):
        cohort = by_month[month]
        wins = sum(1 for p in cohort if p["is_winner"])
        wr = 100 * wins / len(cohort)
        bot_pnl = 0.0
        for p in cohort:
            ep = p["first_entry_price"] + 0.02
            if ep >= 1: continue
            if p["is_winner"]:
                bot_pnl += (100 / ep) - 100 - 2  # -$2 fee
            else:
                bot_pnl -= 100
        cumulative += bot_pnl
        his_pnl = sum(p.get("realized_pnl", 0) for p in cohort)
        print(f"{month:>10} {len(cohort):>4} {wr:>5.1f} "
              f"{bot_pnl:>+10,.0f} {his_pnl:>+12,.0f} {cumulative:>+10,.0f}")


def main() -> None:
    if not ENRICHED_FILE.exists():
        print(f"Enriched data not found at {ENRICHED_FILE}")
        print("Run src/enrich_history.py first.")
        return

    positions = load_positions()
    print(f"Loaded {len(positions):,} positions from enriched file")

    rows = run_sweep(positions)
    print_table(rows)

    print_monthly(positions, min_stake=5000)
    print_monthly(positions, min_stake=2000)

    out = RESULTS_DIR / "backtest_v2_summary.json"
    with open(out, "w") as f:
        json.dump({"rows": rows}, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
