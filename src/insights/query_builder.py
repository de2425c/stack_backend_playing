"""Build semantic queries and metadata filters from HandInsightRequest."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import HandInsightRequest, InsightRequest


@dataclass
class JandaQuery:
    """Query and filters for Janda corpus search."""
    query: str
    filters: dict[str, list[str]]


def build_janda_query(
    hand: HandInsightRequest | InsightRequest,
    is_hu: bool = False,
) -> JandaQuery:
    """
    Convert hand context to semantic query + metadata filters.

    Args:
        hand: HandInsightRequest or InsightRequest with hand context.
        is_hu: When True, bias the query toward heads-up theory (BTN vs BB,
            wide ranges, push/fold at short stacks) so the Janda retrieval
            doesn't drown the HU spot in 6-max passages.
    """
    query_parts = []
    filters: dict[str, list[str]] = {}

    # Determine street
    if isinstance(hand, InsightRequest):
        street = hand.street
        board = hand.board
        pot_type = "single raised"  # Default, try to extract from context
        hero_position = hand.hero_position
        villain_position = hand.villain_position
        action_sequence = hand.action_sequence
    else:
        # HandInsightRequest - find the most relevant street from decisions
        if hand.hero_decisions:
            street = hand.hero_decisions[-1].street
        else:
            street = "preflop"
        board = hand.board
        pot_type = hand.pot_type
        hero_position = hand.hero_position
        villain_position = ""
        action_sequence = ""

    # Add street to filters (include preflop too for proper filtering)
    if street:
        filters["streets"] = [street]
        if street == "preflop":
            query_parts.append("preflop strategy opening ranges 3-betting 4-betting")
        else:
            query_parts.append(f"{street} play")

    # Analyze board texture if postflop
    if board and street in ("flop", "turn", "river"):
        board_info = analyze_board_for_query(board, street)
        if board_info.get("board_textures"):
            filters["board_textures"] = board_info["board_textures"]
            query_parts.extend(board_info["board_textures"])

    # Add pot type
    pot_type_normalized = _normalize_pot_type(pot_type)
    if pot_type_normalized:
        filters["pot_types"] = [pot_type_normalized]
        query_parts.append(f"{pot_type_normalized} pot")

    # Add position context
    position_tag = _determine_position_tag(hero_position, villain_position)
    if position_tag:
        filters["positions"] = [position_tag]
        query_parts.append(f"playing {position_tag}")

    # Add action context from action_sequence
    action_context = _extract_action_context(action_sequence)
    if action_context:
        query_parts.append(action_context)

    # HU bias: prepend HU-specific keywords so the embedding lands closer to
    # heads-up chapters instead of generic 6-max ones. We do NOT add a metadata
    # filter here because the Janda corpus doesn't tag HU explicitly — the
    # keywords steer the semantic search instead.
    if is_hu:
        query_parts = [
            "heads-up HU BTN vs BB wide-range play",
        ] + query_parts

    # Build final query
    query = " ".join(query_parts) if query_parts else "poker strategy"

    return JandaQuery(query=query, filters=filters)


def analyze_board_for_query(board: str, street: str) -> dict[str, list[str]]:
    """
    Extract board texture tags matching Janda metadata schema.

    Args:
        board: Board string, e.g., "Kc 7d 2s" or "Kc7d2s8h"
        street: Current street (affects which cards to analyze)

    Returns:
        {"board_textures": ["dry", "rainbow", "disconnected"]}
    """
    # Parse board cards
    cards = _parse_board_cards(board)
    if not cards:
        return {}

    # Get cards for current street
    if street == "flop":
        cards = cards[:3]
    elif street == "turn":
        cards = cards[:4]
    else:  # river
        cards = cards[:5]

    if len(cards) < 3:
        return {}

    textures = []

    # Analyze flop texture (first 3 cards)
    flop_cards = cards[:3]
    ranks = [c[:-1] for c in flop_cards]
    suits = [c[-1] for c in flop_cards]

    # Flush texture
    unique_suits = len(set(suits))
    if unique_suits == 1:
        textures.append("monotone")
    elif unique_suits == 2:
        textures.append("two_tone")
    elif unique_suits == 3:
        textures.append("rainbow")

    # Connectedness
    rank_values = [_rank_to_value(r) for r in ranks]
    rank_values_sorted = sorted(rank_values)
    gaps = [rank_values_sorted[i+1] - rank_values_sorted[i] for i in range(len(rank_values_sorted)-1)]
    total_gap = sum(gaps)

    if total_gap <= 4:  # Within 4 ranks = connected
        textures.append("connected")
    elif total_gap >= 8:  # Spread out
        textures.append("disconnected")

    # Paired board
    if len(set(ranks)) < 3:
        textures.append("paired")

    # Dry vs wet (combination of factors)
    is_dry = (unique_suits == 3 and total_gap >= 6 and len(set(ranks)) == 3)
    is_wet = (unique_suits <= 2 or total_gap <= 4)

    if is_dry:
        textures.append("dry")
    elif is_wet:
        textures.append("wet")

    # High/low board
    high_cards = sum(1 for v in rank_values if v >= 10)  # T, J, Q, K, A
    if high_cards >= 2:
        textures.append("high")
    elif all(v <= 8 for v in rank_values):
        textures.append("low")

    return {"board_textures": textures}


def _parse_board_cards(board: str) -> list[str]:
    """Parse board string into list of cards."""
    # Handle various formats: "Kc 7d 2s", "Kc7d2s", "Kc-7d-2s"
    board = board.strip()
    if not board:
        return []

    # Try space-separated
    if " " in board:
        return board.split()

    # Try dash-separated
    if "-" in board:
        return board.split("-")

    # Try to parse concatenated (Kc7d2s)
    cards = []
    pattern = r"([2-9TJQKA][cdhs])"
    matches = re.findall(pattern, board, re.IGNORECASE)
    return [m.upper().replace("10", "T") for m in matches]


def _rank_to_value(rank: str) -> int:
    """Convert card rank to numeric value."""
    rank_map = {
        "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "T": 10, "10": 10, "J": 11, "Q": 12, "K": 13, "A": 14
    }
    return rank_map.get(rank.upper(), 0)


def _normalize_pot_type(pot_type: str) -> str:
    """Normalize pot type to match Janda metadata schema."""
    pot_type = pot_type.lower().strip()

    if "3-bet" in pot_type or "3bet" in pot_type or "three" in pot_type:
        return "3-bet"
    elif "4-bet" in pot_type or "4bet" in pot_type or "four" in pot_type:
        return "4-bet"
    elif "limp" in pot_type:
        return "limped"
    elif "single" in pot_type or "raised" in pot_type or "srp" in pot_type:
        return "single_raised"
    elif "multiway" in pot_type:
        return "multiway"

    return "single_raised"  # Default


def _determine_position_tag(hero_pos: str, villain_pos: str) -> str:
    """
    Determine IP/OOP tag based on positions.

    Position order: BTN > CO > HJ > UTG > BB > SB
    """
    if not hero_pos:
        return ""

    position_order = {"BTN": 6, "CO": 5, "HJ": 4, "UTG": 3, "BB": 2, "SB": 1}
    hero_rank = position_order.get(hero_pos.upper(), 0)
    villain_rank = position_order.get(villain_pos.upper(), 0) if villain_pos else 0

    if hero_rank > villain_rank:
        return "IP"
    elif villain_rank > hero_rank:
        return "OOP"

    return ""


def _extract_action_context(action_sequence: str) -> str:
    """Extract action context keywords from action sequence."""
    if not action_sequence:
        return ""

    action_lower = action_sequence.lower()

    context_parts = []

    # First to act
    if "first to act" in action_lower:
        context_parts.append("first to act")

    # Facing a bet
    if "bets" in action_lower or "raises" in action_lower:
        if "to act" in action_lower:
            context_parts.append("facing bet")

    # Check-raise opportunity
    if "checks" in action_lower and "to act" in action_lower:
        context_parts.append("after check")

    # C-bet context
    if any(x in action_lower for x in ["continuation", "c-bet", "cbet"]):
        context_parts.append("c-betting")

    # Donk bet
    if "donk" in action_lower:
        context_parts.append("donk bet")

    return " ".join(context_parts)


def build_query_for_decision(
    street: str,
    board: str,
    pot_type: str,
    hero_position: str,
    villain_position: str,
    facing: str,
    hero_hand_category: str = ""
) -> JandaQuery:
    """
    Build query from individual decision components.

    Useful when you have specific context but not a full HandInsightRequest.

    Args:
        street: Current street
        board: Board cards
        pot_type: Pot type
        hero_position: Hero's position
        villain_position: Villain's position
        facing: What hero is facing ("a bet", "first to act", etc.)
        hero_hand_category: Optional hand category ("overpair", "flush_draw", etc.)

    Returns:
        JandaQuery with semantic query and filters
    """
    query_parts = []
    filters: dict[str, list[str]] = {}

    # Street (include preflop for proper filtering)
    if street:
        filters["streets"] = [street]
        query_parts.append(f"{street} strategy")

    # Board texture
    if board and street in ("flop", "turn", "river"):
        board_info = analyze_board_for_query(board, street)
        if board_info.get("board_textures"):
            filters["board_textures"] = board_info["board_textures"]
            query_parts.extend(board_info["board_textures"][:2])  # Top 2 textures

    # Pot type
    pot_type_normalized = _normalize_pot_type(pot_type)
    if pot_type_normalized:
        filters["pot_types"] = [pot_type_normalized]
        query_parts.append(pot_type_normalized.replace("_", " ") + " pot")

    # Position
    position_tag = _determine_position_tag(hero_position, villain_position)
    if position_tag:
        filters["positions"] = [position_tag]
        query_parts.append(position_tag)

    # Facing context
    facing_lower = facing.lower() if facing else ""
    if "bet" in facing_lower:
        query_parts.append("facing bet")
    elif "raise" in facing_lower:
        query_parts.append("facing raise")
    elif "check" in facing_lower or "first" in facing_lower:
        query_parts.append("betting opportunity")

    # Hand category
    if hero_hand_category:
        query_parts.append(hero_hand_category.replace("_", " "))

    query = " ".join(query_parts) if query_parts else "poker strategy"

    return JandaQuery(query=query, filters=filters)
