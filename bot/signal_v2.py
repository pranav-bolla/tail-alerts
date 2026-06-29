"""
V2 signal pipeline: screen sharps → event thesis + ML clusters + conviction adds.
"""
from __future__ import annotations

from bot import sharp_screener, event_thesis, position_delta, game_date
from bot.event_thesis import V2Signal


def build_v2_signals(
    positions_by_wallet: dict[str, list[dict]],
    sharp_meta: dict[str, dict],
) -> tuple[list[V2Signal], dict[str, sharp_screener.SharpProfile], list]:
    profiles = sharp_screener.screen_pool(positions_by_wallet, sharp_meta)
    eligible = sharp_screener.eligible_wallets(profiles)

    thesis_and_cluster = event_thesis.build_event_signals(
        positions_by_wallet, sharp_meta, eligible,
    )
    adds = position_delta.detect_adds(positions_by_wallet, sharp_meta, eligible)

    # Convert adds to V2Signal-like objects for unified pipeline
    add_signals = []
    for a in adds:
        d = a.to_v2_signal_dict()
        add_signals.append(V2Signal(
            signal_type="add",
            key=d["key"],
            sport=d["sport"],
            title=d["title"],
            slug=d["slug"],
            thesis_label=d["thesis_label"],
            action_outcome=d["action_outcome"],
            cid=d["cid"],
            consensus_n=1,
            raw_sharp_count=1,
            consensus_stake=d["consensus_stake"],
            stake_conviction=1.0,
            consensus_avg_entry=d["consensus_avg_entry"],
            cur_price=d["cur_price"],
            max_kalshi_price=d["max_kalshi_price"],
            contributing_sharps=d["contributing_sharps"],
        ))

    all_signals = thesis_and_cluster + add_signals
    all_signals.sort(
        key=lambda s: (s.signal_type != "add", s.consensus_stake),
        reverse=True,
    )

    # Attach game start times for MLB
    mlb_cids = list({s.cid for s in all_signals if s.sport == "mlb" and s.cid})
    if mlb_cids:
        times = game_date.fetch_game_start_times(mlb_cids)
        for s in all_signals:
            if s.cid and s.cid in times:
                s.game_start_time = times[s.cid]

    return all_signals, profiles, adds
