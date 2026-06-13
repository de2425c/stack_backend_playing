#!/usr/bin/env python3
"""
Backfill (and resync) user_cash_stats/{uid} from existing bot_sessions.

The Ranks-tab cash leaderboards (hands played / chips won) read a per-user
lifetime counter at user_cash_stats/{uid} instead of client-aggregating the
most recent bot_sessions (which undercounts anyone whose history falls outside
the rolling scan window). Going forward the backend increments that counter in
process_session; this script seeds it for sessions that predate that change.

It is an AUTHORITATIVE RECOMPUTE: it sums every cash bot_sessions doc per user
and SETS the absolute totals (overwrites). Because bot_sessions is the source
of truth and each session is counted exactly once, the script is idempotent and
safe to re-run at any time to resync (e.g. if the counter ever drifts).

Duels live in `duels` with their own prize math, so duel-stake sessions are
excluded to match the leaderboard definition.

Usage:
    python scripts/backfill_user_cash_stats.py          # write
    python scripts/backfill_user_cash_stats.py --dry-run # report only
"""

import sys

import firebase_admin
from firebase_admin import firestore


def backfill(dry_run: bool = False):
    if not firebase_admin._apps:
        firebase_admin.initialize_app()

    db = firestore.client()

    print("Scanning bot_sessions...")
    hands_by_user: dict[str, int] = {}
    profit_by_user: dict[str, int] = {}
    last_session_by_user: dict[str, object] = {}
    scanned = 0
    skipped_duel = 0

    for doc in db.collection("bot_sessions").stream():
        data = doc.to_dict() or {}
        user_id = data.get("user_id")
        if not user_id:
            continue
        stake_id = data.get("stake_id") or ""
        # Match the leaderboard's cash-only definition.
        if stake_id.startswith("duel"):
            skipped_duel += 1
            continue
        scanned += 1
        hands_by_user[user_id] = hands_by_user.get(user_id, 0) + int(
            data.get("hands_played") or 0
        )
        profit_by_user[user_id] = profit_by_user.get(user_id, 0) + int(
            data.get("profit_cents") or 0
        )
        # Track each user's most recent session end so updated_at reflects real
        # last-played time. The leaderboard's "active in the last 2 weeks" filter
        # reads this — stamping SERVER_TIMESTAMP would falsely mark every user
        # active at backfill time. (Going forward the per-session increment uses
        # SERVER_TIMESTAMP, which is correct: a session just happened = now.)
        end_time = data.get("end_time")
        if end_time is not None:
            prev = last_session_by_user.get(user_id)
            if prev is None or end_time > prev:
                last_session_by_user[user_id] = end_time

    # Drop users with zero hands — nothing to rank.
    users = [u for u, h in hands_by_user.items() if h > 0]
    print(
        f"Scanned {scanned} cash sessions ({skipped_duel} duel sessions skipped); "
        f"{len(users)} users with cash hands."
    )

    if dry_run:
        for u in sorted(users, key=lambda u: hands_by_user[u], reverse=True)[:10]:
            print(
                f"  {u[:24]:24}  hands={hands_by_user[u]:>6}  "
                f"profit_cents={profit_by_user[u]}"
            )
        print("Dry run — nothing written.")
        return

    written = 0
    batch = db.batch()
    batch_count = 0
    for user_id in users:
        ref = db.collection("user_cash_stats").document(user_id)
        # Fall back to server time only if no session had a parseable end_time
        # (rare); a missing updated_at would otherwise read as "never active".
        last_played = last_session_by_user.get(user_id, firestore.SERVER_TIMESTAMP)
        batch.set(
            ref,
            {
                "user_id": user_id,
                "cash_hands_played": hands_by_user[user_id],
                "cash_profit_cents": profit_by_user[user_id],
                "updated_at": last_played,
            },
            merge=True,
        )
        batch_count += 1
        written += 1
        # Firestore caps batches at 500 writes.
        if batch_count >= 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
            print(f"  Committed {written} docs...")

    if batch_count:
        batch.commit()

    print(f"\nDone! Wrote {written} user_cash_stats docs.")


if __name__ == "__main__":
    backfill(dry_run="--dry-run" in sys.argv)
