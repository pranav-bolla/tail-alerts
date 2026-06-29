"""
Consensus-tail bot orchestrator.

Flow per cycle:
  1. Source top 100 sharps (cached hourly)
  2. Fetch each sharp's open positions
  3. Run signal_filter (stake_conv >= 85%, sport in {NHL, MLB, soccer}, n >= 2)
  4. For each NEW signal (not already fired):
     a. Find Kalshi ticker via matcher
     b. Verify Kalshi ask price <= consensus_avg + 2c
     c. Place bet (paper or live)
     d. Mark signal as fired
  5. Snapshot state, log to journal

Run forever:
    python -m bot.auto_pilot --mode paper            # paper trade
    python -m bot.auto_pilot --mode live --stake 10  # live with $10 bets

Daily kill switch: stops trading if --max-daily-loss exceeded.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import (  # noqa: E402
    sharp_sourcer, signal_filter, kalshi_matcher, kalshi_executor,
    queue_manager, state, game_date, signal_v2,
)
from dataclasses import asdict  # noqa: E402

POSITIONS_URL = "https://data-api.polymarket.com/positions"

logger = logging.getLogger("auto_pilot")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"auto_pilot_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )


def fetch_positions(wallet: str, limit: int = 200) -> list[dict]:
    try:
        r = requests.get(
            POSITIONS_URL,
            params={"user": wallet, "sizeThreshold": 100, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"positions fetch failed for {wallet[:10]}: {e}")
        return []


def get_kalshi_client():
    """Lazy-import the Kalshi client only in live mode."""
    sys.path.insert(0, "/home/pranav/KalshiBot")
    from kalshi_client_rsa import KalshiClientRSA  # type: ignore
    from auth_config_local import KEY_ID, KEY_FILE  # type: ignore
    return KalshiClientRSA(key_id=KEY_ID, key_file=KEY_FILE)


def one_cycle(args, st: dict, kalshi_client=None) -> int:
    """Run one full cycle. Returns number of bets placed."""
    logger.info("=== Starting cycle ===")

    # 1. Source sharps
    sharps = sharp_sourcer.get_top_sharps(
        target_n=args.n_sharps,
        min_week_pnl=args.min_pnl,
        max_hours_idle=args.max_idle_hours,
    )
    if len(sharps) < 5:
        logger.warning(f"only {len(sharps)} active sharps in pool, skipping cycle")
        return 0
    sharp_meta = {s["wallet"]: s for s in sharps}

    # 2. Fetch positions per sharp
    logger.info(f"fetching positions for {len(sharps)} sharps...")
    positions_by_wallet = {}
    for s in sharps:
        positions = fetch_positions(s["wallet"])
        if positions:
            positions_by_wallet[s["wallet"]] = positions
        time.sleep(0.1)
    total_positions = sum(len(p) for p in positions_by_wallet.values())
    logger.info(f"  pulled {total_positions} positions across {len(positions_by_wallet)} sharps")

    # 3. Build signals
    signals = signal_filter.build_signals(
        positions_by_wallet, sharp_meta,
        allow_optional_sports=args.allow_nfl,
    )
    st["stats"]["signals_seen"] += len(signals)
    logger.info(f"  generated {len(signals)} signals passing filter")

    if not signals:
        return 0

    # Attach Polymarket gameStartTime for MLB (slug date is NOT first pitch).
    mlb_cids = list({s.cid for s in signals if s.sport == "mlb"})
    if mlb_cids:
        start_times = game_date.fetch_game_start_times(mlb_cids)
        for s in signals:
            if s.cid in start_times:
                s.game_start_time = start_times[s.cid]

    # 4. Load Kalshi markets ONCE per cycle
    kalshi_markets = kalshi_matcher.get_kalshi_markets()
    logger.info(f"  {len(kalshi_markets)} open Kalshi markets in pool")

    placed = 0
    currently_firing = {}  # for queue_manager reconciliation

    for sig in signals:
        # Kill switch check (only relevant for live mode)
        if args.mode == "live" and state.check_kill_switch(st, args.max_daily_loss):
            logger.warning(f"KILL SWITCH ACTIVE -- skipping remaining signals "
                           f"(daily_pnl=${st['daily_pnl']:+,.2f})")
            break

        # 4a. Try Kalshi match (best-effort hint)
        match = kalshi_matcher.match_kalshi_ticker(
            sig.title, sig.outcome, kalshi_markets=kalshi_markets,
            game_start_time=getattr(sig, "game_start_time", None),
        )

        sig_dict = asdict(sig)

        # In signal mode: always feed the queue manager, regardless of match.
        # The queue manager handles dedup, lifecycle, and queue.md regeneration.
        if args.mode == "signal":
            key = queue_manager.signal_key(sig.cid, sig.outcome)
            currently_firing[key] = (sig_dict, match)
            placed += 1
            st["stats"]["signals_fired"] += 1
            continue

        # PAPER / LIVE modes — require a Kalshi match
        if state.already_fired(st, sig.cid, sig.outcome):
            logger.debug(f"  already fired: {sig.title[:40]} -> {sig.outcome}")
            continue
        if not match:
            logger.info(f"  no Kalshi match for: {sig.title[:50]} -> {sig.outcome}")
            st["stats"]["matches_failed"] += 1
            state.journal("no_kalshi_match",
                          poly_title=sig.title, poly_outcome=sig.outcome,
                          sport=sig.sport)
            continue

        # 4b. Price guard
        if not match.get("price_to_pay") or match["price_to_pay"] <= 0:
            logger.info(f"  no ask price for {match['ticker']}, skipping")
            continue

        if match["price_to_pay"] > sig.max_kalshi_price:
            logger.info(f"  price guard failed: kalshi_ask={match['price_to_pay']:.3f} "
                        f"> max={sig.max_kalshi_price:.3f} for {sig.title[:40]}")
            st["stats"]["price_guard_failed"] += 1
            state.journal("price_guard_failed",
                          ticker=match["ticker"], side=match["side"],
                          kalshi_ask=match["price_to_pay"],
                          consensus_avg=sig.consensus_avg_entry,
                          max_kalshi=sig.max_kalshi_price)
            continue

        # Cap on concurrent open bets (paper/live only)
        if len(st["open_bets"]) >= args.max_open_bets:
            logger.info(f"  max_open_bets={args.max_open_bets} reached, holding")
            break

        # 4c. Execute
        bet = kalshi_executor.execute_bet(
            signal={
                "cid": sig.cid, "outcome": sig.outcome, "title": sig.title,
                "sport": sig.sport, "consensus_n": sig.consensus_n,
                "stake_conviction": sig.stake_conviction,
                "consensus_avg_entry": sig.consensus_avg_entry,
            },
            kalshi_match=match,
            stake_dollars=args.stake,
            mode=args.mode,
            kalshi_client=kalshi_client,
        )
        if bet and bet.get("status") in ("paper", "live"):
            state.record_bet(st, bet)
            state.mark_fired(st, sig.cid, sig.outcome)
            state.journal("bet_placed", **bet)
            placed += 1
            logger.info(f"  ✓ FIRED [{bet['mode']}] {bet['ticker']}/{bet['side']} "
                        f"@ {bet['price_cents']}c x{bet['count']} = ${bet['stake_actual']:.2f}")
            st["stats"]["signals_fired"] += 1
        else:
            logger.warning(f"  ✗ FAILED to execute {sig.title[:40]}")

    # Reconcile signal queue (signal mode only)
    if args.mode == "signal":
        q_state = queue_manager.load_queue()
        summary = queue_manager.reconcile(q_state, currently_firing,
                                          run_resolution_check=True)
        email_tag = ""
        if summary.get("emailed"):
            email_tag = f" [emailed {summary.get('email_count', 0)}]"
        logger.info(
            f"  queue: {summary['new']} new, {summary['refreshed']} refreshed, "
            f"{summary['went_stale']} -> stale, "
            f"{summary['resolved']} -> resolved, {summary['pruned']} pruned"
            f"{email_tag}"
        )

    state.save_state(st)
    logger.info(f"=== Cycle done: {placed} signals fired, "
                f"{len(st['open_bets'])} open total, "
                f"daily_pnl=${st['daily_pnl']:+,.2f} ===\n")
    return placed


def one_cycle_v2(args, st: dict) -> int:
    """V2 signal pipeline: screen sharps, event thesis, adds — no auto Kalshi."""
    logger.info("=== Starting V2 cycle ===")

    sharps = sharp_sourcer.get_top_sharps(
        target_n=args.n_sharps,
        min_week_pnl=args.min_pnl,
        max_hours_idle=args.max_idle_hours,
    )
    if len(sharps) < 5:
        logger.warning(f"only {len(sharps)} active sharps in pool, skipping cycle")
        return 0
    sharp_meta = {s["wallet"]: s for s in sharps}

    pos_limit = 500 if args.mode == "signal-v2" else 200
    logger.info(f"fetching positions for {len(sharps)} sharps (limit={pos_limit})...")
    positions_by_wallet = {}
    for s in sharps:
        positions = fetch_positions(s["wallet"], limit=pos_limit)
        if positions:
            positions_by_wallet[s["wallet"]] = positions
        time.sleep(0.1)
    total_positions = sum(len(p) for p in positions_by_wallet.values())
    logger.info(f"  pulled {total_positions} positions across {len(positions_by_wallet)} sharps")

    signals, profiles, adds = signal_v2.build_v2_signals(
        positions_by_wallet, sharp_meta,
    )
    eligible = sum(1 for p in profiles.values() if p.eligible)
    excluded = sum(1 for p in profiles.values() if not p.eligible)
    logger.info(
        f"  sharp screen: {eligible} eligible, {excluded} excluded (MM/low-directional)"
    )
    by_type = {"thesis": 0, "cluster": 0, "add": 0}
    for s in signals:
        by_type[s.signal_type] = by_type.get(s.signal_type, 0) + 1
    logger.info(
        f"  generated {len(signals)} V2 signals "
        f"(thesis={by_type.get('thesis', 0)}, cluster={by_type.get('cluster', 0)}, "
        f"add={by_type.get('add', 0)})"
    )
    st["stats"]["signals_seen"] += len(signals)

    if not signals:
        q_state = queue_manager.load_queue()
        summary = queue_manager.reconcile(
            q_state, {}, run_resolution_check=True,
            send_email=not args.dry_run,
        )
        logger.info(
            f"  queue: 0 firing, {summary['went_stale']} -> stale, "
            f"{summary['resolved']} -> resolved"
        )
        state.save_state(st)
        return 0

    currently_firing = {}
    for sig in signals:
        sig_dict = sig.to_signal_dict()
        key = sig_dict.get("key") or queue_manager.signal_key(
            sig_dict.get("cid") or "", sig_dict.get("outcome") or "",
        )
        currently_firing[key] = (sig_dict, None)

    q_state = queue_manager.load_queue()
    summary = queue_manager.reconcile(
        q_state, currently_firing,
        run_resolution_check=True,
        send_email=not args.dry_run,
    )
    email_tag = ""
    if summary.get("emailed"):
        email_tag = f" [emailed {summary.get('email_count', 0)}]"
    logger.info(
        f"  queue: {summary['new']} new, {summary['refreshed']} refreshed, "
        f"{summary['went_stale']} -> stale, "
        f"{summary['resolved']} -> resolved, {summary['pruned']} pruned"
        f"{email_tag}"
    )

    for sig in signals[:10]:
        logger.info(
            f"  [{sig.signal_type.upper()}] {sig.title[:50]} -> "
            f"{sig.action_outcome} | {sig.consensus_n:.1f} eff, "
            f"${sig.consensus_stake:,.0f} | {sig.thesis_label}"
        )

    st["stats"]["signals_fired"] += len(signals)
    state.save_state(st)
    logger.info(f"=== V2 cycle done: {len(signals)} signals ===\n")
    return len(signals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["signal", "signal-v2", "paper", "live"],
                    default="signal",
                    help="signal=v1 review queue; signal-v2=event thesis pipeline; "
                         "paper=log simulated Kalshi orders; "
                         "live=place real Kalshi orders")
    ap.add_argument("--stake", type=float, default=10.0,
                    help="$ per bet (default $10)")
    ap.add_argument("--cycle-seconds", type=int, default=600,
                    help="seconds between cycles (default 600 = 10 min)")
    ap.add_argument("--n-sharps", type=int, default=100,
                    help="number of top sharps to poll")
    ap.add_argument("--min-pnl", type=float, default=50_000,
                    help="min week PnL ($) for a sharp to qualify")
    ap.add_argument("--max-idle-hours", type=int, default=72,
                    help="max hours since last trade for a sharp")
    ap.add_argument("--max-daily-loss", type=float, default=50.0,
                    help="kill switch threshold ($)")
    ap.add_argument("--max-open-bets", type=int, default=20,
                    help="cap on concurrent open bets")
    ap.add_argument("--allow-nfl", action="store_true",
                    help="also tail NFL signals (only +7% ROI in backtest)")
    ap.add_argument("--one-shot", action="store_true",
                    help="run one cycle and exit (smoke test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="skip email sends (useful with --one-shot)")
    args = ap.parse_args()

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    setup_logging(log_dir)

    logger.info("==================================================")
    logger.info("CONSENSUS-TAIL BOT START")
    logger.info(f"  mode={args.mode}, stake=${args.stake:.2f}, "
                f"cycle={args.cycle_seconds}s")
    logger.info(f"  max_daily_loss=${args.max_daily_loss}, "
                f"max_open_bets={args.max_open_bets}")
    logger.info("==================================================")

    st = state.load_state()
    kalshi_client = None
    if args.mode == "live":
        logger.info("initializing Kalshi RSA client...")
        kalshi_client = get_kalshi_client()
        try:
            bal = kalshi_client.get_balance()
            logger.info(f"  Kalshi balance: {json.dumps(bal)[:200]}")
        except Exception as e:
            logger.exception(f"  Kalshi auth check failed: {e}")
            sys.exit(2)

    if args.one_shot:
        if args.mode == "signal-v2":
            one_cycle_v2(args, st)
        else:
            one_cycle(args, st, kalshi_client)
        return

    while True:
        try:
            if args.mode == "signal-v2":
                one_cycle_v2(args, st)
            else:
                one_cycle(args, st, kalshi_client)
        except Exception as e:
            logger.exception(f"cycle error: {e}")
            state.journal("cycle_error", error=str(e))
        time.sleep(args.cycle_seconds)


if __name__ == "__main__":
    main()
