"""
Data models for hand logging and chip ledger.

These models are designed for Firestore storage and replay verification.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class LedgerReason(str, Enum):
    """Reason for chip movement in ledger."""
    BLIND = "blind"
    BET = "bet"           # Includes calls and raises
    WIN = "win"
    BUYIN = "buyin"
    CASHOUT = "cashout"


@dataclass
class SeatRecord:
    """Snapshot of a seat at hand start."""
    seat_index: int
    user_id: str
    display_name: str
    starting_stack: int  # cents

    def to_dict(self) -> dict:
        return asdict(self)


def _stringify_keys(obj):
    """Recursively convert dict keys to strings for Firestore compatibility."""
    if isinstance(obj, dict):
        return {str(k): _stringify_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_stringify_keys(item) for item in obj]
    elif hasattr(obj, 'item'):  # numpy scalar
        return obj.item()
    else:
        return obj


@dataclass
class ActionRecord:
    """Single action in a hand."""
    seat: int
    action: str  # fold, check, call, bet, raise_to, post_blind
    amount: Optional[int]  # cents, None for fold/check
    is_all_in: bool
    street: str = "preflop"  # preflop, flop, turn, river
    timestamp: Optional[datetime] = None
    decision_metadata: Optional[dict] = None  # Bot decision context (solver data, ranges, etc.)

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["timestamp"]:
            d["timestamp"] = d["timestamp"].isoformat()
        # Convert decision_metadata keys to strings for Firestore
        if d["decision_metadata"]:
            d["decision_metadata"] = _stringify_keys(d["decision_metadata"])
        return d


@dataclass
class WinnerRecord:
    """Winner of a pot."""
    seat: int
    user_id: str
    amount_won: int  # cents
    hand_description: Optional[str] = None  # e.g., "Two Pair, Aces and Kings"
    shown_cards: Optional[list[str]] = None  # e.g., ["Ah", "Ks"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HandLog:
    """Complete hand history for replay and audit."""
    hand_id: str
    table_id: str
    stake_id: str  # e.g., "nlh_1_2"

    # Timestamps
    started_at: datetime
    ended_at: datetime

    # Participants (snapshot at hand start)
    seats: list[SeatRecord]
    button_seat: int
    small_blind: int  # cents
    big_blind: int    # cents

    # Action sequence (ordered)
    actions: list[ActionRecord]

    # Hole cards per seat: {seat_index: ["Ah", "Ks"]}
    hole_cards: dict[int, list[str]] = field(default_factory=dict)

    # Board runout
    board: list[str] = field(default_factory=list)  # ["Ah", "Ks", "Qd", "Jc", "2h"]

    # Showdown / result
    winners: list[WinnerRecord] = field(default_factory=list)

    # Final deltas (computed) - seat_index -> delta in cents (signed)
    stack_deltas: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dict for Firestore storage."""
        return {
            "hand_id": self.hand_id,
            "table_id": self.table_id,
            "stake_id": self.stake_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "seats": [s.to_dict() for s in self.seats],
            "button_seat": self.button_seat,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "actions": [a.to_dict() for a in self.actions],
            "hole_cards": {str(k): v for k, v in self.hole_cards.items()},
            "board": self.board,
            "winners": [w.to_dict() for w in self.winners],
            "stack_deltas": {str(k): v for k, v in self.stack_deltas.items()},
            # Flat array of user IDs for Firestore array-contains queries
            "participant_ids": [s.user_id for s in self.seats],
        }


@dataclass
class LedgerEntry:
    """Individual chip movement for accounting."""
    entry_id: str
    user_id: str
    delta: int  # Signed cents (+win, -loss)
    reason: LedgerReason
    hand_id: Optional[str]  # Null for BUYIN/CASHOUT
    table_id: str
    created_at: datetime

    @staticmethod
    def create(
        user_id: str,
        delta: int,
        reason: LedgerReason,
        table_id: str,
        hand_id: Optional[str] = None,
    ) -> "LedgerEntry":
        """Factory method to create a new ledger entry."""
        return LedgerEntry(
            entry_id=str(uuid.uuid4()),
            user_id=user_id,
            delta=delta,
            reason=reason,
            hand_id=hand_id,
            table_id=table_id,
            created_at=datetime.utcnow(),
        )

    def to_dict(self) -> dict:
        """Convert to dict for Firestore storage."""
        return {
            "entry_id": self.entry_id,
            "user_id": self.user_id,
            "delta": self.delta,
            "reason": self.reason.value,
            "hand_id": self.hand_id,
            "table_id": self.table_id,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class DuelRecord:
    """Record of a completed heads-up duel match.

    For cross-stake matches the two players paid different fees and were each
    told they were matched at their own stake. `entry_fee_cents` and
    `prize_pool_cents` are kept for back-compat with existing queries — they
    reflect player1's perspective and the winner's payout. The per-player
    fees and `house_pnl_cents` capture the cross-stake delta.
    """
    match_id: str
    table_id: str
    entry_fee_cents: int  # legacy: = player1_entry_fee_cents
    stack_type: str  # "50bb" or "15bb"
    prize_pool_cents: int  # = winner_payout_cents (2x winner's own fee)

    # Players
    player1_id: str
    player1_display_name: str
    player1_is_bot: bool
    player2_id: str
    player2_display_name: str
    player2_is_bot: bool

    # Result
    winner_id: str
    winner_display_name: str
    hands_played: int

    # Timestamps
    started_at: datetime
    ended_at: datetime

    # Per-player fees (cross-stake matchmaking). Default to entry_fee_cents
    # so legacy callers that don't pass these still serialize coherently.
    player1_entry_fee_cents: Optional[int] = None
    player2_entry_fee_cents: Optional[int] = None
    # House chip P&L for this match. Positive = house gained chips,
    # negative = house paid out chips. Always 0 for same-tier or bot matches.
    house_pnl_cents: int = 0

    def to_dict(self) -> dict:
        """Convert to dict for Firestore storage."""
        p1_fee = self.player1_entry_fee_cents if self.player1_entry_fee_cents is not None else self.entry_fee_cents
        p2_fee = self.player2_entry_fee_cents if self.player2_entry_fee_cents is not None else self.entry_fee_cents
        return {
            "match_id": self.match_id,
            "table_id": self.table_id,
            "entry_fee_cents": self.entry_fee_cents,
            "stack_type": self.stack_type,
            "prize_pool_cents": self.prize_pool_cents,
            "player1_id": self.player1_id,
            "player1_display_name": self.player1_display_name,
            "player1_is_bot": self.player1_is_bot,
            "player2_id": self.player2_id,
            "player2_display_name": self.player2_display_name,
            "player2_is_bot": self.player2_is_bot,
            "winner_id": self.winner_id,
            "winner_display_name": self.winner_display_name,
            "hands_played": self.hands_played,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            # Cross-stake fields
            "player1_entry_fee_cents": p1_fee,
            "player2_entry_fee_cents": p2_fee,
            "house_pnl_cents": self.house_pnl_cents,
            "is_cross_stake": p1_fee != p2_fee,
            # Flat array for querying
            "participant_ids": [self.player1_id, self.player2_id],
        }


@dataclass
class DuelRating:
    """Glicko rating for duel mode."""
    user_id: str
    rating: float = 1000.0       # Glicko rating (keep in sync with server.glicko.INITIAL_RATING)
    rd: float = 350.0            # Rating deviation (uncertainty)
    wins: int = 0
    losses: int = 0
    last_played: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "rating": self.rating,
            "rd": self.rd,
            "wins": self.wins,
            "losses": self.losses,
            "last_played": self.last_played.isoformat() if self.last_played else None,
        }
