"""Data models for preflop grading."""

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class Grade(Enum):
    """Grade for a preflop decision."""
    MISTAKE = "mistake"
    GOOD = "good"
    # BRILLIANT = "brilliant"  # Future


@dataclass
class GradedDecision:
    """A graded preflop decision."""
    hand_id: str
    street: str  # "preflop"
    action_taken: str  # canonical category used for GTO lookup: "Raise", "Call", "Fold"
    gto_frequency: float  # GTO frequency for the action taken (0-1)
    confidence: float  # Confidence in the grade (0-1)
    grade: Grade
    reasoning: str  # Human-readable explanation

    # Context for debugging
    position: Optional[str] = None  # "UTG", "BTN", etc.
    hand: Optional[str] = None  # "AhAs", "7h2c", etc.
    spot_path: Optional[str] = None  # "BTN_RFI/SB_3B" etc.
    # Display-friendly label that distinguishes Open / 3bet / 4bet / Limp /
    # Call-vs-Open / Call-vs-3bet / etc. action_taken stays canonical for logic.
    action_label: Optional[str] = None
