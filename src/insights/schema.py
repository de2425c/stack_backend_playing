"""Schema for AI poker insight requests and responses."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StreetAction:
    """Action record for a single street."""
    street: str          # "preflop", "flop", "turn", "river"
    cards: str           # "" for preflop, "Ac-5c-4d" for flop, "8h" for turn/river
    actions: str         # "BTN opens 2.5bb, BB 3-bets to 13bb, BTN calls"


@dataclass
class HeroDecision:
    """A decision point where hero acted."""
    street: str                 # "flop", "turn", etc.
    action_taken: str           # "calls", "raises to 15bb", "folds"
    pot_before_bb: float        # Pot size before this action (excludes hero's own chips this action)
    facing: str                 # "a 3bb bet", "a check", "first to act"
    position_vs_villain: str    # "in position" or "out of position"
    # Pot odds context (populated when hero faces a bet/raise)
    to_call_bb: float = 0.0           # How much hero must add to call
    pot_odds: str = ""                # e.g. "2.0:1" (pot offered : call), "" if not facing a bet
    required_equity_pct: float = 0.0  # Equity needed to break even on a call
    villain_all_in: bool = False      # True if the bet hero is facing is a shove


@dataclass
class HandInsightRequest:
    """Input for generating insights about a complete hand."""

    # Hero info
    hero_position: str          # "BB", "BTN", etc.
    hero_hand: str              # "9s9c"

    # Hand context
    num_players: int            # Number of players dealt in
    pot_type: str               # "single raised", "3-bet", "limped"

    # Board
    board: str                  # "Ac 5c 4d 8h 2s" (full board)

    # Complete action history per street
    street_actions: list[StreetAction]

    # Hero's decisions in the hand
    hero_decisions: list[HeroDecision]

    # Result
    hero_won: bool
    profit_bb: float            # Hero's profit/loss in BB

    # Optional metadata
    final_pot_bb: float = 0.0
    went_to_showdown: bool = False


@dataclass
class InsightRequest:
    """Input for generating poker insights. Maps directly to spot_candidates."""

    # Decision point
    hero_hand: str              # "9s9c"
    board: str                  # "Ac5c4d8h"
    hero_position: str          # "BB"
    villain_position: str       # "BTN"
    street: str                 # "turn"

    # Action history
    street_actions: list[StreetAction]  # Full history per street
    action_sequence: str        # "BB checks → BTN bets 14.2bb → BB to act"

    # Solver data
    available_actions: list[str]        # ["Fold", "Call", "All-in"]
    action_frequencies: dict[str, float]  # {"Fold": 0.07, "Call": 0.93, "All-in": 0.0}
    ev_by_action: dict[str, float]        # {"Fold": 0.0, "Call": -0.09, "All-in": -8.66}
    optimal_action: str                   # "Call"

    # Context (optional)
    pot_size_bb: float = 0.0
    stack_size_bb: float = 0.0
    hand_category: str = ""     # "underpair", "oesd", "gutshot"
    board_texture: str = ""     # "dry", "wet"


@dataclass
class InsightResponse:
    """Output from the insight generator."""

    insight: str  # The 2-3 sentence insight
    model_used: str  # e.g., "claude-3-5-sonnet-20241022"
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Optional structured data extracted from insight
    key_concepts: list[str] = field(default_factory=list)  # ["blocker", "position"]
    terms: dict[str, str] = field(default_factory=dict)  # {"term-id": "text used in insight"}
