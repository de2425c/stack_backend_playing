"""
Command dataclasses for table operations.

Commands are submitted to TableRunner queues and processed serially.
Each command includes a Future for async result delivery.
"""

from dataclasses import dataclass
from typing import Optional, Union
import asyncio

from ..models import PlayerIdentity, Chips, ClientAction


@dataclass
class JoinTableCommand:
    """Request to join a table."""
    user_id: str
    player: PlayerIdentity
    buy_in: Chips
    result_future: asyncio.Future  # Resolves to (seat, snapshot) or raises


@dataclass
class LeaveTableCommand:
    """Request to leave a table."""
    user_id: str
    result_future: asyncio.Future  # Resolves to final_chips or raises


@dataclass
class PlayerActionCommand:
    """Player game action (fold, check, call, bet, raise)."""
    user_id: str
    hand_id: str  # Track which hand this action is for (stale action detection)
    action: ClientAction
    amount: Optional[Chips]
    result_future: asyncio.Future  # Resolves to list[events] or raises
    decision_metadata: Optional[dict] = None  # Bot decision context


@dataclass
class StartHandCommand:
    """Request to start a new hand."""
    result_future: asyncio.Future  # Resolves to list[events] or raises


@dataclass
class GetSnapshotCommand:
    """Request table snapshot for a user."""
    user_id: str
    result_future: asyncio.Future  # Resolves to TableSnapshotMessage or raises


@dataclass
class GetSnapshotsBatchCommand:
    """Request table snapshots for many users in one queue round-trip.

    Avoids the N+1 pattern in handler._broadcast_events where one snapshot
    per user was issued as a separate command. The runner serialises
    commands, so calling get_snapshot in a loop produced N round-trips
    through the queue instead of one — directly translating to N times
    the broadcast latency at a 6-max table.
    """
    user_ids: list[str]
    # Resolves to dict[user_id -> TableSnapshotMessage]. Users not seated
    # at this table at command time are silently dropped from the dict.
    result_future: asyncio.Future


@dataclass
class GetActionRequestCommand:
    """Request action request for a user who needs to act."""
    user_id: str
    result_future: asyncio.Future  # Resolves to ActionRequestMessage or raises


@dataclass
class TimeoutActionCommand:
    """Server-initiated timeout action (auto-fold/auto-check)."""
    user_id: str
    hand_id: str
    seat: int
    facing_bet: bool  # True = fold, False = check
    result_future: asyncio.Future  # Resolves to list[events]


# Union type for all commands
TableCommand = Union[
    JoinTableCommand,
    LeaveTableCommand,
    PlayerActionCommand,
    StartHandCommand,
    GetSnapshotCommand,
    GetSnapshotsBatchCommand,
    GetActionRequestCommand,
    TimeoutActionCommand,
]
