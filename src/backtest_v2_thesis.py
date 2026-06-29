"""
Backtest V2 event-thesis signals on historical sharp wallet cache.

Replays closed positions through sharp screening + event_thesis rules and
measures win rate / ROI if you had tailed at avg sharp entry + 2c slip.

Data: /home/pranav/tail-analysis/data/sharp_wallets/*.json

Run:
    python -m src.backtest_v2_thesis
    python -m src.backtest_v2_thesis --compare-v1
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import event_thesis  # noqa: E402
from bot.signal_filter import derive_sport  # noqa: E402

CACHE_DIR = Path("/home/pranav/tail-analysis/data/sharp_wallets")
OUT_FILE = ROOT / "results" / "backtest_v2_thesis.json"

KALSHI_SLIP_CENTS = 2.0
KALSHI_FEE_PCT = 0.07
STAKE = 100.0
MIN_DIR_RATE = 0.70
MIN_STAKE = 3_000


def load_sharps() -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Load winning sharps + pseudo-live position dicts for screening."""
    sharp_meta: dict[str, dict] = {}
    positions_by_wallet: dict[str, list[dict]] = {}

    for f in glob.glob(str(CACHE_DIR / "0x*.json")):
        if "_clv" in f:
            continue
        with open(f) as fh:
            d = json.load(fh)
        pnl = d.get("overall", {}).get("realized_pnl", 0)
        if pnl <= 0:
            continue
        if len(d.get("positions", [])) < 50:
            continue

        wallet = d["wallet"]
        name = d.get("display_name", "?")
        dir_rate = d.get("directionality", {}).get("directional_rate", 1.0)

        sharp_meta[wallet] = {
            "wallet": wallet,
            "name": name,
            "directional_rate": dir_rate,
        }

        live_positions = []
        for p in d["positions"]:
            if p.get("is_winner") is None:
                continue
            if p.get("is_pregame") is False:
                continue
            stake = float(p.get("total_stake") or 0)
            if stake < MIN_STAKE:
                continue
            slug = p.get("event_slug") or p.get("market_slug") or ""
            if not derive_sport(slug):
                continue
            entry = p.get("avg_buy_price") or p.get("first_entry_price")
            if not entry or not (0 < entry < 1):
                continue

            live_positions.append({
                "conditionId": p["condition_id"],
                "outcome": p["outcome"],
                "title": p.get("market_title") or "?",
                "eventSlug": slug,
                "slug": slug,
                "totalBought": stake / entry,
                "avgPrice": entry,
                "curPrice": entry,
                "_is_winner": p["is_winner"],
                "_entry": entry,
                "_stake": stake,
            })
        if live_positions:
            positions_by_wallet[wallet] = live_positions

    return sharp_meta, positions_by_wallet


def eligible_from_history(
    positions_by_wallet: dict[str, list[dict]],
    sharp_meta: dict[str, dict],
) -> set[str]:
    """Use cached directionality (not full-book MM screen on closed history)."""
    eligible = set()
    for wallet in positions_by_wallet:
        meta = sharp_meta.get(wallet, {})
        if meta.get("directional_rate", 1.0) >= MIN_DIR_RATE:
            eligible.add(wallet)
    return eligible


def resolve_signal(sig: event_thesis.V2Signal,
                   positions_by_wallet: dict[str, list[dict]]) -> dict | None:
    """Map a V2 signal to win/loss using historical is_winner flags."""
    winners = []
    entries = []
    for h in sig.contributing_sharps:
        wallet_prefix = h.get("wallet", "").replace("...", "")
        for wallet, positions in positions_by_wallet.items():
            if not wallet.startswith(wallet_prefix):
                continue
            for p in positions:
                if p["conditionId"] != sig.cid:
                    continue
                if p["outcome"] != sig.action_outcome:
                    continue
                winners.append(p["_is_winner"])
                entries.append(p["_entry"])
    if not winners:
        return None
    won = all(winners)  # should be uniform
    avg_entry = sig.consensus_avg_entry or mean(entries)
    exec_price = min(0.97, avg_entry + KALSHI_SLIP_CENTS / 100)
    shares = STAKE / exec_price
    if won:
        pnl_net = (shares - STAKE) * (1 - KALSHI_FEE_PCT)
    else:
        pnl_net = -STAKE
    return {
        "signal_type": sig.signal_type,
        "sport": sig.sport,
        "title": sig.title,
        "slug": sig.slug,
        "thesis_label": sig.thesis_label,
        "action_outcome": sig.action_outcome,
        "consensus_n": sig.consensus_n,
        "consensus_stake": sig.consensus_stake,
        "stake_conviction": sig.stake_conviction,
        "avg_entry": avg_entry,
        "exec_price": exec_price,
        "won": won,
        "pnl_net": pnl_net,
        "sharps": [h["name"] for h in sig.contributing_sharps],
    }


def build_v1_signals(positions_by_wallet, sharp_meta, eligible):
    """Old per-market consensus for comparison."""
    from collections import defaultdict as dd

    market_holders = dd(lambda: dd(list))
    market_meta = {}
    for wallet, positions in positions_by_wallet.items():
        if wallet not in eligible:
            continue
        name = sharp_meta[wallet]["name"]
        for p in positions:
            cid = p["conditionId"]
            market_holders[cid][p["outcome"]].append({
                "wallet": wallet, "name": name,
                "stake": p["_stake"], "is_winner": p["_is_winner"],
                "entry": p["_entry"],
            })
            market_meta[cid] = {
                "sport": derive_sport(p["eventSlug"]),
                "title": p["title"],
                "slug": p["eventSlug"],
            }

    rows = []
    for cid, by_out in market_holders.items():
        if cid not in market_meta:
            continue
        outcomes = sorted(by_out.items(),
                          key=lambda x: (len(x[1]), sum(h["stake"] for h in x[1])),
                          reverse=True)
        if len(outcomes) < 2:
            continue
        cons_out, cons = outcomes[0]
        opp_out, opp = outcomes[1]
        cons_stake = sum(h["stake"] for h in cons)
        opp_stake = sum(h["stake"] for h in opp)
        total = cons_stake + opp_stake
        if len(cons) < 2 or cons_stake / total < 0.85:
            continue
        if cons_stake < 1_000:
            continue
        won = all(h["is_winner"] for h in cons)
        avg_e = mean(h["entry"] for h in cons)
        exec_p = min(0.97, avg_e + KALSHI_SLIP_CENTS / 100)
        shares = STAKE / exec_p
        pnl = (shares - STAKE) * (1 - KALSHI_FEE_PCT) if won else -STAKE
        rows.append({
            "signal_type": "v1",
            "sport": market_meta[cid]["sport"],
            "title": market_meta[cid]["title"],
            "won": won, "pnl_net": pnl, "consensus_n": len(cons),
            "consensus_stake": cons_stake,
        })
    return rows


def summarize(rows: list[dict], label: str) -> dict | None:
    if not rows:
        return None
    n = len(rows)
    wr = sum(1 for r in rows if r["won"]) / n
    pnl = sum(r["pnl_net"] for r in rows)
    cap = n * STAKE
    by_sport = defaultdict(list)
    for r in rows:
        by_sport[r["sport"]].append(r)
    return {
        "label": label,
        "n": n,
        "wr": wr,
        "pnl_net": pnl,
        "roi_net": pnl / cap,
        "by_sport": {
            s: {
                "n": len(v),
                "wr": sum(1 for x in v if x["won"]) / len(v),
                "roi": sum(x["pnl_net"] for x in v) / (len(v) * STAKE),
            }
            for s, v in sorted(by_sport.items(), key=lambda x: -len(x[1]))
        },
    }


def print_summary(s: dict | None) -> None:
    if not s:
        print("  (no signals)")
        return
    print(f"\n{s['label']}")
    print(f"  Signals: {s['n']:,}  |  WR: {100*s['wr']:.1f}%  |  "
          f"PnL: ${s['pnl_net']:+,.0f}  |  ROI: {100*s['roi_net']:+.2f}%")
    for sport, stats in s["by_sport"].items():
        print(f"    {sport:8} n={stats['n']:4}  WR={100*stats['wr']:5.1f}%  "
              f"ROI={100*stats['roi']:+.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare-v1", action="store_true")
    args = ap.parse_args()

    print("Loading historical sharp wallets...")
    sharp_meta, positions_by_wallet = load_sharps()
    print(f"  {len(sharp_meta)} winning sharps, "
          f"{len(positions_by_wallet)} with qualifying positions")

    eligible = eligible_from_history(positions_by_wallet, sharp_meta)
    print(f"  {len(eligible)} pass V2 directional/MM screen")

    # One signal per event slug — dedupe by key
    all_signals = event_thesis.build_event_signals(
        positions_by_wallet, sharp_meta, eligible,
    )
    resolved = []
    for sig in all_signals:
        row = resolve_signal(sig, positions_by_wallet)
        if row:
            resolved.append(row)

    thesis = [r for r in resolved if r["signal_type"] == "thesis"]
    cluster = [r for r in resolved if r["signal_type"] == "cluster"]
    all_v2 = resolved

    print_summary(summarize(all_v2, "V2 ALL (thesis + cluster)"))
    print_summary(summarize(thesis, "V2 THESIS only"))
    print_summary(summarize(cluster, "V2 CLUSTER only (strict ML)"))

    if args.compare_v1:
        v1 = build_v1_signals(positions_by_wallet, sharp_meta, eligible)
        print_summary(summarize(v1, "V1-style per-market consensus (same eligible pool)"))

    # Show a few example thesis signals
    print("\nSample thesis signals (most recent by stake):")
    for r in sorted(thesis, key=lambda x: -x["consensus_stake"])[:8]:
        mark = "W" if r["won"] else "L"
        print(f"  [{mark}] {r['sport']:6} {r['title'][:45]:45} | "
              f"{r['thesis_label'][:30]} | ${r['consensus_stake']:,.0f} | "
              f"{r['consensus_n']:.1f} eff | {', '.join(r['sharps'][:3])}")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({"all": all_v2, "thesis": thesis, "cluster": cluster}, f, indent=2)
    print(f"\nWrote {len(all_v2)} signals to {OUT_FILE}")


if __name__ == "__main__":
    main()
