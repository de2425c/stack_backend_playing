"""
Message handler for WebSocket protocol.

Routes client messages to TableManager and orchestrates broadcasts.
"""

import time
from typing import Optional

import asyncio
from typing import TYPE_CHECKING

from ..models import (
    ClientAction,
    Chips,
    ErrorCode,
    ErrorMessage,
    AuthOkMessage,
    StateDeltaMessage,
    TableLeftMessage,
    PongMessage,
    PlayerIdentity,
)
from ..manager import TableManager, TimeoutActionCommand
from .connection import ConnectionManager
from .auth import AuthService

if TYPE_CHECKING:
    from .timer import ActionTimerService, PendingAction
    from ..session import SessionTracker


class MessageHandler:
    """Handles client messages and orchestrates responses."""

    def __init__(
        self,
        manager: TableManager,
        connections: ConnectionManager,
        auth: AuthService,
        timer: Optional["ActionTimerService"] = None,
        session_tracker: Optional["SessionTracker"] = None,
    ):
        self._manager = manager
        self._connections = connections
        self._auth = auth
        self._timer = timer
        self._session_tracker = session_tracker
        self._processed_actions: dict[str, float] = {}  # action_id -> timestamp
        self._processed_actions_max_age = 60.0  # Clear entries older than 60s
        # Track tables waiting for animation completion before next hand
        self._animation_complete_events: dict[str, asyncio.Event] = {}  # table_id -> Event
        # Pending REBUY messages queued at hand_end (auto-rebuy + bot
        # top-ups). Broadcast on the NEXT hand_start so opponent chip
        # counts don't visibly jump from $0 in the middle of the
        # winner celebration (P0-5).
        self._pending_rebuy_msgs: dict[str, list[dict]] = {}

    async def handle_auth(self, user_id: str, token: str, protocol_version: int) -> dict:
        """Handle AUTH message. Returns response dict.

        Note: the WS endpoint has already verified the token before reaching
        this method (and passed the verified user_id in). We trust the
        caller-supplied user_id rather than re-running verify_id_token,
        which would be a second sync Firebase RPC on the event loop's
        critical path.
        """
        # Check if user already at a table (reconnect)
        table_id = self._manager.get_table_for_user(user_id)

        return AuthOkMessage(
            user_id=user_id,
            current_table_id=table_id,
        ).model_dump(mode="json")

    async def handle_join_pool(
        self, user_id: str, stake_id: str, buy_in_cents: int, display_name: str
    ) -> tuple[dict, str, int]:
        """Handle JOIN_POOL message. Returns (TABLE_SNAPSHOT, table_id, seat) or (ERROR, None, None)."""
        try:
            player = PlayerIdentity(
                user_id=user_id,
                display_name=display_name,
                avatar_url=None,
            )
            table_id, seat = await self._manager.add_player(
                user_id, stake_id, Chips(amount=buy_in_cents), player
            )

            # Get snapshot FIRST (before registering for broadcasts)
            snapshot = await self._manager.get_snapshot(user_id)

            # Auto-start hand when we reach min players (if no hand in progress)
            runner = self._manager._tables.get(table_id)
            if runner:
                player_count = runner.player_count
                min_players = runner._config.min_players_to_start
                status = runner._engine._status
                has_hand = status.value == "running"
                print(f"[AUTO-START CHECK] table={table_id} players={player_count} min={min_players} status={status} has_hand={has_hand}")
                if player_count >= min_players and not has_hand:
                    print(f"[AUTO-START] Triggering auto-start for table {table_id}")
                    asyncio.create_task(self._auto_start_next_hand(table_id, delay=1.5))

            # Return snapshot and join info for caller to complete registration
            return (snapshot.model_dump(mode="json"), table_id, seat, display_name, buy_in_cents)

        except ValueError as e:
            return (ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json"), None, None, None, None)

    async def handle_join_table(
        self, user_id: str, table_id: str, stake_id: str, buy_in_cents: int, display_name: str
    ) -> tuple:
        """Handle JOIN_TABLE message. Joins a specific table by ID.

        Returns (snapshot, table_id, seat, display_name, buy_in_cents) or (error, None, None, None, None).
        """
        try:
            player = PlayerIdentity(
                user_id=user_id,
                display_name=display_name,
                avatar_url=None,
            )
            result_table_id, seat = await self._manager.add_player(
                user_id, stake_id, Chips(amount=buy_in_cents), player,
                table_id=table_id,
            )

            # Get snapshot FIRST (before registering for broadcasts)
            snapshot = await self._manager.get_snapshot(user_id)

            # Auto-start hand when we reach min players
            runner = self._manager._tables.get(result_table_id)
            if runner:
                player_count = runner.player_count
                min_players = runner._config.min_players_to_start
                status = runner._engine._status
                has_hand = status.value == "running"
                if player_count >= min_players and not has_hand:
                    asyncio.create_task(self._auto_start_next_hand(result_table_id, delay=1.5))

            return (snapshot.model_dump(mode="json"), result_table_id, seat, display_name, buy_in_cents)

        except ValueError as e:
            return (ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json"), None, None, None, None)

    async def complete_join(
        self, user_id: str, table_id: str, seat: int, display_name: str, buy_in_cents: int, stake_id: str = "nlh_1_2", is_pro: bool = False
    ) -> None:
        """Complete join by registering for broadcasts and notifying other players.

        Call this AFTER sending the snapshot response to avoid race conditions.
        """
        # Start session tracking — humans only. Bot subprocesses route
        # through the same JOIN_POOL / JOIN_TABLE codepath but never send
        # LEAVE_TABLE (the orphan-table flow kills the subprocess), so a
        # start_session here would leak the session entry until process
        # restart.
        is_bot_subprocess = user_id.startswith(("bot_", "user_bot_"))
        if self._session_tracker and not is_bot_subprocess:
            self._session_tracker.start_session(
                user_id=user_id,
                table_id=table_id,
                stake_id=stake_id,
                seat=seat,
                buy_in_cents=buy_in_cents,
                display_name=display_name,
                is_pro=is_pro,
            )

        # Register for broadcasts
        self._connections.join_table(user_id, table_id)

        # Broadcast SEAT_UPDATE to other players at the table
        seat_update = {
            "type": "SEAT_UPDATE",
            "seat": {
                "seat_index": seat,
                "status": "seated",
                "player": {
                    "user_id": user_id,
                    "display_name": display_name,
                    "avatar_url": None,
                },
                "chips": {"amount": buy_in_cents},
                "bet": {"amount": 0},
                "is_button": False,
                "is_connected": True,
            },
            "seq": None,
        }
        await self._connections.broadcast_to_table(table_id, seat_update, exclude=user_id)

    async def handle_action(
        self,
        user_id: str,
        hand_id: str,
        action_id: str,
        action: str,
        amount_cents: Optional[int],
        decision_metadata: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Handle ACTION message.

        Returns error dict or None (broadcasts results on success).
        """
        # Clean old entries from idempotency cache
        now = time.time()
        old_keys = [k for k, ts in self._processed_actions.items()
                    if now - ts > self._processed_actions_max_age]
        for k in old_keys:
            del self._processed_actions[k]

        # Idempotency check
        if action_id in self._processed_actions:
            print(f"[IDEMPOTENT] Ignoring duplicate action_id={action_id} user={user_id}", flush=True)
            return None  # Already processed, ignore

        # Deadline check - reject if expired
        if self._timer and self._timer.is_expired(user_id):
            return ErrorMessage(
                code=ErrorCode.ACTION_TIMEOUT,
                message="Action deadline expired",
                ref_msg_id=action_id,
            ).model_dump(mode="json")

        # Clear deadline since action arrived in time
        if self._timer:
            self._timer.clear_deadline(user_id)

        # Capture table_id BEFORE processing - user might be removed during action
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message="User not at any table",
                ref_msg_id=action_id,
            ).model_dump(mode="json")

        try:
            client_action = ClientAction(action)
            amount = Chips(amount=amount_cents) if amount_cents else None

            events = await self._manager.route_action(user_id, hand_id, client_action, amount, decision_metadata)

            # Mark as processed (with timestamp for cleanup)
            self._processed_actions[action_id] = time.time()

            # Broadcast to table (table_id captured before action processing)
            await self._broadcast_events(table_id, hand_id, events)

            return None  # Success - no direct response, just broadcasts

        except ValueError as e:
            error_code = self._map_error(str(e))
            print(f"[ACTION ERROR] user={user_id} hand={hand_id} action={action} error={str(e)}")
            return ErrorMessage(
                code=error_code,
                message=str(e),
                ref_msg_id=action_id,
            ).model_dump(mode="json")

    async def handle_leave_table(self, user_id: str) -> dict:
        """Handle LEAVE_TABLE message. Returns TABLE_LEFT or ERROR."""
        try:
            # Get table_id and seat before removing
            table_id = self._manager.get_table_for_user(user_id)
            seat_index = None
            if table_id:
                runner = self._manager._tables.get(table_id)
                if runner:
                    for i, seat in enumerate(runner._engine._seats):
                        if seat and seat.player and seat.player.user_id == user_id:
                            seat_index = i
                            break

            # Flush any pending REBUY messages queued at the prior hand_end
            # before responding (P0-5 follow-up). Otherwise the user could
            # leave the table during the celebration window of a hand they
            # auto-rebought into and their session totals would miss the
            # rebuy — profit calculation would be wrong.
            if table_id:
                pending = self._pending_rebuy_msgs.pop(table_id, [])
                for rebuy_msg in pending:
                    await self._connections.send_to_user(user_id, rebuy_msg)
                    # Re-queue messages for any other players left at the
                    # table — but only ones the leaver wasn't the seat of.
                    # The leaver's auto-rebuy never publishes a REBUY to
                    # other players (REBUY is broadcast, not unicast, and
                    # other players will get the seat-empty SEAT_UPDATE
                    # right after this anyway).
                    if rebuy_msg.get("seat") != seat_index:
                        self._pending_rebuy_msgs.setdefault(table_id, []).append(rebuy_msg)

            chips = await self._manager.remove_player(user_id)
            self._connections.leave_table(user_id)

            # End session tracking (triggers analysis)
            if self._session_tracker:
                self._session_tracker.end_session(user_id, chips.amount)

            # Credit balance back to wallet (skip for bots)
            if (
                chips.amount > 0
                and self._manager._firestore
                and not user_id.startswith(("bot_", "user_bot_"))
            ):
                await self._manager._firestore.add_balance(user_id, chips.amount)

            # Broadcast SEAT_UPDATE (empty seat) to remaining players
            if table_id and seat_index is not None:
                seat_update = {
                    "type": "SEAT_UPDATE",
                    "seat": {
                        "seat_index": seat_index,
                        "status": "empty",
                        "player": None,
                        "chips": {"amount": 0},
                        "bet": {"amount": 0},
                        "is_button": False,
                        "is_connected": False,
                    },
                    "seq": None,
                }
                await self._connections.broadcast_to_table(table_id, seat_update)

            return TableLeftMessage(final_chips=chips).model_dump(mode="json")

        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_ping(self, user_id: str, client_ts: int) -> dict:
        """Handle PING message. Returns PONG."""
        return PongMessage(
            client_ts=client_ts,
            server_ts=int(time.time() * 1000),
        ).model_dump(mode="json")

    async def handle_next_hand(self, user_id: str) -> Optional[dict]:
        """Handle NEXT_HAND message from client. Starts the next hand."""
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message="Not at a table",
            ).model_dump(mode="json")

        try:
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            return None
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_animation_complete(self, user_id: str) -> Optional[dict]:
        """Handle ANIMATION_COMPLETE message from client. Signals animations are done."""
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            return None  # Silently ignore if not at table

        # Signal that this table's animation is complete
        event = self._animation_complete_events.get(table_id)
        if event:
            print(f"[ANIMATION] Client {user_id[:20]}... signaled animation complete for {table_id}")
            event.set()
        else:
            print(f"[ANIMATION] No pending animation event for {table_id} (user: {user_id[:20]}...)")

        return None

    async def handle_topup_request(self, user_id: str, request_id: str) -> dict:
        """Handle TOP_UP_REQUEST message. Queue a manual top-up for next hand."""
        try:
            topup_amount, new_stack = await self._manager.request_topup(user_id)
            return {
                "type": "TOP_UP_PENDING",
                "request_id": request_id,
                "topup_amount": {"amount": topup_amount},
                "new_stack": {"amount": new_stack},
            }
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_set_auto_top_up(self, user_id: str, enabled: bool) -> dict:
        """Handle SET_AUTO_TOP_UP message. Toggle auto top-up for a player mid-session."""
        try:
            table_id = self._manager._user_tables.get(user_id)
            if table_id is None:
                raise ValueError("User not at any table")

            runner = self._manager._tables.get(table_id)
            if runner is None:
                raise ValueError("Table not found")

            # Find user's seat and update auto_topup_enabled
            for seat_state in runner._engine._seats:
                if seat_state is not None and seat_state.player.user_id == user_id:
                    seat_state.auto_topup_enabled = enabled
                    print(f"[AUTO_TOP_UP] Set auto_topup_enabled={enabled} for user {user_id[:20]}...")
                    return {
                        "type": "AUTO_TOP_UP_SET",
                        "enabled": enabled,
                    }

            raise ValueError("User not seated at table")
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_start_hand(self, table_id: str) -> Optional[dict]:
        """
        Handle start hand request (from debug endpoint).

        Returns error dict or None (broadcasts results on success).
        """
        try:
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            return None
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def _broadcast_events(
        self, table_id: str, hand_id: Optional[str], events: list
    ) -> None:
        """Broadcast STATE_DELTA to all users at table."""
        user_ids = self._connections.get_table_users(table_id)
        if not user_ids:
            # Log this case to help debug disconnection issues
            event_types = [e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", "?") for e in events]
            print(f"[BROADCAST] SKIPPED table={table_id} events={event_types} (no connected users)", flush=True)
            return

        # Check if this is a hand_started event - if so, send snapshots with hole cards
        is_hand_start = any(
            getattr(e, 'event_type', None) == 'hand_started' or
            (isinstance(e, dict) and e.get('event_type') == 'hand_started')
            for e in events
        )

        # Check if this is a hand_ended event - if so, auto-start next hand
        is_hand_end = any(
            getattr(e, 'event_type', None) == 'hand_ended' or
            (isinstance(e, dict) and e.get('event_type') == 'hand_ended')
            for e in events
        )

        # Fetch every per-user snapshot in a single runner round-trip.
        # Before the batch command this loop issued N separate snapshot
        # commands serialised on the runner queue; on a 6-max table that
        # was the dominant broadcast latency.
        try:
            snapshots_by_user = await self._manager.get_snapshots_batch(
                table_id, list(user_ids)
            )
        except Exception as e:
            print(f"[BROADCAST] Batch snapshot failed: {e}", flush=True)
            snapshots_by_user = {}

        # Pick a representative snapshot (any user's) for seq + actor_seat.
        if snapshots_by_user:
            representative = next(iter(snapshots_by_user.values()))
            seq = representative.seq
            if hand_id is None and representative.hand:
                hand_id = representative.hand.hand_id
            actor_seat = representative.hand.actor_seat if representative.hand else None
        else:
            seq = 0
            actor_seat = None

        # Compute the actor's deadline + original window so opponent
        # clients can render an accurate timer ring. Without these the
        # iOS ring assumed a fixed 60s window — wrong for bot turns
        # (5s preflop/flop). Source: engine.get_action_request matches
        # this calculation. Done out here so a single get_actor_seat
        # call suffices regardless of which user is the actor (or if
        # the actor is a bot subprocess that doesn't get an
        # ACTION_REQUEST).
        actor_expires_at_ms: Optional[int] = None
        actor_window_seconds: Optional[int] = None
        if actor_seat is not None and not is_hand_end:
            runner = self._manager._tables.get(table_id)
            if runner and runner._engine._status.value == "running":
                cfg = runner._config
                # Map seat → user_id to detect bot for the early-street window.
                actor_user_id = None
                for uid, sidx in runner._user_seats.items():
                    if sidx == actor_seat:
                        actor_user_id = uid
                        break
                is_bot_actor = bool(
                    actor_user_id
                    and actor_user_id.startswith(("bot_", "user_bot_"))
                )
                street_obj = runner._engine._get_current_street()
                street_name = street_obj.value if hasattr(street_obj, "value") else str(street_obj)
                is_early_street = street_name in ("preflop", "flop")
                if is_bot_actor and is_early_street:
                    actor_window_seconds = cfg.bot_early_street_timeout_seconds
                else:
                    actor_window_seconds = cfg.action_timeout_seconds
                actor_expires_at_ms = int(time.time() * 1000) + actor_window_seconds * 1000

        # Build STATE_DELTA
        delta = StateDeltaMessage(
            table_id=table_id,
            hand_id=hand_id,
            seq=seq,
            events=events,
            actor_seat=actor_seat,
            actor_expires_at_ms=actor_expires_at_ms,
            actor_window_seconds=actor_window_seconds,
        )
        delta_dict = delta.model_dump(mode="json")

        event_types = [e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", "?") for e in events]
        print(f"[BROADCAST] table={table_id} events={event_types} is_hand_end={is_hand_end}", flush=True)

        # If hand just started, check for applied top-ups and broadcast
        # REBUY messages — both the in-hand pending top-ups and any
        # rebuys queued at the prior hand's end (P0-5: queued instead
        # of broadcast mid-celebration). Order: queued first (their
        # chips visually arrive WITH the new deal), then this hand's
        # applied top-ups (typically empty unless someone tapped Top Up
        # between hands).
        if is_hand_start:
            queued = self._pending_rebuy_msgs.pop(table_id, [])
            for rebuy_msg in queued:
                await self._connections.broadcast_to_table(table_id, rebuy_msg)
                print(f"[REBUY] Sent queued (deferred from hand_end): {rebuy_msg}")
            runner = self._manager._tables.get(table_id)
            if runner:
                applied_topups = runner._engine.get_and_clear_applied_topups()
                for seat_idx, topup_amount, new_stack in applied_topups:
                    rebuy_msg = {
                        "type": "REBUY",
                        "seat": seat_idx,
                        "amount": {"amount": topup_amount},
                        "new_stack": {"amount": new_stack},
                    }
                    await self._connections.broadcast_to_table(table_id, rebuy_msg)
                    print(f"[REBUY] Sent for applied top-up: seat {seat_idx}, +{topup_amount} cents")

        # Send to all users, reusing the snapshot we already fetched in the
        # batch above instead of issuing a fresh get_snapshot per user.
        for user_id in user_ids:
            await self._connections.send_to_user(user_id, delta_dict)

            try:
                user_snapshot = snapshots_by_user.get(user_id)
                if user_snapshot is None:
                    # User wasn't in the batch result — either they left
                    # between get_table_users() and the batch, or the
                    # batch failed entirely. Skip cleanly.
                    continue

                # If hand just started, send TABLE_SNAPSHOT with hole cards
                if is_hand_start:
                    snapshot_dict = user_snapshot.model_dump(mode="json")
                    await self._connections.send_to_user(user_id, snapshot_dict)

                # Check if this user is the actor and send ACTION_REQUEST
                actor_seat = user_snapshot.hand.actor_seat if user_snapshot.hand else None
                your_seat = user_snapshot.your_seat
                if (user_snapshot.hand and
                    actor_seat is not None and
                    actor_seat == your_seat):
                    # This user needs to act - send ACTION_REQUEST
                    print(f"[ACTION_REQUEST] Sending to {user_id} (seat {your_seat})", flush=True)
                    await self._send_action_request(user_id, user_snapshot)
            except Exception as e:
                print(f"[BROADCAST] Error sending to {user_id}: {e}", flush=True)

        # Safety net: ensure the current actor has an ACTION_REQUEST and timer
        # This catches cases where the broadcast loop failed for the actor
        if not is_hand_end:
            try:
                runner = self._manager._tables.get(table_id)
                if runner and runner._engine._status.value == "running":
                    actor_seat = runner._engine.get_actor_seat()
                    if actor_seat is not None:
                        for uid, seat in runner._user_seats.items():
                            if seat == actor_seat:
                                if not self._timer or not self._timer.get_pending(uid):
                                    print(f"[SAFETY_NET] Actor {uid[:20]}... missing timer, sending ACTION_REQUEST", flush=True)
                                    snapshot = await self._manager.get_snapshot(uid)
                                    await self._send_action_request(uid, snapshot)
                                break
            except Exception as e:
                print(f"[SAFETY_NET] Error ensuring actor has timer: {e}", flush=True)

        # Check for bust players and process rebuys after hand ends
        if is_hand_end:
            # Clear all pending timers for players at this table
            if self._timer:
                for uid in user_ids:
                    self._timer.clear_deadline(uid)
                print(f"[TIMER] Cleared all deadlines for table {table_id} (hand ended)")

            # Check if this is a duel table
            runner = self._manager._tables.get(table_id)
            if runner and runner.is_duel:
                # Duel mode: check for bust (0 chips) and complete match
                duel_ended = await self._check_duel_bust(table_id)
                if not duel_ended:
                    # No one busted yet - auto-start next hand
                    asyncio.create_task(self._duel_auto_start_next_hand(table_id))
            else:
                # Regular cash game: process rebuys
                await self._check_and_process_rebuys(table_id)
                await self._topup_bots_and_broadcast(table_id)

                # Auto-start next hand only if no human players at the table
                human_users = [
                    uid for uid in user_ids if not uid.startswith("user_bot_")
                ]
                if not human_users:
                    asyncio.create_task(self._auto_start_next_hand(table_id))

    async def _auto_start_next_hand(self, table_id: str, delay: float = 3.0) -> None:
        """Auto-start the next hand after a delay."""
        print(f"[AUTO-START] Waiting {delay}s before starting hand on {table_id}")
        await asyncio.sleep(delay)

        # Check if table still has enough players
        runner = self._manager._tables.get(table_id)
        if not runner:
            print(f"[AUTO-START] Table {table_id} not found, aborting")
            return

        player_count = runner.player_count
        min_players = runner._config.min_players_to_start
        if player_count < min_players:
            print(f"[AUTO-START] Not enough players ({player_count} < {min_players}), aborting")
            return

        # Check if hand already in progress
        if runner._engine._status.value == "running":
            print(f"[AUTO-START] Hand already running, aborting")
            return

        # Start the next hand
        try:
            print(f"[AUTO-START] Starting hand on {table_id}")
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            print(f"[AUTO-START] Hand started successfully on {table_id}")
        except Exception as e:
            print(f"[AUTO-START] Error starting hand: {e}")
            pass

    async def _send_action_request(self, user_id: str, snapshot) -> None:
        """Send ACTION_REQUEST to a user who needs to act."""
        try:
            import time as _time
            _t0 = int(_time.time() * 1000)
            action_request = await self._manager.get_action_request(user_id)
            _t1 = int(_time.time() * 1000)
            print(f"[DEBUG_TIMER] now={_t1} expires_at={action_request.expires_at_ms} delta={action_request.expires_at_ms - _t1}ms (get_action took {_t1-_t0}ms)", flush=True)
            msg = action_request.model_dump(mode="json")
            await self._connections.send_to_user(user_id, msg)

            # Register deadline with timer service
            if self._timer:
                table_id = self._manager.get_table_for_user(user_id)
                # Facing bet if there's an amount to call > 0
                facing_bet = (
                    action_request.call_amount is not None
                    and action_request.call_amount.amount > 0
                )
                _t2 = int(_time.time() * 1000)
                deadline_id = self._timer.register_deadline(
                    table_id=table_id,
                    user_id=user_id,
                    hand_id=action_request.hand_id,
                    seat=action_request.seat,
                    deadline_ms=action_request.expires_at_ms,
                    facing_bet=facing_bet,
                )
                _t3 = int(_time.time() * 1000)
                delta_at_reg = action_request.expires_at_ms - _t3
                print(f"[TIMER_REG] {user_id[:25]}... deadline_id={deadline_id} expires={action_request.expires_at_ms} delta_at_reg={delta_at_reg}ms (send took {_t2-_t1}ms)", flush=True)
            else:
                print(f"[TIMER_REG] No timer service for {user_id[:20]}...", flush=True)
        except Exception as e:
            # User may no longer be the actor (race condition) - that's ok
            print(f"[TIMER_REG] Exception for {user_id[:20]}...: {e}", flush=True)
            pass

    async def _check_duel_bust(self, table_id: str) -> bool:
        """Check for bust player in duel mode. Returns True if duel ended."""
        # Import here to avoid circular dependency
        from .app import _active_duels, _complete_duel_match

        runner = self._manager._tables.get(table_id)
        if not runner:
            return False

        # Find player with 0 chips
        bust_user_id = None
        winner_user_id = None

        for seat_idx, seat_state in enumerate(runner._engine._seats):
            if seat_state is None:
                continue

            user_id = seat_state.player.user_id
            chips = seat_state.chips
            print(f"[DUEL_BUST_CHECK] seat {seat_idx} user={user_id[:20]}... chips={chips}")

            if chips <= 0:
                bust_user_id = user_id
            else:
                winner_user_id = user_id

        print(f"[DUEL_BUST_CHECK] bust={bust_user_id}, winner={winner_user_id}")
        if bust_user_id and winner_user_id:
            # Engine seat user_ids can diverge from match player_ids (bot seats use
            # an ephemeral `user_bot_tbl_XXX_N` id, while match.player2_id holds the
            # persona id like `bot_danielr`). Normalize to match-level ids so rating
            # updates score the correct player.
            match = _active_duels.get(table_id)
            if match:
                if bust_user_id == match.player1_id:
                    winner_user_id = match.player2_id
                else:
                    bust_user_id = match.player2_id
                    winner_user_id = match.player1_id

            print(f"[DUEL] Player {bust_user_id} busted, winner: {winner_user_id}", flush=True)

            # Defer the animation-wait + match-complete to a background task.
            # Previously this awaited up to 15 s inline; because _check_duel_bust
            # is called from _broadcast_events which runs inside the WS message
            # loop, the winning player's loop was blocked for the entire
            # animation timeout — no PING, no ACTIONs accepted. The task below
            # carries the same logic but lets the caller's loop continue.
            #
            # We still return True so the caller skips auto-starting the next
            # hand; the table state will be torn down by _complete_duel_match
            # in the background task.
            async def _finish_duel():
                try:
                    await self._wait_for_animation_complete(table_id, timeout=5.0)
                    await _complete_duel_match(table_id, winner_user_id)
                except Exception as e:
                    print(f"[DUEL] _finish_duel error: {e}", flush=True)

            asyncio.create_task(_finish_duel())
            return True

        return False

    async def _wait_for_animation_complete(self, table_id: str, timeout: float = 10.0) -> None:
        """Wait for client to signal ANIMATION_COMPLETE, with timeout fallback."""
        event = asyncio.Event()
        self._animation_complete_events[table_id] = event

        try:
            print(f"[ANIMATION] Waiting for client animation complete on {table_id} (timeout={timeout}s)")
            await asyncio.wait_for(event.wait(), timeout=timeout)
            print(f"[ANIMATION] Client signaled animation complete for {table_id}")
        except asyncio.TimeoutError:
            print(f"[ANIMATION] Timeout waiting for animation complete on {table_id}, proceeding anyway")
        finally:
            self._animation_complete_events.pop(table_id, None)

    async def _duel_auto_start_next_hand(self, table_id: str) -> None:
        """Auto-start next hand in a duel match after client signals ready."""
        from .app import _active_duels

        # Wait for client to signal animation complete
        await self._wait_for_animation_complete(table_id, timeout=10.0)

        # Check if duel still active
        if table_id not in _active_duels:
            return

        runner = self._manager._tables.get(table_id)
        if not runner:
            return

        # Check if hand already in progress
        if runner._engine._status.value == "running":
            return

        # Safety check: verify no one has 0 chips before starting
        # This catches edge cases where bust check missed someone
        for seat_state in runner._engine._seats:
            if seat_state is not None and seat_state.chips <= 0:
                print(f"[DUEL] Safety check: {seat_state.player.user_id[:20]}... has 0 chips, triggering bust", flush=True)
                # Re-run bust check to properly end the duel
                await self._check_duel_bust(table_id)
                return

        try:
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
        except Exception as e:
            print(f"[DUEL] Error starting next hand: {e}", flush=True)

    async def _check_and_process_rebuys(self, table_id: str) -> None:
        """Check for bust human players and attempt auto-rebuy.

        Each candidate's rebuy involves a Firestore get_user_balance +
        deduct_balance (now properly yielding the event loop via P0-1).
        Running them sequentially produced a visible per-hand-end stall
        scaling with the number of bust players. asyncio.gather over the
        list lets all rebuys share one Firestore RTT.
        """
        runner = self._manager._tables.get(table_id)
        if not runner:
            return

        rebuy_target = 100 * runner._config.big_blind_cents

        # Gather candidates up-front while the engine state is still
        # internally consistent.
        candidates: list[tuple[int, str, int]] = []  # (seat_idx, user_id, chips_before)
        for seat_idx, seat_state in enumerate(runner._engine._seats):
            if seat_state is None:
                continue
            user_id = seat_state.player.user_id
            if user_id.startswith(("bot_", "user_bot_")):
                continue
            if not seat_state.auto_topup_enabled:
                print(f"[REBUY_CHECK] seat {seat_idx} user {user_id[:20]}... skipped (auto top-up disabled)")
                continue
            print(f"[REBUY_CHECK] seat {seat_idx} user {user_id[:20]}... chips={seat_state.chips}")
            if seat_state.chips < rebuy_target:
                candidates.append((seat_idx, user_id, seat_state.chips))

        if not candidates:
            return

        # Issue all try_rebuy calls concurrently. Each runs a Firestore
        # transaction; the gather lets them overlap on the thread pool.
        results = await asyncio.gather(
            *(self._manager.try_rebuy(uid, table_id, sidx) for sidx, uid, _ in candidates),
            return_exceptions=True,
        )

        # Collect users whose rebuy failed (need balance lookup for OUT_OF_CHIPS).
        oo_candidates: list[tuple[int, str, int]] = []

        for (seat_idx, user_id, chips_before), result in zip(candidates, results):
            if isinstance(result, Exception):
                print(f"[REBUY] Exception for {user_id}: {result}", flush=True)
                continue
            if result:
                rebuy_amount, new_stack = result
                rebuy_msg = {
                    "type": "REBUY",
                    "seat": seat_idx,
                    "amount": {"amount": rebuy_amount},
                    "new_stack": {"amount": new_stack},
                }
                # Queue for the next hand_start broadcast (P0-5). Sending
                # this mid-celebration made the player's chip count
                # visibly jump while the winner banner was still showing.
                self._pending_rebuy_msgs.setdefault(table_id, []).append(rebuy_msg)
                print(f"[REBUY] Queued for next hand_start: seat {seat_idx}: +{rebuy_amount} cents")
            else:
                oo_candidates.append((seat_idx, user_id, chips_before))

        if not oo_candidates:
            return

        # Parallel balance reads for the OUT_OF_CHIPS notifications.
        balances: list = []
        if self._manager._firestore:
            balances = await asyncio.gather(
                *(self._manager._firestore.get_user_balance(uid) for _, uid, _ in oo_candidates),
                return_exceptions=True,
            )
        else:
            balances = [0] * len(oo_candidates)

        for (seat_idx, user_id, chips_before), balance in zip(oo_candidates, balances):
            if isinstance(balance, Exception):
                balance = 0
            out_msg = {
                "type": "OUT_OF_CHIPS",
                "balance_cents": balance,
                "rebuy_cost_cents": max(rebuy_target - chips_before, 0),
            }
            await self._connections.send_to_user(user_id, out_msg)
            print(f"[OUT_OF_CHIPS] Sent to {user_id}")

    async def _topup_bots_and_broadcast(self, table_id: str) -> None:
        """Top up busted bot seats to 100bb in cash mode, broadcast REBUY for each.

        Runs at hand end (cash path only) so the next can_start_hand() check
        sees the bot as active. Without this the table stalls in HU after a
        bot bust.
        """
        runner = self._manager._tables.get(table_id)
        if not runner:
            return
        topups = runner._engine.topup_bots_for_cash()
        for seat_idx, amount, new_stack in topups:
            rebuy_msg = {
                "type": "REBUY",
                "seat": seat_idx,
                "amount": {"amount": amount},
                "new_stack": {"amount": new_stack},
            }
            # Queue for the next hand_start broadcast (P0-5). Bot
            # rebuys mid-celebration made busted bots visibly refill
            # from $0 while the winner banner was on screen.
            self._pending_rebuy_msgs.setdefault(table_id, []).append(rebuy_msg)
            print(f"[BOT_REBUY] queued for next hand_start: seat {seat_idx}: +{amount} cents (new stack {new_stack})")

    async def handle_timeout(self, pending: "PendingAction") -> None:
        """Handle action timeout - apply auto-action."""
        try:
            # Note: The pending action has already been removed from the timer
            # by the time this callback runs, so we use the passed-in pending object
            print(f"[TIMEOUT] Processing timeout for {pending.user_id[:20]}... seat={pending.seat} facing_bet={pending.facing_bet}", flush=True)

            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()

            runner = self._manager._tables.get(pending.table_id)
            if not runner:
                return

            await runner.submit(TimeoutActionCommand(
                user_id=pending.user_id,
                hand_id=pending.hand_id,
                seat=pending.seat,
                facing_bet=pending.facing_bet,
                result_future=future,
            ))

            events = await future
            if events:
                await self._broadcast_events(pending.table_id, pending.hand_id, events)
        except Exception as e:
            # Log but don't crash on timeout handling errors
            print(f"[TIMEOUT] Error handling timeout for {pending.user_id}: {e}", flush=True)

    async def handle_quip(
        self,
        user_id: str,
        hand_id: str,
        seat: int,
        text: str,
    ) -> None:
        """Handle QUIP message from bot. Broadcasts quip to all players at table."""
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            print(f"[QUIP] Ignored - user {user_id} not at any table")
            return

        # Build quip message to broadcast
        quip_msg = {
            "type": "QUIP",
            "hand_id": hand_id,
            "seat": seat,
            "text": text,
        }

        # Broadcast to all players at the table
        await self._connections.broadcast_to_table(table_id, quip_msg)
        print(f"[QUIP] Broadcast to table {table_id}: seat {seat} says \"{text}\"", flush=True)

    def _map_error(self, error_msg: str) -> ErrorCode:
        """Map error message to ErrorCode."""
        msg_lower = error_msg.lower()
        if "insufficient_balance" in msg_lower:
            return ErrorCode.INSUFFICIENT_BALANCE
        if "turn" in msg_lower:
            return ErrorCode.NOT_YOUR_TURN
        if "invalid" in msg_lower:
            return ErrorCode.INVALID_ACTION
        if "already at" in msg_lower:
            return ErrorCode.ALREADY_AT_TABLE
        if "not at" in msg_lower:
            return ErrorCode.NOT_AT_TABLE
        return ErrorCode.BAD_REQUEST
