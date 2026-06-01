"""
Session tracking for post-session analysis.

Tracks active player sessions, accumulates hand_ids,
and triggers analysis when sessions end.
"""

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
import uuid


@dataclass
class ActiveSession:
    """Tracks an active player session."""
    session_id: str
    user_id: str
    table_id: str
    stake_id: str
    seat: int
    buy_in_cents: int
    display_name: str
    started_at: datetime
    hand_ids: list[str] = field(default_factory=list)
    total_rebuys_cents: int = 0
    rebuy_count: int = 0
    # If the iOS client supplied a session UUID up-front, the backend treats it
    # as the canonical id and uses it as the doc id under bot_sessions. Empty
    # string means no client UUID was supplied (legacy / non-bot path).
    client_session_id: str = ""
    is_pro: bool = False


@dataclass
class CompletedSession:
    """Data for a completed session, ready for analysis."""
    session_id: str
    user_id: str
    table_id: str
    stake_id: str
    display_name: str
    started_at: datetime
    ended_at: datetime
    hand_ids: list[str]
    buy_in_cents: int
    total_rebuys_cents: int
    rebuy_count: int
    final_chips_cents: int
    client_session_id: str = ""
    is_pro: bool = False

    @property
    def duration_seconds(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())

    @property
    def profit_cents(self) -> int:
        total_invested = self.buy_in_cents + self.total_rebuys_cents
        return self.final_chips_cents - total_invested

    @property
    def hands_played(self) -> int:
        return len(self.hand_ids)


# Type alias for the session completion callback
SessionCallback = Callable[[CompletedSession], Awaitable[None]]


class SessionTracker:
    """
    Tracks active player sessions and triggers analysis on completion.

    Usage:
        tracker = SessionTracker(on_session_end=process_session)

        # When player joins
        tracker.start_session(user_id, table_id, stake_id, seat, buy_in, display_name)

        # After each hand ends (call for each participant)
        tracker.add_hand(user_id, hand_id)

        # When player leaves
        session = tracker.end_session(user_id, final_chips)
        # -> triggers on_session_end callback
    """

    def __init__(self, on_session_end: Optional[SessionCallback] = None):
        self._sessions: dict[str, ActiveSession] = {}  # user_id -> session
        self._on_session_end = on_session_end

    def start_session(
        self,
        user_id: str,
        table_id: str,
        stake_id: str,
        seat: int,
        buy_in_cents: int,
        display_name: str,
        client_session_id: Optional[str] = None,
        is_pro: bool = False,
    ) -> str:
        """Start tracking a new session. Returns session_id.

        If `client_session_id` is provided (the iOS-generated UUID used as the
        bot_sessions doc id), it becomes the canonical session_id. Otherwise a
        backend `sess_xxx` id is generated for the legacy `sessions` collection.
        """
        # Skip bots
        if user_id.startswith(("bot_", "user_bot_")):
            return ""

        client_uuid = (client_session_id or "").strip()
        session_id = client_uuid if client_uuid else f"sess_{uuid.uuid4().hex[:12]}"

        self._sessions[user_id] = ActiveSession(
            session_id=session_id,
            user_id=user_id,
            table_id=table_id,
            stake_id=stake_id,
            seat=seat,
            buy_in_cents=buy_in_cents,
            display_name=display_name,
            started_at=datetime.now(timezone.utc),
            client_session_id=client_uuid,
            is_pro=is_pro,
        )

        print(f"[SESSION] Started {session_id} for {user_id[:20]}... at table {table_id}"
              + (" (client uuid)" if client_uuid else ""))
        return session_id

    def add_hand(self, user_id: str, hand_id: str) -> None:
        """Add a completed hand to the user's active session."""
        session = self._sessions.get(user_id)
        if session and hand_id not in session.hand_ids:
            session.hand_ids.append(hand_id)
            print(f"[SESSION] Added hand {hand_id} to {session.session_id} (total: {len(session.hand_ids)})")

    def add_hands_for_participants(self, participant_user_ids: list[str], hand_id: str) -> None:
        """Add a completed hand to all participants' sessions."""
        for user_id in participant_user_ids:
            self.add_hand(user_id, hand_id)

    def record_rebuy(self, user_id: str, amount_cents: int) -> None:
        """Record a rebuy for the user's session."""
        session = self._sessions.get(user_id)
        if session:
            session.total_rebuys_cents += amount_cents
            session.rebuy_count += 1
            print(f"[SESSION] Rebuy {amount_cents} cents for {session.session_id}")

    def end_session(self, user_id: str, final_chips_cents: int) -> Optional[CompletedSession]:
        """
        End a session and trigger analysis.

        Returns CompletedSession or None if user had no active session.
        """
        session = self._sessions.pop(user_id, None)
        if session is None:
            return None

        completed = CompletedSession(
            session_id=session.session_id,
            user_id=session.user_id,
            table_id=session.table_id,
            stake_id=session.stake_id,
            display_name=session.display_name,
            started_at=session.started_at,
            ended_at=datetime.now(timezone.utc),
            hand_ids=session.hand_ids,
            buy_in_cents=session.buy_in_cents,
            total_rebuys_cents=session.total_rebuys_cents,
            rebuy_count=session.rebuy_count,
            final_chips_cents=final_chips_cents,
            client_session_id=session.client_session_id,
            is_pro=session.is_pro,
        )

        print(f"[SESSION] Ended {completed.session_id}: {completed.hands_played} hands, "
              f"profit={completed.profit_cents} cents, duration={completed.duration_seconds}s")

        # Trigger async processing
        if self._on_session_end and completed.hands_played > 0:
            asyncio.create_task(self._trigger_callback(completed))

        return completed

    async def _trigger_callback(self, session: CompletedSession) -> None:
        """Trigger the session end callback."""
        try:
            await self._on_session_end(session)
        except Exception as e:
            print(f"[SESSION] Error in session callback: {e}")

    def get_active_session(self, user_id: str) -> Optional[ActiveSession]:
        """Get the active session for a user, if any."""
        return self._sessions.get(user_id)

    def get_active_session_count(self) -> int:
        """Get count of active sessions."""
        return len(self._sessions)
