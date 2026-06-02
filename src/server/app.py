"""
FastAPI application with WebSocket endpoint.

Entry point for the poker WebSocket server.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException

from ..manager import TableManager
from ..persistence import FirestoreClient, HandLogger
from ..session import SessionTracker, process_session
from .connection import ConnectionManager
from .handler import MessageHandler
from .auth import AuthService
from .timer import ActionTimerService
from .reconnect import ReconnectManager
from .logging_config import logger
from ..insights.generator import InsightGenerator
from ..insights.hand_converter import convert_hand_to_insight_request, convert_hand_to_full_insight_request
from ..persistence.models import DuelRecord
from .glicko import (
    calculate_new_rating,
    update_rd_for_inactivity,
    get_default_rating,
    INITIAL_RATING,
    INITIAL_RD,
)
from .bot_personas import get_persona_pool, BotPersonaPool


# Global instances (initialized in lifespan)
manager: Optional[TableManager] = None
connections: Optional[ConnectionManager] = None
handler: Optional[MessageHandler] = None
timer: Optional[ActionTimerService] = None
reconnect_mgr: Optional[ReconnectManager] = None
firestore: Optional[FirestoreClient] = None
hand_logger: Optional[HandLogger] = None
session_tracker: Optional[SessionTracker] = None
_insight_generator: Optional[InsightGenerator] = None

# Bot table management: table_id -> list of (bot_user_id, subprocess.Process)
_bot_processes: dict[str, list[tuple[str, asyncio.subprocess.Process]]] = {}
# Track which human user owns which bot table: user_id -> table_id
_bot_table_owners: dict[str, str] = {}


# =============================================================================
# DUEL MODE STATE
# =============================================================================

@dataclass
class DuelMatch:
    """Tracks a duel match from queue to completion.

    Entry fees are tracked per-player so cross-stake matchmaking can pair a
    waiting player at one tier with an arrival at a different tier. Each
    player is told they were matched at their own stake; the house absorbs
    the chip differential on payout (play-money chips, not real currency).
    """
    match_id: str
    table_id: Optional[str]
    stack_type: str  # "50bb" or "15bb"
    status: str  # "waiting", "in_progress", "completed"
    player1_id: str
    player1_display_name: str
    player1_entry_fee_cents: int
    player2_id: Optional[str] = None
    player2_display_name: Optional[str] = None
    player2_entry_fee_cents: Optional[int] = None
    player2_is_bot: bool = False
    winner_id: Optional[str] = None
    # widen_level: 0 = strict (same fee only), 1 = any fee within stack_type
    widen_level: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    queue_timeout_task: Optional[asyncio.Task] = None
    # Disconnect / forfeit tracking. When a player drops, we set
    # disconnected_player_id and start disconnect_grace_task. On reconnect,
    # the auth path cancels the task and clears the field. _complete_duel_match
    # cancels defensively on teardown.
    disconnected_player_id: Optional[str] = None
    disconnect_grace_task: Optional[asyncio.Task] = None


# Duel queue: queue_key (entry_fee_stack_type or challenge_<id>) -> waiting match
_duel_queues: dict[str, DuelMatch] = {}
# Active duels: table_id -> match
_active_duels: dict[str, DuelMatch] = {}
# User -> match_id tracking
_user_duels: dict[str, str] = {}


# Valid duel entry fees in cents ($100, $500, $1000, $5000)
VALID_DUEL_ENTRY_FEES = {10000, 50000, 100000, 500000}
# Stack types: 50bb (500 chips) or 15bb (150 chips) - always at 5¢/10¢
VALID_DUEL_STACK_TYPES = {"50bb", "15bb"}
# Strict-tier wait before widening to any tier (seconds)
DUEL_STRICT_WAIT_SECONDS = 10.0
# Widened wait before bot fills in (seconds)
DUEL_WIDENED_WAIT_SECONDS = 10.0
# Grace period for duel disconnections (shorter than cash games)
DUEL_DISCONNECT_GRACE_SECONDS = 30.0

# Heartbeat reaper: close sockets with no inbound traffic for this long.
# A suspended/killed iOS app does not send FIN, so the socket can look alive
# at the TCP layer forever. Reaping triggers WebSocketDisconnect → the
# existing duel-grace / reconnect-grace cleanup paths.
# Threshold sized at ~2× the iOS client PING interval (currently 30s) so
# normal Timer jitter / brief network blips don't false-positive. Any inbound
# message (ACTION, PING, NEXT_HAND, ANIMATION_COMPLETE, …) resets the timer.
# If the iOS PING interval changes, keep the 2× rule.
HEARTBEAT_IDLE_SECONDS = 65.0
HEARTBEAT_REAPER_INTERVAL_SECONDS = 5.0


def _kill_orphan_bot_processes() -> int:
    """Kill any orphan openbot_client processes from previous server runs.

    Returns the number of processes killed.
    """
    try:
        # Find all openbot_client processes
        result = subprocess.run(
            ["pgrep", "-f", "openbot_client"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # No processes found
            return 0

        pids = result.stdout.strip().split("\n")
        killed = 0
        for pid in pids:
            if pid:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed += 1
                except (ProcessLookupError, ValueError):
                    pass

        if killed > 0:
            print(f"[STARTUP] Killed {killed} orphan bot process(es)", flush=True)
        return killed
    except Exception as e:
        print(f"[STARTUP] Error killing orphan bots: {e}", flush=True)
        return 0


# Reconciliation sweeper interval. Runs every N seconds to scrub stuck entries
# from _user_duels / _active_duels / _bot_table_owners that have no live
# counterpart. Defense-in-depth on top of the in-line cleanup paths — any leak
# vector we miss (panic, partial async cancel, refactor regression) gets healed
# within one sweep instead of permanently blocking the affected user.
DUEL_SWEEPER_INTERVAL_SECONDS = 30.0


async def _reconcile_duel_state() -> None:
    """Single sweep pass: scrub state-map entries with no live counterpart.

    Reconciliation rules (no time-based heuristics — only scrub when the entry
    is provably orphaned):

      _user_duels[user_id] = match_id
        scrub if match_id is not in any _duel_queues value AND not in any
        _active_duels value.

      _active_duels[table_id] = match
        scrub if manager._tables doesn't have table_id (the table has been
        torn down — the duel cannot progress). Also scrubs both players from
        _user_duels, releases bot persona, terminates stale bot processes.

      _bot_table_owners[user_id] = table_id
        scrub if manager._tables doesn't have table_id.

      _bot_processes[table_id]
        terminate + pop if table_id is not in manager._tables.
    """
    if manager is None:
        return

    live_match_ids: set[str] = set()
    for m in _duel_queues.values():
        live_match_ids.add(m.match_id)
    for m in _active_duels.values():
        live_match_ids.add(m.match_id)

    # _user_duels: scrub entries whose match_id has no live counterpart.
    for user_id, match_id in list(_user_duels.items()):
        if match_id not in live_match_ids:
            _user_duels.pop(user_id, None)
            print(
                f"[DUEL][SWEEP] Scrubbed orphan _user_duels[{user_id}]={match_id} "
                f"(no live queue/active match)",
                flush=True,
            )

    # _active_duels: scrub entries whose table no longer exists.
    live_tables: set[str] = set(getattr(manager, "_tables", {}).keys())
    for table_id, match in list(_active_duels.items()):
        if table_id in live_tables:
            continue

        _active_duels.pop(table_id, None)
        if match.player1_id:
            _user_duels.pop(match.player1_id, None)
        if match.player2_id:
            _user_duels.pop(match.player2_id, None)

        # Release bot persona if the orphaned duel was bot-vs-human.
        if match.player2_is_bot and match.player2_id:
            try:
                persona_pool = get_persona_pool()
                persona_pool.release_persona(match.player2_id)
            except Exception as e:
                print(f"[DUEL][SWEEP] Error releasing persona: {e}", flush=True)

        print(
            f"[DUEL][SWEEP] Scrubbed orphan _active_duels[{table_id}] "
            f"(match_id={match.match_id}, table no longer in manager)",
            flush=True,
        )

    # _bot_table_owners: scrub entries whose table no longer exists.
    for user_id, table_id in list(_bot_table_owners.items()):
        if table_id not in live_tables:
            _bot_table_owners.pop(user_id, None)
            print(
                f"[BOT][SWEEP] Scrubbed orphan _bot_table_owners[{user_id}]={table_id} "
                f"(table no longer in manager)",
                flush=True,
            )

    # _bot_processes: terminate + pop entries for tables that no longer exist.
    for table_id in list(_bot_processes.keys()):
        if table_id in live_tables:
            continue
        for bot_user_id, proc in _bot_processes.pop(table_id, []):
            try:
                proc.terminate()
            except Exception:
                pass
        print(
            f"[BOT][SWEEP] Terminated orphan bot processes for table {table_id}",
            flush=True,
        )


async def _heartbeat_reaper_loop() -> None:
    """Close WebSockets idle longer than HEARTBEAT_IDLE_SECONDS.

    The TCP socket from a suspended iOS app can stay 'open' on the server side
    indefinitely. We close it ourselves so the receive loop raises
    WebSocketDisconnect and the existing duel-forfeit / reconnect-grace logic
    runs.
    """
    print(
        f"[HEARTBEAT] Reaper started "
        f"(idle_threshold={HEARTBEAT_IDLE_SECONDS}s, "
        f"interval={HEARTBEAT_REAPER_INTERVAL_SECONDS}s)",
        flush=True,
    )
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_REAPER_INTERVAL_SECONDS)
            try:
                stale = connections.get_stale_users(HEARTBEAT_IDLE_SECONDS)
                for user_id, ws in stale:
                    print(
                        f"[HEARTBEAT] Reaping idle socket user={user_id}",
                        flush=True,
                    )
                    try:
                        await ws.close(code=4002)
                    except Exception:
                        # close() failing is fine — receive_json on the loop
                        # side will still raise and run the disconnect path.
                        pass
            except Exception as e:
                print(f"[HEARTBEAT] Reaper iteration failed: {e}", flush=True)
    except asyncio.CancelledError:
        print("[HEARTBEAT] Reaper stopped", flush=True)
        raise


async def _duel_state_sweeper_loop() -> None:
    """Periodic reconciliation loop. Runs until cancelled."""
    print(
        f"[DUEL][SWEEP] Reconciliation sweeper started "
        f"(interval={DUEL_SWEEPER_INTERVAL_SECONDS}s)",
        flush=True,
    )
    try:
        while True:
            await asyncio.sleep(DUEL_SWEEPER_INTERVAL_SECONDS)
            try:
                await _reconcile_duel_state()
            except Exception as e:
                print(f"[DUEL][SWEEP] Sweeper iteration failed: {e}", flush=True)
    except asyncio.CancelledError:
        print("[DUEL][SWEEP] Reconciliation sweeper stopped", flush=True)
        raise


# Hand-logger retry drain interval. Without this, the in-memory retry queue
# in HandLogger grew unbounded under Firestore flakes since
# retry_failed_writes() was never called from anywhere.
HAND_LOG_RETRY_INTERVAL_SECONDS = 60.0


async def _hand_log_retry_loop() -> None:
    """Drain the hand-logger retry queue periodically.

    HandLogger.log_hand() schedules its Firestore write as an asyncio.Task and
    appends failed writes to _retry_queue. Previously no code called
    retry_failed_writes(), so any Firestore flake stranded the hand log forever
    and the queue grew with every fresh failure. This loop runs the drain
    so a flake heals on its own once Firestore recovers.
    """
    print(
        f"[HAND_LOG][RETRY] Drain loop started "
        f"(interval={HAND_LOG_RETRY_INTERVAL_SECONDS}s)",
        flush=True,
    )
    try:
        while True:
            await asyncio.sleep(HAND_LOG_RETRY_INTERVAL_SECONDS)
            try:
                if hand_logger is None:
                    continue
                qsize = hand_logger.retry_queue_size
                if qsize == 0:
                    continue
                succeeded = await hand_logger.retry_failed_writes()
                print(
                    f"[HAND_LOG][RETRY] Drained {succeeded}/{qsize} pending writes "
                    f"(remaining={hand_logger.retry_queue_size})",
                    flush=True,
                )
            except Exception as e:
                print(f"[HAND_LOG][RETRY] Iteration failed: {e}", flush=True)
    except asyncio.CancelledError:
        print("[HAND_LOG][RETRY] Drain loop stopped", flush=True)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - initialize and cleanup resources."""
    global manager, connections, handler, timer, reconnect_mgr, firestore, hand_logger, session_tracker

    # Kill any orphan bot processes from previous server runs
    _kill_orphan_bot_processes()

    # Initialize persistence layer
    firestore = FirestoreClient()
    hand_logger = HandLogger(firestore)

    # Initialize bot persona pool and seed personas
    persona_pool = get_persona_pool(firestore)
    await persona_pool.ensure_personas_exist()

    # Initialize session tracking with analysis callback
    async def on_session_end(session):
        await process_session(session, firestore)

    session_tracker = SessionTracker(on_session_end=on_session_end)

    # Pass session_tracker to hand_logger for hand tracking
    hand_logger.set_session_tracker(session_tracker)

    manager = TableManager(hand_logger, firestore)
    connections = ConnectionManager()
    auth = AuthService()
    timer = ActionTimerService()
    reconnect_mgr = ReconnectManager(grace_period_seconds=60.0)
    handler = MessageHandler(manager, connections, auth, timer, session_tracker)

    # Set timeout callback
    async def on_timeout(pending) -> None:
        await handler.handle_timeout(pending)

    timer.set_timeout_callback(on_timeout)
    timer.start()

    # Set reconnect grace period expiry callback
    async def on_grace_expired(user_id: str, table_id: str) -> None:
        """Called when a player's grace period expires - actually remove them."""
        try:
            # Only remove if they're still disconnected (not reconnected)
            if not connections.is_connected(user_id):
                # Check if this is a bot table owner
                if user_id in _bot_table_owners:
                    logger.info("Grace period expired for bot table owner", user_id=user_id)
                    await _cleanup_bot_table(user_id)
                else:
                    # Regular table - remove player and return chips
                    chips = await manager.remove_player(user_id)
                    if chips.amount > 0 and firestore:
                        await firestore.add_balance(user_id, chips.amount)
                        logger.info(f"Returned {chips.amount} cents after grace period", user_id=user_id)
                connections.disconnect(user_id)
                logger.info("Player removed after grace period expired", user_id=user_id, table_id=table_id)
        except Exception as e:
            logger.warning(f"Error removing player after grace period: {e}", user_id=user_id)

    reconnect_mgr.set_expiry_callback(on_grace_expired)

    sweeper_task = asyncio.create_task(_duel_state_sweeper_loop())
    heartbeat_task = asyncio.create_task(_heartbeat_reaper_loop())
    hand_log_retry_task = asyncio.create_task(_hand_log_retry_loop())

    yield

    sweeper_task.cancel()
    heartbeat_task.cancel()
    hand_log_retry_task.cancel()
    for t in (sweeper_task, heartbeat_task, hand_log_retry_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    await timer.stop()
    await manager.shutdown()


app = FastAPI(title="Poker Server", lifespan=lifespan)


@app.get("/health")
async def health():
    """Liveness probe - basic health check."""
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """
    Readiness probe - checks if service can accept traffic.

    Cloud Run uses this to determine if instance should receive requests.
    """
    checks = {
        "manager": manager is not None,
        "connections": connections is not None,
        "timer": timer is not None and timer._running,
    }

    all_healthy = all(checks.values())

    if all_healthy:
        return {
            "status": "ready",
            "checks": checks,
            "active_tables": len(manager._tables) if manager else 0,
            "active_connections": len(connections._connections) if connections else 0,
        }
    else:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks}
        )


@app.post("/debug/start_hand/{table_id}")
async def debug_start_hand(table_id: str):
    """
    Debug endpoint to start a hand at a table.

    Useful for testing - normally hands would start automatically
    when enough players are seated.
    """
    try:
        error = await handler.handle_start_hand(table_id)
        if error:
            raise HTTPException(status_code=400, detail=error.get("message", "Error"))
        return {"status": "hand_started"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/debug/tables")
async def debug_list_tables():
    """Debug endpoint to list active tables."""
    tables = []
    for table_id, runner in manager._tables.items():
        tables.append({
            "table_id": table_id,
            "player_count": runner.player_count,
            "has_open_seats": runner.has_open_seats(),
        })
    return {"tables": tables}


@app.post("/debug/force_timeout/{user_id}")
async def debug_force_timeout(user_id: str):
    """
    Debug endpoint to force a timeout for a user.

    Useful for testing - directly triggers the timeout handler
    without waiting for the timer tick.
    """
    pending = timer.get_pending(user_id)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending action for user")

    await handler.handle_timeout(pending)
    timer.clear_deadline(user_id)
    return {"status": "timeout_forced", "user_id": user_id}


@app.get("/debug/hand_logs")
async def debug_list_hand_logs():
    """Debug endpoint to list all hand logs."""
    return {"hand_logs": firestore.get_all_hand_logs()}


@app.get("/debug/hand_logs/{hand_id}")
async def debug_get_hand_log(hand_id: str):
    """Debug endpoint to get a specific hand log."""
    hand_log = firestore.get_hand_log(hand_id)
    if not hand_log:
        raise HTTPException(status_code=404, detail="Hand log not found")
    return hand_log


@app.get("/debug/grade_hand/{hand_id}")
async def debug_grade_hand(hand_id: str, user_id: str):
    """Debug endpoint to inspect preflop grading for a specific hand.

    Args:
        hand_id: The hand ID to grade
        user_id: The user ID to grade decisions for
    """
    from ..session.grading import PreflopGrader

    if not firestore:
        raise HTTPException(status_code=500, detail="Firestore not initialized")

    hand_log = firestore.get_hand_log(hand_id)
    if not hand_log:
        raise HTTPException(status_code=404, detail="Hand log not found")

    grader = PreflopGrader(firestore)
    grades = grader.grade_hand(hand_log, user_id)

    return {
        "hand_id": hand_id,
        "user_id": user_id,
        "grades": [
            {
                "street": g.street,
                "action_taken": g.action_taken,
                "hand": g.hand,
                "position": g.position,
                "spot_path": g.spot_path,
                "gto_frequency": g.gto_frequency,
                "confidence": g.confidence,
                "grade": g.grade.value,
                "reasoning": g.reasoning,
            }
            for g in grades
        ],
    }


@app.get("/debug/ledger")
async def debug_list_ledger():
    """Debug endpoint to list all ledger entries."""
    return {"ledger_entries": firestore.get_all_ledger_entries()}


@app.get("/debug/ledger/{user_id}")
async def debug_get_user_ledger(user_id: str):
    """Debug endpoint to get ledger entries for a user."""
    return {"ledger_entries": firestore.get_ledger_entries(user_id)}


@app.get("/debug/sessions")
async def debug_get_sessions():
    """Debug endpoint to get all active sessions."""
    if not session_tracker:
        return {"error": "Session tracker not initialized"}

    sessions = []
    for user_id, session in session_tracker._sessions.items():
        sessions.append({
            "session_id": session.session_id,
            "user_id": session.user_id,
            "table_id": session.table_id,
            "stake_id": session.stake_id,
            "hands_played": len(session.hand_ids),
            "hand_ids": session.hand_ids,
            "buy_in_cents": session.buy_in_cents,
            "started_at": session.started_at.isoformat(),
        })

    return {"active_sessions": sessions, "count": len(sessions)}


@app.get("/debug/sessions/{user_id}")
async def debug_get_user_session(user_id: str):
    """Debug endpoint to get active session for a user."""
    if not session_tracker:
        return {"error": "Session tracker not initialized"}

    session = session_tracker.get_active_session(user_id)
    if not session:
        return {"error": f"No active session for user {user_id}"}

    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "table_id": session.table_id,
        "stake_id": session.stake_id,
        "hands_played": len(session.hand_ids),
        "hand_ids": session.hand_ids,
        "buy_in_cents": session.buy_in_cents,
        "total_rebuys_cents": session.total_rebuys_cents,
        "started_at": session.started_at.isoformat(),
    }


@app.get("/debug/stored_sessions/{user_id}")
async def debug_get_stored_sessions(user_id: str, limit: int = 10):
    """Debug endpoint to get stored (completed) sessions for a user from Firestore."""
    if not firestore:
        return {"error": "Firestore not initialized"}

    sessions = await firestore.get_user_sessions(user_id, limit)
    return {"sessions": sessions, "count": len(sessions)}


@app.get("/debug/session_insights")
async def debug_session_insights(min_hands: int = 15, session_id: str = None):
    """
    Debug endpoint: pick a random session, run key hands + AI insights.
    DEPRECATED: Use /debug/session_info + /debug/hand_insight for progressive loading.
    """
    from ..insights.key_hands import select_key_hands
    from ..insights.hand_converter import convert_hand_to_full_insight_request
    import random

    if not firestore or not firestore._db:
        return {"error": "Firestore not initialized"}

    db = firestore._db

    # Find a session
    if session_id:
        doc = db.collection("sessions").document(session_id).get()
        if not doc.exists:
            return {"error": f"Session {session_id} not found"}
        session = doc.to_dict()
    else:
        # Query sessions with hand_ids
        query = db.collection("sessions").where("session_id", ">=", "sess_").where("session_id", "<", "sess_~").limit(100)
        docs = list(query.stream())

        # Filter by min_hands
        valid = [d.to_dict() for d in docs if len(d.to_dict().get("hand_ids", [])) >= min_hands]
        if not valid:
            return {"error": f"No sessions found with >= {min_hands} hands"}

        session = random.choice(valid)

    session_id = session.get("session_id")
    user_id = session.get("user_id")
    hand_ids = session.get("hand_ids", [])
    display_name = session.get("display_name", "?")

    # Fetch hands
    hands = []
    for hid in hand_ids:
        doc = db.collection("hands").document(hid).get()
        if doc.exists:
            hands.append(doc.to_dict())

    # Run key hands algorithm
    key_hands = select_key_hands(hands, user_id)

    # Generate insights in parallel
    import asyncio
    generator = get_insight_generator()

    # Prepare tasks
    async def generate_single_insight(kh):
        hand_data = next((h for h in hands if h.get("hand_id") == kh.hand_id), None)
        if not hand_data:
            return None

        try:
            request = convert_hand_to_full_insight_request(hand_data, user_id)
            if request:
                # Run sync API call in thread pool
                response = await asyncio.to_thread(generator.generate_hand_insight, request)
                insight_text = response.insight if response else None
            else:
                insight_text = None
        except Exception as e:
            insight_text = f"Error: {e}"

        return {
            "hand_id": kh.hand_id,
            "score": kh.score,
            "hero_position": kh.hero_position,
            "hero_hand": kh.hero_hand,
            "board": kh.board,
            "profit_bb": kh.profit_bb,
            "pot_type": kh.pot_type,
            "max_street": kh.max_street,
            "insight": insight_text,
        }

    # Run all insight generations in parallel
    tasks = [generate_single_insight(kh) for kh in key_hands]
    results = await asyncio.gather(*tasks)
    insights = [r for r in results if r is not None]

    return {
        "session_id": session_id,
        "display_name": display_name,
        "hands_played": len(hands),
        "key_hands_count": len(key_hands),
        "key_hand_insights": insights,
    }


@app.get("/debug/session_info")
async def debug_session_info(min_hands: int = 15, session_id: str = None):
    """
    Fast endpoint: Get session stats, preflop analysis, and key hands metadata.
    Does NOT generate AI insights - use /debug/hand_insight/{hand_id} for that.
    """
    from ..insights.key_hands import select_key_hands
    from ..insights.hand_converter import convert_hand_to_full_insight_request
    from ..session.grading import PreflopGrader
    import random

    if not firestore or not firestore._db:
        return {"error": "Firestore not initialized"}

    db = firestore._db

    # Find a session
    if session_id:
        doc = db.collection("sessions").document(session_id).get()
        if not doc.exists:
            return {"error": f"Session {session_id} not found"}
        session = doc.to_dict()
    else:
        query = db.collection("sessions").where("session_id", ">=", "sess_").where("session_id", "<", "sess_~").limit(100)
        docs = list(query.stream())
        valid = [d.to_dict() for d in docs if len(d.to_dict().get("hand_ids", [])) >= min_hands]
        if not valid:
            return {"error": f"No sessions found with >= {min_hands} hands"}
        session = random.choice(valid)

    session_id = session.get("session_id")
    user_id = session.get("user_id")
    hand_ids = session.get("hand_ids", [])
    display_name = session.get("display_name", "?")

    # Fetch hands
    hands = []
    for hid in hand_ids:
        doc = db.collection("hands").document(hid).get()
        if doc.exists:
            hands.append(doc.to_dict())

    # Calculate session stats
    analysis = session.get("analysis", {})
    vpip = analysis.get("vpip", 0) * 100 if analysis else 0
    pfr = analysis.get("pfr", 0) * 100 if analysis else 0

    # Calculate profit
    profit_cents = session.get("profit_cents", 0)
    big_blind = 200  # Default
    if hands:
        big_blind = hands[0].get("big_blind") or 200

    # Run preflop grading
    all_mistakes = []
    try:
        grader = PreflopGrader(firestore)
        for hand in hands:
            grades = grader.grade_hand(hand, user_id)
            for g in grades:
                if g.grade.value == "mistake":
                    all_mistakes.append({
                        "hand_id": g.hand_id,
                        "hand": g.hand,
                        "position": g.position,
                        "action": g.action_taken,
                        "reasoning": g.reasoning,
                        "spot_path": g.spot_path,  # e.g. "RFI" or "BTN_RFI/SB_3B"
                        "confidence": g.confidence,
                    })
    except Exception as e:
        print(f"[DEBUG] Preflop grading error: {e}")

    # Select diverse mix of 5 high-confidence mistakes
    # Separate into RFI and non-RFI categories
    rfi_mistakes = [m for m in all_mistakes if m["spot_path"] == "RFI"]
    non_rfi_mistakes = [m for m in all_mistakes if m["spot_path"] != "RFI"]

    # Sort each by confidence (highest first)
    rfi_mistakes.sort(key=lambda x: x["confidence"], reverse=True)
    non_rfi_mistakes.sort(key=lambda x: x["confidence"], reverse=True)

    # Build diverse selection: prioritize non-RFI (rarer/more interesting)
    # Take up to 2 non-RFI, then fill with RFI, max 5 total
    preflop_mistakes = []

    # First, take up to 2 high-confidence non-RFI mistakes
    for m in non_rfi_mistakes[:2]:
        if m["confidence"] >= 0.5:  # Only if reasonably confident
            preflop_mistakes.append(m)

    # Then fill remaining slots with RFI mistakes
    remaining_slots = 5 - len(preflop_mistakes)
    preflop_mistakes.extend(rfi_mistakes[:remaining_slots])

    # If we still have room and more non-RFI, add them
    if len(preflop_mistakes) < 5 and len(non_rfi_mistakes) > 2:
        for m in non_rfi_mistakes[2:]:
            if len(preflop_mistakes) >= 5:
                break
            if m["confidence"] >= 0.5:
                preflop_mistakes.append(m)

    # Remove confidence from output (internal use only)
    for m in preflop_mistakes:
        m.pop("confidence", None)

    # Run key hands algorithm
    key_hands = select_key_hands(hands, user_id)

    # Build key hand details with full action
    key_hand_details = []
    for kh in key_hands:
        hand_data = next((h for h in hands if h.get("hand_id") == kh.hand_id), None)
        if not hand_data:
            continue

        # Get street actions
        try:
            request = convert_hand_to_full_insight_request(hand_data, user_id)
            street_actions = []
            if request and request.street_actions:
                for sa in request.street_actions:
                    street_actions.append({
                        "street": sa.street,
                        "cards": sa.cards,
                        "actions": sa.actions,
                    })
            hero_decisions = []
            if request and request.hero_decisions:
                for hd in request.hero_decisions:
                    hero_decisions.append({
                        "street": hd.street,
                        "action": hd.action_taken,
                        "facing": hd.facing,
                        "pot_bb": round(hd.pot_before_bb, 1),
                    })
        except Exception as e:
            street_actions = []
            hero_decisions = []
            print(f"[DEBUG] Error converting hand {kh.hand_id}: {e}")

        key_hand_details.append({
            "hand_id": kh.hand_id,
            "score": kh.score,
            "hero_position": kh.hero_position,
            "hero_hand": kh.hero_hand,
            "board": kh.board,
            "profit_bb": kh.profit_bb,
            "pot_type": kh.pot_type,
            "max_street": kh.max_street,
            "street_actions": street_actions,
            "hero_decisions": hero_decisions,
        })

    return {
        "session_id": session_id,
        "user_id": user_id,
        "display_name": display_name,
        "hands_played": len(hands),
        "profit_cents": profit_cents,
        "profit_bb": round(profit_cents / big_blind, 1) if big_blind else 0,
        "big_blind": big_blind,
        "stats": {
            "vpip": round(vpip, 1),
            "pfr": round(pfr, 1),
        },
        "preflop_mistakes": preflop_mistakes,  # All mistakes
        "key_hands": key_hand_details,
    }


@app.get("/debug/hand_insight/{hand_id}")
async def debug_hand_insight(hand_id: str, user_id: str):
    """
    Generate AI insight for a single hand.
    Call this after /debug/session_info for progressive loading.
    """
    from ..insights.hand_converter import convert_hand_to_full_insight_request
    import asyncio

    if not firestore or not firestore._db:
        return {"error": "Firestore not initialized"}

    db = firestore._db

    # Fetch the hand
    doc = db.collection("hands").document(hand_id).get()
    if not doc.exists:
        return {"hand_id": hand_id, "insight": None, "terms": {}, "error": f"Hand {hand_id} not found"}

    hand_data = doc.to_dict()

    # Generate insight
    try:
        request = convert_hand_to_full_insight_request(hand_data, user_id)
        if not request:
            return {"hand_id": hand_id, "insight": None, "terms": {}, "error": "Could not convert hand"}

        generator = get_insight_generator()
        response = await asyncio.to_thread(generator.generate_hand_insight, request)
        insight_text = response.insight if response else None
        terms = response.terms if response else {}
    except Exception as e:
        return {"hand_id": hand_id, "insight": None, "terms": {}, "error": str(e)}

    return {
        "hand_id": hand_id,
        "insight": insight_text,
        "terms": terms,
    }


@app.get("/debug/range_check")
async def debug_range_check(spot: str = "BTN_RFI", position: str = "BB", hand: str = "AhAs"):
    """
    Debug endpoint to check what GTO range data exists for a spot.

    Examples:
        /debug/range_check?spot=BTN_RFI&position=BB&hand=AhAs
        /debug/range_check?spot=BTN_RFI/SB_3B&position=BTN&hand=KhKs
    """
    from ..session.grading.range_lookup import RangeLookup, normalize_hand

    if not firestore:
        return {"error": "Firestore not initialized"}

    rl = RangeLookup(firestore)

    # Parse spot path
    spot_path = spot.split("/") if "/" in spot else [spot]
    position_8max = rl.map_position_6max_to_8max(position)
    normalized_hand = normalize_hand(hand)

    result = {
        "input": {
            "spot": spot,
            "spot_path": spot_path,
            "position": position,
            "position_8max": position_8max,
            "hand": hand,
            "normalized_hand": normalized_hand,
        },
        "nodes_checked": [],
        "frequencies": {},
    }

    # Check if spot exists
    spot_node = rl.get_node_at_spot(spot_path)
    result["nodes_checked"].append({
        "path": "/".join(spot_path),
        "exists": spot_node is not None,
        "size": spot_node.get("size") if spot_node else None,
    })

    if not spot_node:
        result["error"] = f"Spot {spot_path} not found in Firestore"
        return result

    # Check call range
    call_path = spot_path + [f"{position_8max}_C"]
    call_node = rl.get_node_at_spot(call_path)
    call_freq = 0
    if call_node and "range" in call_node:
        call_freq = call_node["range"].get(normalized_hand, 0)
    result["nodes_checked"].append({
        "path": "/".join(call_path),
        "exists": call_node is not None,
        "has_range": "range" in call_node if call_node else False,
        "hand_freq": call_freq,
    })
    if call_freq > 0:
        result["frequencies"]["Call"] = call_freq

    # Check 3-bet range
    threeb_path = spot_path + [f"{position_8max}_3B"]
    threeb_node = rl.get_node_at_spot(threeb_path)
    threeb_freq = 0
    if threeb_node and "range" in threeb_node:
        threeb_freq = threeb_node["range"].get(normalized_hand, 0)
    result["nodes_checked"].append({
        "path": "/".join(threeb_path),
        "exists": threeb_node is not None,
        "has_range": "range" in threeb_node if threeb_node else False,
        "hand_freq": threeb_freq,
    })
    if threeb_freq > 0:
        result["frequencies"]["Raise"] = threeb_freq

    # Check 4-bet range (if applicable)
    fourb_path = spot_path + [f"{position_8max}_4B"]
    fourb_node = rl.get_node_at_spot(fourb_path)
    fourb_freq = 0
    if fourb_node and "range" in fourb_node:
        fourb_freq = fourb_node["range"].get(normalized_hand, 0)
    result["nodes_checked"].append({
        "path": "/".join(fourb_path),
        "exists": fourb_node is not None,
        "has_range": "range" in fourb_node if fourb_node else False,
        "hand_freq": fourb_freq,
    })
    if fourb_freq > 0:
        result["frequencies"]["Raise"] = result["frequencies"].get("Raise", 0) + fourb_freq

    # Calculate fold freq
    total_play = sum(result["frequencies"].values())
    if total_play < 1.0:
        result["frequencies"]["Fold"] = round(1.0 - total_play, 4)

    return result


@app.post("/debug/add_bots/{table_id}")
async def debug_add_bots(table_id: str, count: int = 1):
    """
    Debug endpoint to add bot players to a table.

    Bots are added but don't auto-play - use /debug/start_hand to begin.
    Broadcasts seat updates to all connected clients at the table.
    """
    from ..models import PlayerIdentity, Chips, Seat, SeatStatus
    from .config import config
    import random
    import string

    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    runner = manager._tables[table_id]
    stake_id = runner._config.stake_id
    bots_added = []

    for i in range(count):
        bot_id = f"bot_{''.join(random.choices(string.ascii_lowercase, k=6))}"
        bot_name = f"Bot{random.randint(1, 999)}"

        try:
            player = PlayerIdentity(
                user_id=bot_id,
                display_name=bot_name,
                avatar_url=None,
            )
            buy_in = Chips(amount=config.default_max_buy_in_cents // 2)

            # Use manager.add_player which handles table assignment
            result_table_id, seat = await manager.add_player(
                bot_id, stake_id, buy_in, player
            )

            bots_added.append({
                "bot_id": bot_id,
                "display_name": bot_name,
                "seat": seat,
                "chips": buy_in.amount,
            })

            # Broadcast SEAT_UPDATE to all connected clients at this table
            seat_update = {
                "type": "SEAT_UPDATE",
                "seat": {
                    "seat_index": seat,
                    "status": "seated",
                    "player": {
                        "user_id": bot_id,
                        "display_name": bot_name,
                        "avatar_url": None,
                    },
                    "chips": {"amount": buy_in.amount},
                    "bet": {"amount": 0},
                    "is_button": False,
                    "is_connected": True,
                },
            }
            await connections.broadcast_to_table(table_id, seat_update)

        except ValueError as e:
            # Table might be full
            break

    return {
        "table_id": table_id,
        "bots_added": bots_added,
        "player_count": runner.player_count,
    }


@app.post("/debug/kill_bots/{table_id}")
async def debug_kill_bots(table_id: str):
    """Debug endpoint to remove all bot players from a table."""
    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    # Find all bot user_ids at this table
    bot_ids = [uid for uid in manager._user_tables if uid.startswith("bot_") and manager._user_tables[uid] == table_id]

    removed = []
    for bot_id in bot_ids:
        try:
            await manager.remove_player(bot_id)
            removed.append(bot_id)
        except Exception:
            pass

    runner = manager._tables.get(table_id)
    return {
        "table_id": table_id,
        "bots_removed": removed,
        "player_count": runner.player_count if runner else 0,
    }


@app.post("/debug/reset_table/{table_id}")
async def debug_reset_table(table_id: str):
    """Debug endpoint to completely reset a table (end hand, remove all players)."""
    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    runner = manager._tables[table_id]

    # Get all players at this table
    player_ids = [uid for uid, tid in manager._user_tables.items() if tid == table_id]

    # Remove all players
    removed = []
    for player_id in player_ids:
        try:
            await manager.remove_player(player_id)
            removed.append(player_id)
        except Exception:
            pass

    # Delete the table
    if table_id in manager._tables:
        del manager._tables[table_id]

    return {
        "table_id": table_id,
        "players_removed": removed,
        "status": "table_deleted",
    }


def get_insight_generator() -> InsightGenerator:
    """Get or create the global insight generator (lazy initialization)."""
    global _insight_generator
    if _insight_generator is None:
        _insight_generator = InsightGenerator(use_vector_search=True)
    return _insight_generator


@app.post("/api/insight/{hand_id}")
async def generate_insight(hand_id: str, hero_user_id: str = None):
    """
    Generate a poker insight for a hand, identifying the key decision point.

    Args:
        hand_id: The Firestore document ID for the hand
        hero_user_id: The user ID of the hero (human player viewing the hand)

    Returns:
        JSON with insight text, matched terms, and hand_id
    """
    # Load hand from Firestore using the client's method
    hand_data = firestore.get_hand_log(hand_id)
    if not hand_data:
        raise HTTPException(status_code=404, detail="Hand not found")

    # If no hero_user_id provided, try to find the human player
    if not hero_user_id:
        # Look for a non-bot player in the hand
        seats = hand_data.get("seats", [])
        for seat in seats:
            user_id = seat.get("user_id", "")
            if not user_id.startswith(("bot_", "user_bot_")):
                hero_user_id = user_id
                break

    if not hero_user_id:
        raise HTTPException(status_code=400, detail="Could not determine hero - please provide hero_user_id")

    # Convert to HandInsightRequest
    try:
        request = convert_hand_to_full_insight_request(hand_data, hero_user_id)
        if request is None:
            raise HTTPException(
                status_code=400,
                detail=f"Could not build insight request for hero {hero_user_id}"
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot convert hand: {e}")

    # Generate insight
    generator = get_insight_generator()
    response = generator.generate_hand_insight(request)

    return {
        "insight": response.insight,
        "terms": response.terms,
        "hand_id": hand_id,
    }


# Cache for poker terms
_poker_terms_cache: Optional[dict] = None


def _load_poker_terms() -> dict:
    """Load poker terms from local_content.json."""
    global _poker_terms_cache
    if _poker_terms_cache is None:
        from pathlib import Path
        terms_path = Path(__file__).parent.parent / "insights" / "content_admin" / "local_content.json"
        import json
        with open(terms_path) as f:
            _poker_terms_cache = json.load(f)
    return _poker_terms_cache


@app.get("/api/terms/{term_id}")
async def get_term(term_id: str):
    """
    Get details for a poker term.

    Args:
        term_id: The term ID (e.g., "fold-equity", "position")

    Returns:
        JSON with term name, blurb, and body
    """
    terms = _load_poker_terms()
    if term_id not in terms:
        raise HTTPException(status_code=404, detail=f"Term not found: {term_id}")

    term = terms[term_id]
    return {
        "term_id": term_id,
        "name": term.get("name", term_id),
        "blurb": term.get("blurb", ""),
        "body": term.get("body", ""),
    }


@app.get("/api/terms")
async def list_terms():
    """
    List all available poker terms.

    Returns:
        JSON with list of term IDs and names
    """
    terms = _load_poker_terms()
    return {
        "terms": [
            {"term_id": tid, "name": t.get("name", tid)}
            for tid, t in terms.items()
        ]
    }


_OPENBOT_CWD = os.environ.get("OPENBOT_DIR", "/home/de2425/openbot")
_OPENBOT_PYTHON = os.environ.get(
    "OPENBOT_PYTHON",
    os.path.join(_OPENBOT_CWD, "venv", "bin", "python"),
)
_OPENBOT_POLICY = os.environ.get("OPENBOT_POLICY", "/home/de2425/policy_iter252M.db")
# Stake-tiered HU policies; effective-stack routing lives in translator._get_policy_for_game.
_OPENBOT_HU_POLICY_8BB = os.environ.get("OPENBOT_HU_POLICY_8BB", "/home/de2425/hu8bb_policy.db")
_OPENBOT_HU_POLICY_15BB = os.environ.get("OPENBOT_HU_POLICY_15BB", "/home/de2425/hu15bb_v2_policy.db")
_OPENBOT_HU_POLICY_50BB = os.environ.get("OPENBOT_HU_POLICY_50BB", "/home/de2425/hu50bb_policy.db")
_OPENBOT_HU_POLICY_100BB = os.environ.get("OPENBOT_HU_POLICY_100BB", "/home/de2425/hu100bb_policy.db")
_OPENBOT_ABSTRACTION_DIR = os.environ.get("OPENBOT_ABSTRACTION_DIR", "/home/de2425/openbot/models/checkpoints")
_USE_PREFLOP_DB = os.environ.get("USE_PREFLOP_DB", "false").lower() == "true"  # Toggle: use preflop_ranges.db or main policy
_SOLVER_BIN = os.environ.get("SOLVER_BIN", "/home/de2425/poker_solver/cpp/build/river_solver_optimized")


async def _spawn_bot(
    table_id: str,
    bot_index: int,
    stake_id: str,
    buy_in_cents: int,
) -> tuple[str, asyncio.subprocess.Process]:
    """Spawn an OpenBot policy client subprocess that connects via websocket.

    DEPRECATED: Use _spawn_bot_process() for multi-bot mode instead.
    This function is kept for backwards compatibility with single-bot spawning.
    """
    bot_user_id = f"user_bot_{table_id}_{bot_index}"
    display_name = f"Bot{bot_index + 1}"

    cmd = [
        _OPENBOT_PYTHON, "-m", "src.serving.openbot_client",
        "--server", "ws://localhost:8000/ws",
        "--table-id", table_id,
        "--user-id", bot_user_id,
        "--policy", _OPENBOT_POLICY,
        "--display-name", display_name,
        "--stake", stake_id,
        "--buy-in", str(buy_in_cents),
        "--solver-bin", _SOLVER_BIN,
    ]
    if _USE_PREFLOP_DB:
        cmd.extend(["--preflop-db", "preflop_ranges.db"])

    log_path = os.path.join(_OPENBOT_CWD, f"bot_{bot_index}.log")
    log_file = open(log_path, "w")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=log_file,
        stderr=asyncio.subprocess.STDOUT,
        cwd=_OPENBOT_CWD,
    )
    print(f"[BOT] Spawned OpenBot {bot_user_id} (pid={proc.pid}) for table {table_id}, log={log_path}", flush=True)
    return bot_user_id, proc


_AGGRESSION_BIAS = float(os.environ.get("BOT_AGGRESSION_BIAS", "1.5"))
_SERVER_PORT = os.environ.get("PORT", "8000")


async def _spawn_bot_process(
    table_id: str,
    bot_count: int,
    stake_id: str,
    buy_in_cents: int,
    persona_ids: list[str] | None = None,
    bot_ids: list[str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn ONE process with multiple bots for a table.

    This consolidates N bots into a single process, reducing memory from
    N * ~150MB to ~200MB total by sharing policy stores and abstraction LUTs.

    Args:
        table_id: Table ID for bots to join
        bot_count: Number of bots to run in this process
        stake_id: Stake identifier
        buy_in_cents: Buy-in amount per bot

    Returns:
        The subprocess.Process handle
    """
    cmd = [
        _OPENBOT_PYTHON, "-m", "src.serving.openbot_client",
        "--server", f"ws://localhost:{_SERVER_PORT}/ws",
        "--table-id", table_id,
        "--num-bots", str(bot_count),
        "--policy", _OPENBOT_POLICY,
        "--hu-policy-8bb", _OPENBOT_HU_POLICY_8BB,
        "--hu-policy-15bb", _OPENBOT_HU_POLICY_15BB,
        "--hu-policy-50bb", _OPENBOT_HU_POLICY_50BB,
        "--hu-policy-100bb", _OPENBOT_HU_POLICY_100BB,
        "--abstraction-dir", _OPENBOT_ABSTRACTION_DIR,
        "--stake", stake_id,
        "--buy-in", str(buy_in_cents),
        "--solver-bin", _SOLVER_BIN,
        "--aggression-bias", str(_AGGRESSION_BIAS),
    ]
    if _USE_PREFLOP_DB:
        cmd.extend(["--preflop-db", "preflop_ranges.db"])
    if persona_ids:
        cmd.extend(["--persona", ",".join(persona_ids)])
    if bot_ids:
        cmd.extend(["--display-names", ",".join(bot_ids)])

    log_path = os.path.join(_OPENBOT_CWD, f"bot_table_{table_id}.log")
    log_file = open(log_path, "w")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=log_file,
        stderr=asyncio.subprocess.STDOUT,
        cwd=_OPENBOT_CWD,
    )
    print(f"[BOT] Spawned {bot_count} bots in single process (pid={proc.pid}) for table {table_id}, personas={persona_ids}, log={log_path}", flush=True)
    return proc


async def _create_bot_table(
    user_id: str,
    stake_id: str,
    buy_in_cents: int,
    display_name: str,
    bot_count: int,
    auto_top_up: bool = True,
    blitz_mode: bool = False,
    persona_ids: list[str] | None = None,
    bot_ids: list[str] | None = None,
    client_session_id: str | None = None,
    is_pro: bool = False,
) -> dict:
    """Create a table, seat the human, then spawn bot subprocess clients."""
    from ..models import PlayerIdentity, Chips

    player = PlayerIdentity(
        user_id=user_id,
        display_name=display_name,
        avatar_url=None,
    )
    buy_in = Chips(amount=buy_in_cents)

    # Seat the human player via normal join flow
    table_id, seat = await manager.add_player(user_id, stake_id, buy_in, player)
    connections.join_table(user_id, table_id)

    # Start session tracking
    if session_tracker:
        session_tracker.start_session(
            user_id=user_id,
            table_id=table_id,
            stake_id=stake_id,
            seat=seat,
            buy_in_cents=buy_in_cents,
            display_name=display_name,
            client_session_id=client_session_id,
            is_pro=is_pro,
        )

    # Set auto top-up preference on the player's seat and blitz mode on runner
    runner = manager._tables.get(table_id)
    if runner:
        # Set blitz mode on the runner
        runner.set_blitz_mode(blitz_mode, human_seat=seat)
        print(f"[BOT_TABLE] Set blitz_mode={blitz_mode} for table {table_id}")

        if seat < len(runner._engine._seats):
            seat_state = runner._engine._seats[seat]
            if seat_state:
                seat_state.auto_topup_enabled = auto_top_up
                print(f"[BOT_TABLE] Set auto_topup_enabled={auto_top_up} for seat {seat}")

    # Track ownership
    _bot_table_owners[user_id] = table_id

    # Get snapshot for human
    snapshot = await manager.get_snapshot(user_id)

    # Log persona and bot_id selection
    if persona_ids:
        print(f"[BOT_TABLE] Using personas: {persona_ids}")
    else:
        print(f"[BOT_TABLE] No personas - using normal GTO bots")
    if bot_ids:
        print(f"[BOT_TABLE] Using bot_ids: {bot_ids}")
    else:
        print(f"[BOT_TABLE] No bot_ids - using default Bot1, Bot2, etc.")

    # Spawn single process with all bots (memory efficient: ~200MB vs 5 * 150MB)
    proc = await _spawn_bot_process(
        table_id=table_id,
        bot_count=bot_count,
        stake_id=stake_id,
        buy_in_cents=buy_in_cents,
        persona_ids=persona_ids,
        bot_ids=bot_ids,
    )
    # Store as list with single entry for compatibility with cleanup code
    _bot_processes[table_id] = [("bot_table_process", proc)]

    # Wait for bots to connect and seat themselves (policy bots load slower)
    for attempt in range(100):  # Up to 10 seconds
        runner = manager._tables.get(table_id)
        if runner and runner.player_count >= bot_count + 1:
            break
        await asyncio.sleep(0.1)

    print(f"[BOT] All {bot_count} bots seated at {table_id}", flush=True)

    # Re-fetch snapshot after bots are seated
    snapshot = await manager.get_snapshot(user_id)
    return snapshot.model_dump(mode="json")


async def _cleanup_bot_table(user_id: str) -> None:
    """Clean up bot table when the human owner disconnects/leaves."""
    table_id = _bot_table_owners.pop(user_id, None)
    if not table_id:
        return

    print(f"[BOT] Cleaning up bot table {table_id}", flush=True)

    # First, get the human's chips and return them to wallet BEFORE removing
    final_chips = 0
    try:
        runner = manager._tables.get(table_id)
        if runner and firestore:
            # Find human's seat and chips
            for seat_idx, seat_state in enumerate(runner._engine._seats):
                if seat_state and seat_state.player and seat_state.player.user_id == user_id:
                    final_chips = seat_state.chips
                    if final_chips > 0:
                        await firestore.add_balance(user_id, final_chips)
                        print(f"[BOT] Returned {final_chips} cents to {user_id}", flush=True)
                    break
    except Exception as e:
        print(f"[BOT] Error returning chips: {e}", flush=True)

    # End session tracking (triggers analysis)
    if session_tracker:
        session_tracker.end_session(user_id, final_chips)

    # Terminate bot processes using stored handles
    bot_procs = _bot_processes.pop(table_id, [])
    for bot_user_id, proc in bot_procs:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            print(f"[BOT] Terminated bot process pid={proc.pid}", flush=True)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Fallback: kill any bot processes for this table by table_id in command line
    # This catches orphans if process handles are stale or tracking failed
    try:
        result = subprocess.run(
            ["pkill", "-f", f"openbot_client.*--table-id {table_id}"],
            capture_output=True,
        )
        if result.returncode == 0:
            print(f"[BOT] Fallback pkill cleaned up processes for {table_id}", flush=True)
    except Exception as e:
        print(f"[BOT] Fallback pkill failed: {e}", flush=True)

    # Remove bot players from manager (don't need to return their chips)
    for bot_user_id, _ in bot_procs:
        try:
            await manager.remove_player(bot_user_id)
        except Exception:
            pass

    # Remove human player from manager tracking
    try:
        # Use a direct removal that skips the normal leave flow since we already handled chips
        if user_id in manager._user_tables:
            del manager._user_tables[user_id]
    except Exception:
        pass

    # Delete the table
    if table_id in manager._tables:
        runner = manager._tables[table_id]
        await runner.stop()
        del manager._tables[table_id]

    print(f"[BOT] Bot table {table_id} cleaned up", flush=True)


# =============================================================================
# DUEL MODE HANDLERS
# =============================================================================

def _get_duel_queue_key(entry_fee_cents: int, stack_type: str, challenge_id: Optional[str] = None) -> str:
    """Generate a queue key for a waiting duel match.

    Public matches are keyed per (fee, stack) so a strict-tier match is an
    O(1) dict lookup. Cross-tier matching is handled by _find_waiting_match,
    which scans for widened waiters on the same stack_type. Friend challenges
    get a private queue and never participate in cross-tier matching.
    """
    if challenge_id:
        return f"challenge_{challenge_id}"
    return f"{entry_fee_cents}_{stack_type}"


def _find_waiting_match(entry_fee_cents: int, stack_type: str, challenge_id: Optional[str]) -> Optional[tuple[str, "DuelMatch"]]:
    """Find a waiting match for an arriving player.

    Returns (queue_key, match) of the chosen waiter, or None.

    Matching rules:
      1. Friend challenge: only the matching challenge_id queue.
      2. Strict same-tier: same fee + stack_type.
      3. Cross-tier: any waiter on the same stack_type whose widen_level >= 1.
         The waiter has been queued long enough that we widened their search;
         the arriving player is told they were matched at their own stake.
    """
    if challenge_id:
        key = f"challenge_{challenge_id}"
        match = _duel_queues.get(key)
        return (key, match) if match else None

    # 1. Strict same-fee match (preferred — no cross-stake).
    strict_key = f"{entry_fee_cents}_{stack_type}"
    if strict_key in _duel_queues:
        return (strict_key, _duel_queues[strict_key])

    # 2. Widened match: any waiter on same stack_type that has widened.
    for key, match in _duel_queues.items():
        if key.startswith("challenge_"):
            continue
        if match.stack_type != stack_type:
            continue
        if match.widen_level >= 1:
            return (key, match)
    return None


def _get_duel_stake_id(stack_type: str) -> str:
    """Get the stake_id for a duel stack type."""
    return f"duel_{stack_type}"


def _get_duel_buy_in(stack_type: str) -> int:
    """Get the chip buy-in amount for a duel stack type (always 5¢/10¢ blinds)."""
    if stack_type == "50bb":
        return 500   # 50 big blinds at 10¢
    elif stack_type == "15bb":
        return 150   # 15 big blinds at 10¢
    else:
        raise ValueError(f"Invalid stack type: {stack_type}")


async def _join_duel_queue(
    user_id: str,
    entry_fee_cents: int,
    stack_type: str,
    display_name: str,
    websocket: WebSocket,
    challenge_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Handle JOIN_DUEL request.

    If challenge_id is provided, creates a private queue for friend challenges.
    Both players must join with the same challenge_id to be matched.

    Returns error dict if failed, None on success (messages sent via websocket).
    """
    # Validate entry fee
    if entry_fee_cents not in VALID_DUEL_ENTRY_FEES:
        return {
            "type": "ERROR",
            "code": "bad_request",
            "message": f"Invalid entry fee. Must be one of: {sorted(VALID_DUEL_ENTRY_FEES)}",
        }

    # Validate stack type
    if stack_type not in VALID_DUEL_STACK_TYPES:
        return {
            "type": "ERROR",
            "code": "bad_request",
            "message": f"Invalid stack type. Must be one of: {sorted(VALID_DUEL_STACK_TYPES)}",
        }

    # Check if user is already in a duel
    if user_id in _user_duels:
        return {
            "type": "ERROR",
            "code": "already_at_table",
            "message": "Already in a duel or duel queue",
        }

    # Check if user is at a regular table
    if manager.get_table_for_user(user_id):
        return {
            "type": "ERROR",
            "code": "already_at_table",
            "message": "Already at a table. Leave first before joining a duel.",
        }

    # Check and deduct entry fee
    if firestore and not user_id.startswith(("bot_", "user_bot_")):
        try:
            balance = await firestore.get_user_balance(user_id)
            if balance < entry_fee_cents:
                return {
                    "type": "ERROR",
                    "code": "insufficient_balance",
                    "message": f"Insufficient balance: {balance} cents < {entry_fee_cents} cents",
                }
            await firestore.deduct_balance(user_id, entry_fee_cents)
        except Exception as e:
            return {
                "type": "ERROR",
                "code": "bad_request",
                "message": str(e),
            }

    # Look for a waiting opponent (strict same-tier first, then any tier
    # if a waiter has already widened).
    found = _find_waiting_match(entry_fee_cents, stack_type, challenge_id)
    if found is not None:
        waiting_key, waiting_match = found
        _duel_queues.pop(waiting_key, None)

        # Cancel the queue timeout task
        if waiting_match.queue_timeout_task:
            waiting_match.queue_timeout_task.cancel()

        # Update match with player 2 (their own fee, possibly different from p1's)
        waiting_match.player2_id = user_id
        waiting_match.player2_display_name = display_name
        waiting_match.player2_entry_fee_cents = entry_fee_cents
        waiting_match.player2_is_bot = False
        waiting_match.status = "in_progress"

        if waiting_match.player1_entry_fee_cents != entry_fee_cents:
            print(
                f"[DUEL][CROSS] Cross-stake match: p1_fee={waiting_match.player1_entry_fee_cents} "
                f"p2_fee={entry_fee_cents} stack={stack_type} match_id={waiting_match.match_id}",
                flush=True,
            )

        # Track both players
        _user_duels[user_id] = waiting_match.match_id

        # Start the match
        await _start_duel_match(waiting_match, websocket)
        return None

    # No opponent waiting - create new match and add to queue
    match_id = f"duel_{uuid.uuid4().hex[:12]}"
    queue_key = _get_duel_queue_key(entry_fee_cents, stack_type, challenge_id)

    new_match = DuelMatch(
        match_id=match_id,
        table_id=None,
        stack_type=stack_type,
        status="waiting",
        player1_id=user_id,
        player1_display_name=display_name,
        player1_entry_fee_cents=entry_fee_cents,
    )

    # Track user
    _user_duels[user_id] = match_id

    # Add to queue
    _duel_queues[queue_key] = new_match

    # Start queue timeout task (10s strict -> widen -> 10s any tier -> bot)
    new_match.queue_timeout_task = asyncio.create_task(
        _duel_queue_timeout(new_match, queue_key)
    )

    # Send DUEL_QUEUED response
    await connections.send_to_user(user_id, {
        "type": "DUEL_QUEUED",
        "match_id": match_id,
        "entry_fee_cents": entry_fee_cents,
        "stack_type": stack_type,
    })

    print(f"[DUEL] {user_id} queued for {queue_key}, match_id={match_id}", flush=True)
    return None


async def _duel_queue_timeout(match: DuelMatch, queue_key: str) -> None:
    """Two-phase wait before bot fills in.

    Phase 1 (DUEL_STRICT_WAIT_SECONDS): strict same-tier matching only.
    Phase 2 (DUEL_WIDENED_WAIT_SECONDS): widen_level=1, accept any fee on
        the same stack_type. Each player is told they were matched at their
        own stake; the chip differential on payout is absorbed by the house.
    Then: spawn a bot.

    Friend-challenge matches never widen (challenge_id queues are private).
    """
    try:
        # Phase 1: strict
        await asyncio.sleep(DUEL_STRICT_WAIT_SECONDS)
        if queue_key not in _duel_queues:
            return

        # Widen unless this is a friend-challenge queue
        if not queue_key.startswith("challenge_"):
            match.widen_level = 1
            print(
                f"[DUEL] Widening match {match.match_id} to any tier "
                f"(stack={match.stack_type})",
                flush=True,
            )

        # Phase 2: any tier
        await asyncio.sleep(DUEL_WIDENED_WAIT_SECONDS)
        if queue_key not in _duel_queues:
            return

        waiting_match = _duel_queues.pop(queue_key, None)
        if waiting_match is None or waiting_match.match_id != match.match_id:
            return

        print(f"[DUEL] Queue timeout for {match.player1_id}, spawning bot", flush=True)

        # Fill with bot
        await _start_duel_match_with_bot(waiting_match)

    except asyncio.CancelledError:
        # Queue was cancelled (opponent found or player cancelled)
        pass
    except Exception as e:
        print(f"[DUEL] Queue timeout error: {e}", flush=True)


async def _start_duel_match(match: DuelMatch, player2_websocket: WebSocket) -> None:
    """Start a duel match between two human players."""
    from ..models import PlayerIdentity, Chips

    stake_id = _get_duel_stake_id(match.stack_type)
    buy_in_cents = _get_duel_buy_in(match.stack_type)

    # Create table
    table_id = manager.create_table(stake_id)
    match.table_id = table_id

    # Get runner and set duel mode
    runner = manager._tables.get(table_id)
    if runner:
        runner.set_duel_mode(True)

    # Track active duel
    _active_duels[table_id] = match

    # Seat player 1
    player1 = PlayerIdentity(
        user_id=match.player1_id,
        display_name=match.player1_display_name,
        avatar_url=None,
    )
    buy_in = Chips(amount=buy_in_cents)

    _, seat1 = await manager.add_player(
        match.player1_id, stake_id, buy_in, player1, table_id=table_id
    )
    connections.join_table(match.player1_id, table_id)

    # Seat player 2
    player2 = PlayerIdentity(
        user_id=match.player2_id,
        display_name=match.player2_display_name,
        avatar_url=None,
    )

    _, seat2 = await manager.add_player(
        match.player2_id, stake_id, buy_in, player2, table_id=table_id
    )
    connections.join_table(match.player2_id, table_id)

    # Fetch ratings for both players
    p1_rating, p1_wins, p1_losses = INITIAL_RATING, 0, 0
    p2_rating, p2_wins, p2_losses = INITIAL_RATING, 0, 0
    if firestore:
        p1_data = await firestore.get_duel_rating(match.player1_id)
        p2_data = await firestore.get_duel_rating(match.player2_id)
        if p1_data:
            p1_rating = int(p1_data.get("rating", INITIAL_RATING))
            p1_wins = p1_data.get("wins", 0)
            p1_losses = p1_data.get("losses", 0)
        if p2_data:
            p2_rating = int(p2_data.get("rating", INITIAL_RATING))
            p2_wins = p2_data.get("wins", 0)
            p2_losses = p2_data.get("losses", 0)

    # Send DUEL_MATCHED to both players (with opponent's rating/record)
    await connections.send_to_user(match.player1_id, {
        "type": "DUEL_MATCHED",
        "match_id": match.match_id,
        "opponent_display_name": match.player2_display_name,
        "is_bot": False,
        "opponent_rating": p2_rating,
        "opponent_wins": p2_wins,
        "opponent_losses": p2_losses,
    })

    await connections.send_to_user(match.player2_id, {
        "type": "DUEL_MATCHED",
        "match_id": match.match_id,
        "opponent_display_name": match.player1_display_name,
        "is_bot": False,
        "opponent_rating": p1_rating,
        "opponent_wins": p1_wins,
        "opponent_losses": p1_losses,
    })

    # Send TABLE_SNAPSHOT to both
    snapshot1 = await manager.get_snapshot(match.player1_id)
    snapshot2 = await manager.get_snapshot(match.player2_id)

    await connections.send_to_user(match.player1_id, snapshot1.model_dump(mode="json"))
    await connections.send_to_user(match.player2_id, snapshot2.model_dump(mode="json"))

    # Auto-start first hand after short delay
    asyncio.create_task(_duel_auto_start_hand(table_id, delay=5.0))

    print(f"[DUEL] Match started: {match.player1_display_name} vs {match.player2_display_name} at {table_id}", flush=True)


async def _start_duel_match_with_bot(match: DuelMatch) -> None:
    """Start a duel match with a bot opponent."""
    from ..models import PlayerIdentity, Chips

    stake_id = _get_duel_stake_id(match.stack_type)
    buy_in_cents = _get_duel_buy_in(match.stack_type)

    # Create table
    table_id = manager.create_table(stake_id)
    match.table_id = table_id
    match.status = "in_progress"

    # Get runner and set duel mode
    runner = manager._tables.get(table_id)
    if runner:
        runner.set_duel_mode(True)

    # Track active duel
    _active_duels[table_id] = match

    # Get a bot persona from the pool (matches player's rating if possible)
    persona_pool = get_persona_pool(firestore)

    # Get player's rating for matching
    player_rating = INITIAL_RATING
    if firestore:
        rating_doc = await firestore.get_duel_rating(match.player1_id)
        if rating_doc:
            player_rating = rating_doc.get("rating", INITIAL_RATING)

    persona = await persona_pool.get_available_persona(target_rating=player_rating)

    if persona:
        bot_user_id = persona["persona_id"]
        bot_display_name = persona["username"]  # Use username, not displayName
        print(f"[DUEL] Using bot persona: {bot_display_name} ({bot_user_id})", flush=True)
    else:
        # Fallback if all personas in use
        bot_user_id = f"bot_duel_{match.match_id[:8]}"
        bot_display_name = "duelbot"
        print(f"[DUEL] All personas in use, using fallback: {bot_display_name}", flush=True)

    match.player2_id = bot_user_id
    match.player2_display_name = bot_display_name
    match.player2_is_bot = True
    # Bot matches the human's fee — no cross-stake P&L on bot fills.
    match.player2_entry_fee_cents = match.player1_entry_fee_cents

    # Track bot in user_duels
    _user_duels[bot_user_id] = match.match_id

    # Seat player 1
    player1 = PlayerIdentity(
        user_id=match.player1_id,
        display_name=match.player1_display_name,
        avatar_url=None,
    )
    buy_in = Chips(amount=buy_in_cents)

    _, seat1 = await manager.add_player(
        match.player1_id, stake_id, buy_in, player1, table_id=table_id
    )
    connections.join_table(match.player1_id, table_id)

    # Fetch bot's rating
    bot_rating, bot_wins, bot_losses = INITIAL_RATING, 0, 0
    if firestore:
        bot_data = await firestore.get_duel_rating(bot_user_id)
        if bot_data:
            bot_rating = int(bot_data.get("rating", INITIAL_RATING))
            bot_wins = bot_data.get("wins", 0)
            bot_losses = bot_data.get("losses", 0)

    # Send DUEL_MATCHED to player 1 (with bot's rating/record)
    await connections.send_to_user(match.player1_id, {
        "type": "DUEL_MATCHED",
        "match_id": match.match_id,
        "opponent_display_name": bot_display_name,
        "is_bot": True,
        "opponent_rating": bot_rating,
        "opponent_wins": bot_wins,
        "opponent_losses": bot_losses,
    })

    # Spawn bot process
    proc = await _spawn_bot_process(
        table_id=table_id,
        bot_count=1,
        stake_id=stake_id,
        buy_in_cents=buy_in_cents,
        bot_ids=[bot_display_name],
    )
    _bot_processes[table_id] = [(bot_user_id, proc)]

    # Wait for bot to connect
    for attempt in range(50):  # Up to 5 seconds
        runner = manager._tables.get(table_id)
        if runner and runner.player_count >= 2:
            break
        await asyncio.sleep(0.1)

    # Send TABLE_SNAPSHOT to player 1
    snapshot1 = await manager.get_snapshot(match.player1_id)
    await connections.send_to_user(match.player1_id, snapshot1.model_dump(mode="json"))

    # Auto-start first hand after short delay
    asyncio.create_task(_duel_auto_start_hand(table_id, delay=5.0))

    print(f"[DUEL] Match started with bot: {match.player1_display_name} vs {bot_display_name} at {table_id}", flush=True)


async def _duel_auto_start_hand(table_id: str, delay: float = 2.0) -> None:
    """Auto-start a hand in a duel match."""
    await asyncio.sleep(delay)

    if table_id not in _active_duels:
        return

    runner = manager._tables.get(table_id)
    if not runner or runner._engine._status.value == "running":
        return

    try:
        events = await manager.start_hand(table_id)
        await handler._broadcast_events(table_id, None, events)
    except Exception as e:
        print(f"[DUEL] Error starting hand: {e}", flush=True)


async def _complete_duel_match(table_id: str, winner_id: str) -> None:
    """Complete a duel match and award the prize."""
    match = _active_duels.pop(table_id, None)
    if not match:
        print(f"[DUEL] Cannot complete - no active duel for table {table_id}", flush=True)
        return

    # Cancel any pending disconnect-grace task — this match is ending now and
    # the task would otherwise fire a second forfeit on a torn-down match.
    if match.disconnect_grace_task and not match.disconnect_grace_task.done():
        match.disconnect_grace_task.cancel()
    match.disconnect_grace_task = None
    match.disconnected_player_id = None

    # Clear _user_duels up front so any later exception (Firestore, send_to_user,
    # etc.) cannot leak entries and block these players from joining new duels
    # until process restart.
    _user_duels.pop(match.player1_id, None)
    if match.player2_id:
        _user_duels.pop(match.player2_id, None)

    match.status = "completed"
    match.winner_id = winner_id

    # Determine winner display name
    if winner_id == match.player1_id:
        winner_display_name = match.player1_display_name
        loser_id = match.player2_id
    else:
        winner_display_name = match.player2_display_name
        loser_id = match.player1_id

    # Get hands played from runner
    runner = manager._tables.get(table_id)
    hands_played = runner.hands_played if runner else 0

    # Per-player payouts. Each player was told they were matched at their
    # own stake, so each sees their own entry doubled in DUEL_ENDED. Winner
    # is paid 2 * winner's_entry. House absorbs the chip differential when
    # fees differ (cross-stake match).
    p1_entry = match.player1_entry_fee_cents
    p2_entry = match.player2_entry_fee_cents if match.player2_entry_fee_cents is not None else p1_entry
    p1_prize_cents = p1_entry * 2
    p2_prize_cents = p2_entry * 2
    if winner_id == match.player1_id:
        winner_payout_cents = p1_prize_cents
    else:
        winner_payout_cents = p2_prize_cents
    # P&L only meaningful for human-vs-human; bot matches don't change it
    # because the bot didn't pay a fee in the first place.
    if match.player2_is_bot:
        house_pnl_cents = 0
    else:
        house_pnl_cents = (p1_entry + p2_entry) - winner_payout_cents

    if p1_entry != p2_entry:
        print(
            f"[DUEL][CROSS] match_id={match.match_id} p1_fee={p1_entry} p2_fee={p2_entry} "
            f"winner_fee={p1_entry if winner_id == match.player1_id else p2_entry} "
            f"house_pnl={house_pnl_cents}",
            flush=True,
        )

    # =========================================================================
    # GLICKO RATING UPDATE - Fetch ratings in parallel for speed
    # =========================================================================
    rating_updates = {}  # user_id -> {new_rating, rating_change, wins, losses}

    if firestore:
        try:
            # Get current ratings for both players IN PARALLEL
            p1_rating_coro = firestore.get_duel_rating(match.player1_id)
            p2_rating_coro = firestore.get_duel_rating(match.player2_id)
            p1_rating_data, p2_rating_data = await asyncio.gather(p1_rating_coro, p2_rating_coro)

            # Parse player 1 rating (or use defaults)
            if p1_rating_data:
                p1_rating = p1_rating_data.get("rating", INITIAL_RATING)
                p1_rd = p1_rating_data.get("rd", INITIAL_RD)
                p1_wins = p1_rating_data.get("wins", 0)
                p1_losses = p1_rating_data.get("losses", 0)
                # Apply inactivity RD increase
                last_played_str = p1_rating_data.get("last_played")
                if last_played_str:
                    from datetime import datetime
                    last_played = datetime.fromisoformat(last_played_str)
                    p1_rd = update_rd_for_inactivity(p1_rd, last_played)
            else:
                p1_rating, p1_rd = get_default_rating()
                p1_wins, p1_losses = 0, 0

            # Parse player 2 rating (or use defaults)
            if p2_rating_data:
                p2_rating = p2_rating_data.get("rating", INITIAL_RATING)
                p2_rd = p2_rating_data.get("rd", INITIAL_RD)
                p2_wins = p2_rating_data.get("wins", 0)
                p2_losses = p2_rating_data.get("losses", 0)
                # Apply inactivity RD increase
                last_played_str = p2_rating_data.get("last_played")
                if last_played_str:
                    from datetime import datetime
                    last_played = datetime.fromisoformat(last_played_str)
                    p2_rd = update_rd_for_inactivity(p2_rd, last_played)
            else:
                p2_rating, p2_rd = get_default_rating()
                p2_wins, p2_losses = 0, 0

            # Calculate new ratings
            p1_score = 1.0 if winner_id == match.player1_id else 0.0
            p2_score = 1.0 if winner_id == match.player2_id else 0.0

            p1_new_rating, p1_new_rd = calculate_new_rating(
                p1_rating, p1_rd, p2_rating, p2_rd, p1_score
            )
            p2_new_rating, p2_new_rd = calculate_new_rating(
                p2_rating, p2_rd, p1_rating, p1_rd, p2_score
            )

            # Update win/loss counts
            if winner_id == match.player1_id:
                p1_wins += 1
                p2_losses += 1
            else:
                p2_wins += 1
                p1_losses += 1

            # Store rating updates for messages
            rating_updates[match.player1_id] = {
                "new_rating": int(round(p1_new_rating)),
                "rating_change": int(round(p1_new_rating - p1_rating)),
                "wins": p1_wins,
                "losses": p1_losses,
            }
            rating_updates[match.player2_id] = {
                "new_rating": int(round(p2_new_rating)),
                "rating_change": int(round(p2_new_rating - p2_rating)),
                "wins": p2_wins,
                "losses": p2_losses,
            }

            # Store rating data for background write
            p1_rating_update = (match.player1_id, p1_new_rating, p1_new_rd, p1_wins, p1_losses, p1_rating)
            p2_rating_update = (match.player2_id, p2_new_rating, p2_new_rd, p2_wins, p2_losses, p2_rating)

        except Exception as e:
            print(f"[DUEL] Error calculating ratings: {e}", flush=True)
            p1_rating_update = None
            p2_rating_update = None
    else:
        # No Firestore - no rating updates
        p1_rating_update = None
        p2_rating_update = None

    # =========================================================================
    # SEND DUEL_ENDED IMMEDIATELY - Don't block on Firestore writes
    # =========================================================================
    # Each player sees prize_pool_cents = 2 * their own entry fee. In a
    # same-tier match these are identical; in a cross-stake match each
    # side sees the prize they expected at their own stake.
    duel_ended_common = {
        "type": "DUEL_ENDED",
        "match_id": match.match_id,
        "winner_id": winner_id,
        "winner_display_name": winner_display_name,
        "hands_played": hands_played,
    }

    # Send to player 1
    msg = {
        **duel_ended_common,
        "prize_pool_cents": p1_prize_cents,
        "your_result": "won" if winner_id == match.player1_id else "lost",
    }
    if match.player1_id in rating_updates:
        ru = rating_updates[match.player1_id]
        msg["your_new_rating"] = ru["new_rating"]
        msg["your_rating_change"] = ru["rating_change"]
        msg["your_wins"] = ru["wins"]
        msg["your_losses"] = ru["losses"]
    await connections.send_to_user(match.player1_id, msg)

    # Send to player 2 (if human)
    if not match.player2_is_bot:
        msg = {
            **duel_ended_common,
            "prize_pool_cents": p2_prize_cents,
            "your_result": "won" if winner_id == match.player2_id else "lost",
        }
        if match.player2_id in rating_updates:
            ru = rating_updates[match.player2_id]
            msg["your_new_rating"] = ru["new_rating"]
            msg["your_rating_change"] = ru["rating_change"]
            msg["your_wins"] = ru["wins"]
            msg["your_losses"] = ru["losses"]
        await connections.send_to_user(match.player2_id, msg)

    print(f"[DUEL] DUEL_ENDED sent to players", flush=True)

    # =========================================================================
    # BACKGROUND: Write to Firestore (don't block the response)
    # =========================================================================
    async def _write_duel_to_firestore():
        """Background task to write duel data to Firestore."""
        try:
            # Award prize to winner — winner's own stake doubled.
            if not winner_id.startswith(("bot_", "user_bot_")):
                await firestore.add_balance(winner_id, winner_payout_cents)
                print(f"[DUEL] Awarded {winner_payout_cents} cents to winner {winner_id}", flush=True)

            # Update ratings
            if p1_rating_update:
                uid, new_r, new_rd, wins, losses, old_r = p1_rating_update
                await firestore.update_duel_rating(uid, new_r, new_rd, wins, losses)
                print(f"[DUEL] Updated rating for {uid}: {old_r:.0f} -> {new_r:.0f} ({new_r - old_r:+.0f})", flush=True)

            if p2_rating_update:
                uid, new_r, new_rd, wins, losses, old_r = p2_rating_update
                await firestore.update_duel_rating(uid, new_r, new_rd, wins, losses)
                print(f"[DUEL] Updated rating for {uid}: {old_r:.0f} -> {new_r:.0f} ({new_r - old_r:+.0f})", flush=True)

            # Write duel record. entry_fee_cents kept for back-compat queries
            # (= player1's fee); cross-stake details in the new fields.
            duel_record = DuelRecord(
                match_id=match.match_id,
                table_id=table_id,
                entry_fee_cents=p1_entry,
                stack_type=match.stack_type,
                prize_pool_cents=winner_payout_cents,
                player1_id=match.player1_id,
                player1_display_name=match.player1_display_name,
                player1_is_bot=False,
                player2_id=match.player2_id,
                player2_display_name=match.player2_display_name,
                player2_is_bot=match.player2_is_bot,
                winner_id=winner_id,
                winner_display_name=winner_display_name,
                hands_played=hands_played,
                started_at=match.created_at,
                ended_at=datetime.utcnow(),
                player1_entry_fee_cents=p1_entry,
                player2_entry_fee_cents=p2_entry,
                house_pnl_cents=house_pnl_cents,
            )
            await firestore.write_duel_record(duel_record)
            print(f"[DUEL] Duel record written to Firestore", flush=True)

        except Exception as e:
            print(f"[DUEL] Error in background Firestore writes: {e}", flush=True)

    # Run Firestore writes in background - don't await
    if firestore:
        asyncio.create_task(_write_duel_to_firestore())

    # _user_duels was already cleared at the top of this function.

    # Release bot persona back to pool
    if match.player2_is_bot:
        persona_pool = get_persona_pool()
        persona_pool.release_persona(match.player2_id)

    # Clean up bot processes if any
    if table_id in _bot_processes:
        for bot_user_id, proc in _bot_processes.pop(table_id, []):
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # Remove players from table
    for player_id in [match.player1_id, match.player2_id]:
        try:
            if player_id in manager._user_tables:
                del manager._user_tables[player_id]
        except Exception:
            pass

    # Clean up connections
    connections.leave_table(match.player1_id)
    if not match.player2_is_bot:
        connections.leave_table(match.player2_id)

    # Delete the table
    if table_id in manager._tables:
        runner = manager._tables[table_id]
        await runner.stop()
        del manager._tables[table_id]

    print(f"[DUEL] Match {match.match_id} completed. Winner: {winner_display_name}, Hands: {hands_played}", flush=True)


async def _cancel_duel_queue(user_id: str) -> dict:
    """Cancel a pending duel queue and refund entry fee."""
    match_id = _user_duels.get(user_id)
    if not match_id:
        return {
            "type": "ERROR",
            "code": "bad_request",
            "message": "Not in a duel queue",
        }

    # Find and remove from queue
    for queue_key, match in list(_duel_queues.items()):
        if match.match_id == match_id and match.player1_id == user_id:
            # Cancel timeout task
            if match.queue_timeout_task:
                match.queue_timeout_task.cancel()

            # Remove from queue
            _duel_queues.pop(queue_key, None)
            _user_duels.pop(user_id, None)

            # Refund entry fee (only player1 is in the queue when waiting).
            refund_cents = match.player1_entry_fee_cents
            if firestore and not user_id.startswith(("bot_", "user_bot_")):
                try:
                    await firestore.add_balance(user_id, refund_cents)
                    print(f"[DUEL] Refunded {refund_cents} cents to {user_id}", flush=True)
                except Exception as e:
                    print(f"[DUEL] Error refunding: {e}", flush=True)

            return {
                "type": "DUEL_CANCELLED",
                "match_id": match_id,
                "refunded_cents": refund_cents,
            }

    # Not in queue. Two cases:
    #  (a) match transitioned to active and is live → leave _user_duels alone;
    #      the disconnect grace path / _complete_duel_match owns the cleanup.
    #  (b) match no longer exists anywhere (engine cleanup raced ahead, or a
    #      prior _complete_duel_match crashed before clearing _user_duels) →
    #      scrub the stale entry so the user can queue again.
    for active_match in _active_duels.values():
        if active_match.match_id == match_id:
            return {
                "type": "ERROR",
                "code": "bad_request",
                "message": "Cannot cancel - match already started",
            }

    print(
        f"[DUEL] Scrubbed stale _user_duels entry for {user_id} "
        f"(match_id={match_id} not in queue or active duels)",
        flush=True,
    )
    _user_duels.pop(user_id, None)
    return {
        "type": "DUEL_CANCELLED",
        "match_id": match_id,
        "refunded_cents": 0,
    }


async def _handle_duel_disconnect(user_id: str, table_id: str) -> None:
    """Handle player disconnection during a duel (forfeit after grace period)."""
    match = _active_duels.get(table_id)
    if not match:
        return

    # Determine winner (opponent of disconnected player)
    if user_id == match.player1_id:
        winner_id = match.player2_id
    elif user_id == match.player2_id:
        winner_id = match.player1_id
    else:
        return

    print(f"[DUEL] Player {user_id} disconnected, forfeiting to {winner_id}", flush=True)
    await _complete_duel_match(table_id, winner_id)


def _duel_opponent_id(match: DuelMatch, user_id: str) -> Optional[str]:
    """Return the opponent's user_id within a duel match, or None if user isn't in it."""
    if user_id == match.player1_id:
        return match.player2_id
    if user_id == match.player2_id:
        return match.player1_id
    return None


async def _start_duel_disconnect_grace(user_id: str, table_id: str) -> None:
    """Notify opponent of disconnect, mark match, and schedule the forfeit task.

    Idempotent: if a grace task is already running for this user on this match,
    this is a no-op (e.g. transient WS hiccup that re-fires the disconnect path).
    """
    match = _active_duels.get(table_id)
    if not match:
        return

    # Already in grace for this user — don't double-notify or double-schedule.
    if match.disconnected_player_id == user_id and match.disconnect_grace_task and not match.disconnect_grace_task.done():
        return

    opponent_id = _duel_opponent_id(match, user_id)
    if opponent_id is None:
        return

    grace_seconds = DUEL_DISCONNECT_GRACE_SECONDS
    forfeit_at_ms = int(time.time() * 1000) + int(grace_seconds * 1000)

    match.disconnected_player_id = user_id

    # Notify opponent (skip if opponent is a bot — bots don't render UI).
    if not match.player2_is_bot or opponent_id != match.player2_id:
        try:
            await connections.send_to_user(opponent_id, {
                "type": "OPPONENT_DISCONNECTED",
                "match_id": match.match_id,
                "disconnected_user_id": user_id,
                "grace_period_seconds": grace_seconds,
                "forfeit_at_ms": forfeit_at_ms,
            })
        except Exception as e:
            print(f"[DUEL] Error sending OPPONENT_DISCONNECTED to {opponent_id}: {e}", flush=True)

    async def _grace_expired():
        try:
            await asyncio.sleep(grace_seconds)
            # Re-check: user might have reconnected (cancel_grace clears field).
            if not connections.is_connected(user_id):
                await _handle_duel_disconnect(user_id, table_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[DUEL] Grace task error: {e}", flush=True)

    match.disconnect_grace_task = asyncio.create_task(_grace_expired())


async def _resolve_duel_reconnect(user_id: str) -> None:
    """If user was the disconnected player in an active duel, clear the grace
    state and notify the opponent that they reconnected.

    Safe to call on every successful AUTH; no-op when not applicable.
    """
    match_id = _user_duels.get(user_id)
    if not match_id:
        return

    # Find the active match for this user.
    match: Optional[DuelMatch] = None
    table_id: Optional[str] = None
    for tid, m in _active_duels.items():
        if m.match_id == match_id and (user_id in (m.player1_id, m.player2_id)):
            match = m
            table_id = tid
            break
    if match is None or match.disconnected_player_id != user_id:
        return

    # Cancel the pending forfeit.
    if match.disconnect_grace_task and not match.disconnect_grace_task.done():
        match.disconnect_grace_task.cancel()
    match.disconnect_grace_task = None
    match.disconnected_player_id = None

    opponent_id = _duel_opponent_id(match, user_id)
    if opponent_id is None or (match.player2_is_bot and opponent_id == match.player2_id):
        return

    try:
        await connections.send_to_user(opponent_id, {
            "type": "OPPONENT_RECONNECTED",
            "match_id": match.match_id,
            "reconnected_user_id": user_id,
        })
    except Exception as e:
        print(f"[DUEL] Error sending OPPONENT_RECONNECTED to {opponent_id}: {e}", flush=True)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Main WebSocket endpoint.

    Protocol:
    1. Client connects
    2. Client sends AUTH with token
    3. Server validates and responds AUTH_OK
    4. Client sends JOIN_POOL to join a table
    5. Server responds with TABLE_SNAPSHOT
    6. Game loop: client sends ACTIONs, server broadcasts STATE_DELTAs
    """
    user_id: Optional[str] = None

    try:
        # Must accept before receiving
        await websocket.accept()

        # Wait for AUTH message first
        data = await websocket.receive_json()

        if data.get("type") != "AUTH":
            await websocket.send_json({
                "type": "ERROR",
                "code": "bad_request",
                "message": "First message must be AUTH"
            })
            await websocket.close(code=4001)
            return

        token = data.get("token", "")
        auth = AuthService()
        user_id = auth.verify_token(token)

        if not user_id:
            await websocket.send_json({
                "type": "ERROR",
                "code": "unauthorized",
                "message": "Invalid token"
            })
            await websocket.close(code=4001)
            return

        # Register connection (after accept, but before sending AUTH_OK)
        # Note: connect() no longer calls accept() since we did it above
        if user_id in connections._connections:
            old_ws = connections._connections[user_id]
            try:
                await old_ws.close()
            except Exception:
                pass
        connections._connections[user_id] = websocket
        # Seed `_last_seen` immediately. Without this, the heartbeat reaper
        # treats freshly-authed users (whose first inbound message hasn't yet
        # called `mark_seen`) as stale and closes their socket within ~5s,
        # producing a reconnect storm for anyone idling in the lobby.
        connections._last_seen[user_id] = time.monotonic()

        # Cancel any pending grace period - player has reconnected
        was_in_grace_period = reconnect_mgr.cancel_grace_period(user_id)
        if was_in_grace_period:
            logger.info("Player reconnected within grace period", user_id=user_id)

        # Send AUTH_OK (route through ConnectionManager so the per-user lock
        # protects subsequent concurrent producers from interleaving frames).
        response = await handler.handle_auth(
            user_id, token, data.get("protocol_version", 1)
        )
        await connections.send_to_user(user_id, response)

        # If reconnecting and already at table, send snapshot
        # This can happen either from normal reconnect OR from grace period reconnect
        if response.get("current_table_id"):
            table_id = response.get("current_table_id")
            connections.join_table(user_id, table_id)
            try:
                snapshot = await manager.get_snapshot(user_id)
                await connections.send_to_user(user_id, snapshot.model_dump(mode="json"))
                if was_in_grace_period:
                    logger.info("Sent snapshot to reconnected player", user_id=user_id, table_id=table_id)

                # Check if it's this player's turn - re-send ACTION_REQUEST if so
                if snapshot.hand:
                    actor_seat = snapshot.hand.actor_seat
                    your_seat = snapshot.your_seat
                    if actor_seat is not None and actor_seat == your_seat:
                        logger.info(f"Reconnected player needs to act (seat {your_seat}), sending ACTION_REQUEST", user_id=user_id)
                        await handler._send_action_request(user_id, snapshot)
            except Exception as e:
                logger.warning(f"Failed to send snapshot on reconnect: {e}", user_id=user_id)

        # If this user was mid-forfeit in an active duel, cancel the grace
        # task and tell the opponent they're back. Safe no-op otherwise.
        await _resolve_duel_reconnect(user_id)

        # Message loop
        while True:
            data = await websocket.receive_json()
            connections.mark_seen(user_id)
            msg_type = data.get("type")

            # Debug: log every message received
            if msg_type == "ACTION":
                print(f"[RECV] {user_id}: ACTION received hand={data.get('hand_id')} action={data.get('action')}", flush=True)

            if msg_type == "JOIN_POOL":
                stake_id = data.get("stake_id", "nlh_1_2")
                is_pro = bool(data.get("is_pro", False))
                response, table_id, seat, display_name, buy_in = await handler.handle_join_pool(
                    user_id,
                    stake_id,
                    data.get("buy_in_cents"),
                    data.get("display_name", "Player"),
                )
                await connections.send_to_user(user_id, response)
                # Complete join AFTER sending response to avoid race condition
                if table_id is not None:
                    await handler.complete_join(user_id, table_id, seat, display_name, buy_in, stake_id, is_pro=is_pro)

            elif msg_type == "JOIN_TABLE":
                stake_id = data.get("stake_id", "nlh_1_2")
                is_pro = bool(data.get("is_pro", False))
                response, table_id, seat, display_name, buy_in = await handler.handle_join_table(
                    user_id,
                    data.get("table_id"),
                    stake_id,
                    data.get("buy_in_cents"),
                    data.get("display_name", "Player"),
                )
                await connections.send_to_user(user_id, response)
                # Complete join AFTER sending response to avoid race condition
                if table_id is not None:
                    await handler.complete_join(user_id, table_id, seat, display_name, buy_in, stake_id, is_pro=is_pro)

            elif msg_type == "CREATE_BOT_TABLE":
                try:
                    # Auto-calculate bot_count from stake's max_players if not provided
                    stake_id = data.get("stake_id", "nlh_1_2")
                    stake_config = manager._stake_configs.get(stake_id)
                    default_bot_count = (stake_config.max_players - 1) if stake_config else 5

                    response = await _create_bot_table(
                        user_id=user_id,
                        stake_id=stake_id,
                        buy_in_cents=data.get("buy_in_cents"),
                        display_name=data.get("display_name", "Player"),
                        bot_count=data.get("bot_count") or default_bot_count,
                        auto_top_up=data.get("auto_top_up", True),
                        blitz_mode=data.get("blitz_mode", False),
                        persona_ids=data.get("persona_ids"),
                        bot_ids=data.get("bot_ids"),
                        client_session_id=data.get("client_session_id"),
                        is_pro=bool(data.get("is_pro", False)),
                    )
                    await connections.send_to_user(user_id, response)
                except Exception as e:
                    await connections.send_to_user(user_id, {
                        "type": "ERROR",
                        "code": "bad_request",
                        "message": str(e),
                    })

            elif msg_type == "JOIN_DUEL":
                error = await _join_duel_queue(
                    user_id=user_id,
                    entry_fee_cents=data.get("entry_fee_cents", 200),
                    stack_type=data.get("stack_type", "50bb"),
                    display_name=data.get("display_name", "Player"),
                    websocket=websocket,
                    challenge_id=data.get("challenge_id"),
                )
                if error:
                    await connections.send_to_user(user_id, error)

            elif msg_type == "CANCEL_DUEL":
                response = await _cancel_duel_queue(user_id)
                await connections.send_to_user(user_id, response)

            elif msg_type == "ACTION":
                response = await handler.handle_action(
                    user_id,
                    data.get("hand_id"),
                    data.get("action_id"),
                    data.get("action"),
                    data.get("amount_cents"),
                    data.get("decision_metadata"),
                )
                if response:  # Error response
                    await connections.send_to_user(user_id, response)

            elif msg_type == "LEAVE_TABLE":
                response = await handler.handle_leave_table(user_id)
                await connections.send_to_user(user_id, response)

            elif msg_type == "NEXT_HAND":
                response = await handler.handle_next_hand(user_id)
                if response:  # Error response
                    await connections.send_to_user(user_id, response)

            elif msg_type == "ANIMATION_COMPLETE":
                await handler.handle_animation_complete(user_id)

            elif msg_type == "PING":
                response = await handler.handle_ping(
                    user_id, data.get("client_ts", 0)
                )
                await connections.send_to_user(user_id, response)

            elif msg_type == "REQUEST_SNAPSHOT":
                # Client detected a seq gap and wants a fresh snapshot
                # without doing a full reconnect. Reuse manager.get_snapshot
                # (same path used by the post-AUTH reconnect flow). If it's
                # the requester's turn, also re-send ACTION_REQUEST so the
                # client can drop its local synthesize-and-guess path
                # (former iOS behaviour drifted out of sync with the
                # server timer — see INVESTIGATION.md P1-9).
                try:
                    snapshot = await manager.get_snapshot(user_id)
                    if snapshot is not None:
                        await connections.send_to_user(
                            user_id, snapshot.model_dump(mode="json")
                        )
                        if snapshot.hand:
                            actor_seat = snapshot.hand.actor_seat
                            your_seat = snapshot.your_seat
                            if actor_seat is not None and actor_seat == your_seat:
                                await handler._send_action_request(user_id, snapshot)
                    else:
                        await connections.send_to_user(user_id, {
                            "type": "ERROR",
                            "code": "not_at_table",
                            "message": "Cannot snapshot — not at a table",
                        })
                except Exception as e:
                    await connections.send_to_user(user_id, {
                        "type": "ERROR",
                        "code": "internal_error",
                        "message": f"Snapshot failed: {e}",
                    })

            elif msg_type == "TOP_UP_REQUEST":
                response = await handler.handle_topup_request(
                    user_id, data.get("request_id", "")
                )
                await connections.send_to_user(user_id, response)

            elif msg_type == "SET_AUTO_TOP_UP":
                response = await handler.handle_set_auto_top_up(
                    user_id, data.get("enabled", True)
                )
                await connections.send_to_user(user_id, response)

            elif msg_type == "QUIP":
                # Bot quip message - broadcast to table
                await handler.handle_quip(
                    user_id,
                    data.get("hand_id", ""),
                    data.get("seat", 0),
                    data.get("text", ""),
                )
                # No response needed - just broadcast

            else:
                await connections.send_to_user(user_id, {
                    "type": "ERROR",
                    "code": "unknown_message_type",
                    "message": f"Unknown message type: {msg_type}"
                })

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", user_id=user_id)
    except Exception as e:
        # Log unexpected errors but don't crash
        logger.exception(f"WebSocket error: {e}", user_id=user_id)
    finally:
        if user_id:
            # Remove WebSocket from active connections
            connections._connections.pop(user_id, None)

            # Check bot table owners FIRST - clean up immediately (no grace period)
            if user_id in _bot_table_owners:
                logger.info("Bot table owner disconnected, cleaning up immediately", user_id=user_id)
                await _cleanup_bot_table(user_id)
            elif user_id in _user_duels:
                # Check if in duel queue (waiting) vs active duel
                match_id = _user_duels.get(user_id)
                table_id = manager.get_table_for_user(user_id)

                if table_id and table_id in _active_duels:
                    # In active duel - notify opponent, start grace period, then forfeit.
                    logger.info(f"Duel player disconnected, starting {DUEL_DISCONNECT_GRACE_SECONDS}s grace period", user_id=user_id)
                    await _start_duel_disconnect_grace(user_id, table_id)
                else:
                    # In queue - cancel and refund
                    await _cancel_duel_queue(user_id)
                    logger.info("Duel queue cancelled for disconnected player", user_id=user_id)
            else:
                # Regular players: start grace period for reconnection
                table_id = manager.get_table_for_user(user_id)
                if table_id and not user_id.startswith(("bot_", "user_bot_")):
                    reconnect_mgr.start_grace_period(user_id, table_id)
                    logger.info("Grace period started for disconnected player", user_id=user_id, table_id=table_id)
                else:
                    # No table - just clean up connection tracking
                    connections.disconnect(user_id)
