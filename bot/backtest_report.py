"""
Report on manual-strategy backtest ledger.

Assumes $stake per signal at entry price (default: max_kalshi_price = poly avg + 2c).
Kalshi fee: 7% on profit (same as historical backtests).

    python -m bot.backtest_report
    python -m bot.backtest_report --stake 10 --entry max_kalshi
    python -m bot.backtest_report --sport mlb
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from bot.backtest_ledger import load_ledger

KALSHI_FEE = 0.07


def build_report(stake: float = 10.0, entry_field: str = "max_kalshi_price",
                 sport_filter: str | None = None) -> dict:
    rows = load_ledger()
    opened = {}
    resolved = {}

    for rec in rows:
        key = rec["signal_key"]
        if rec["event"] == "signal_opened":
            opened[key] = rec
        elif rec["event"] == "signal_resolved":
            resolved[key] = rec

    trades = []
    for key, sig in opened.items():
        if sport_filter and sig.get("sport") != sport_filter:
            continue
        res = resolved.get(key)
        entry = sig.get(entry_field)
        if entry is None or entry <= 0 or entry >= 1:
            entry = sig.get("consensus_avg_entry")
        if not entry or entry <= 0:
            continue

        shares = stake / entry
        rec = {
            "signal_key": key,
            "ts": sig["ts"],
            "sport": sig["sport"],
            "title": sig["title"],
            "outcome": sig["outcome"],
            "consensus_n": sig["consensus_n"],
            "consensus_stake": sig["consensus_stake"],
            "stake_conviction": sig["stake_conviction"],
            "entry": entry,
            "cur_price_at_signal": sig["cur_price"],
            "status": "open",
            "pnl": None,
        }
        if res and res.get("won") is not None:
            rec["status"] = "won" if res["won"] else "lost"
            if res["won"]:
                gross = shares * (1.0 - entry)
                rec["pnl"] = gross * (1 - KALSHI_FEE)
            else:
                rec["pnl"] = -stake
        trades.append(rec)

    settled = [t for t in trades if t["pnl"] is not None]
    open_ = [t for t in trades if t["pnl"] is None]
    wins = sum(1 for t in settled if t["pnl"] > 0)
    losses = sum(1 for t in settled if t["pnl"] <= 0)
    total_pnl = sum(t["pnl"] for t in settled)

    by_sport = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
    for t in settled:
        s = by_sport[t["sport"]]
        s["n"] += 1
        s["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            s["w"] += 1
        else:
            s["l"] += 1

    return {
        "total_signals": len(trades),
        "open": len(open_),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(settled) if settled else None,
        "total_pnl": total_pnl,
        "roi_on_stake": total_pnl / (stake * len(settled)) if settled else None,
        "by_sport": dict(by_sport),
        "trades": sorted(trades, key=lambda t: t["ts"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--entry", choices=["max_kalshi_price", "consensus_avg_entry", "cur_price"],
                    default="max_kalshi_price")
    ap.add_argument("--sport", default=None)
    args = ap.parse_args()

    r = build_report(stake=args.stake, entry_field=args.entry, sport_filter=args.sport)

    print("=== MANUAL STRATEGY BACKTEST (ledger) ===")
    print(f"  Signals logged:  {r['total_signals']}")
    print(f"  Still open:      {r['open']}")
    print(f"  Settled:         {r['settled']}")
    if r["settled"]:
        print(f"  Record:          {r['wins']}-{r['losses']}  "
              f"({r['win_rate']:.0%} WR)")
        print(f"  PnL (@ ${args.stake:.0f}/bet): ${r['total_pnl']:+,.2f}")
        print(f"  ROI on deployed: {r['roi_on_stake']:.1%}")
    else:
        print("  No settled signals yet — check back after games finish.")

    if r["by_sport"]:
        print("\n--- By sport ---")
        for sport, s in sorted(r["by_sport"].items()):
            wr = s["w"] / s["n"] if s["n"] else 0
            print(f"  {sport:>8}: {s['w']}-{s['l']} ({wr:.0%})  pnl=${s['pnl']:+,.2f}")

    if r["trades"]:
        print("\n--- All signals ---")
        for t in r["trades"]:
            pnl_str = f"${t['pnl']:+.2f}" if t["pnl"] is not None else "open"
            print(f"  [{t['sport']:>6}] {t['title'][:45]:45s} -> {t['outcome'][:20]:20s}  "
                  f"n={t['consensus_n']} ${t['consensus_stake']:>8,.0f}  "
                  f"entry={t['entry']:.3f}  {pnl_str}")


if __name__ == "__main__":
    main()
