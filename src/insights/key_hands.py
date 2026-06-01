"""
Key Hands Algorithm - identifies the most interesting/educational hands from a session.

Scoring criteria:
1. Hero participation - must have acted beyond posting blinds
2. Street reached - Turn/River > Flop > Preflop
3. Pot size - Larger pots = more impactful decisions
4. Board texture - Wet/interesting boards score higher
5. Decision complexity - Mixed strategy spots more educational
"""

from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ScoredHand:
    """A hand with its educational interest score."""
    hand_id: str
    score: float
    hero_position: str
    hero_hand: str
    board: str
    profit_bb: float
    max_street: str
    pot_type: str
    breakdown: dict  # Individual score components


def get_hero_seat(hand_data: dict, hero_user_id: str) -> Optional[int]:
    """Find hero's seat index in the hand."""
    seats = hand_data.get("seats", [])
    for seat in seats:
        if seat.get("user_id") == hero_user_id:
            return seat.get("seat_index")
    return None


def get_position_name(seat_index: int, button_seat: int, num_seats: int) -> str:
    """Get position name from seat index."""
    relative_pos = (seat_index - button_seat) % num_seats
    if num_seats == 2:
        positions = ["BTN", "BB"]
    elif num_seats <= 6:
        positions = ["BTN", "SB", "BB", "UTG", "HJ", "CO"][:num_seats]
    else:
        positions = ["BTN", "SB", "BB", "UTG", "UTG1", "LJ", "HJ", "CO"][:num_seats]

    return positions[relative_pos] if relative_pos < len(positions) else f"P{relative_pos}"


def analyze_board_texture(board_cards: list[str]) -> dict:
    """
    Analyze board texture for scoring.

    Returns dict with:
        - suits: 'monotone', 'two_tone', 'rainbow'
        - connectedness: 'connected', 'semi_connected', 'disconnected'
        - paired: bool
        - high_card: str
    """
    if not board_cards or len(board_cards) < 3:
        return {"suits": "unknown", "connectedness": "unknown", "paired": False, "high_card": "?"}

    # Parse cards
    ranks = []
    suits = []
    rank_order = "23456789TJQKA"

    for card in board_cards[:5]:  # Max 5 cards
        if len(card) >= 2:
            ranks.append(card[0].upper())
            suits.append(card[1].lower())

    # Suit analysis
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit_count = max(suit_counts.values()) if suit_counts else 0

    if max_suit_count >= 3:
        suit_texture = "monotone"
    elif max_suit_count == 2:
        suit_texture = "two_tone"
    else:
        suit_texture = "rainbow"

    # Connectedness - check for straights/draws
    rank_values = []
    for r in ranks:
        if r in rank_order:
            rank_values.append(rank_order.index(r))
    rank_values.sort()

    # Check gaps between consecutive cards
    if len(rank_values) >= 3:
        gaps = [rank_values[i+1] - rank_values[i] for i in range(len(rank_values)-1)]
        min_gap = min(gaps)
        max_gap = max(gaps)

        if max_gap <= 2:
            connectedness = "connected"
        elif min_gap <= 2:
            connectedness = "semi_connected"
        else:
            connectedness = "disconnected"
    else:
        connectedness = "unknown"

    # Paired board
    paired = len(ranks) != len(set(ranks))

    # High card
    high_card = max(ranks, key=lambda r: rank_order.index(r) if r in rank_order else -1)

    return {
        "suits": suit_texture,
        "connectedness": connectedness,
        "paired": paired,
        "high_card": high_card,
    }


def get_pot_type(actions: list[dict], hero_seat: int, big_blind: int) -> str:
    """Determine pot type from preflop action."""
    raises = 0

    for action in actions:
        if action.get("street") != "preflop":
            break
        act = action.get("action", "").lower()
        if act in ("bet", "raise", "raise_to"):
            raises += 1

    if raises == 0:
        return "limped"
    elif raises == 1:
        return "single_raised"
    elif raises == 2:
        return "3bet"
    elif raises == 3:
        return "4bet"
    else:
        return f"{raises}bet"


def score_hand(hand_data: dict, hero_user_id: str) -> Optional[ScoredHand]:
    """
    Score a hand for educational value.

    Returns ScoredHand with score, or None if hand should be excluded.
    """
    hand_id = hand_data.get("hand_id", "unknown")
    actions = hand_data.get("actions", [])
    seats = hand_data.get("seats", [])
    hole_cards = hand_data.get("hole_cards", {})
    board = hand_data.get("board", [])
    big_blind = hand_data.get("big_blind", 200)
    button_seat = hand_data.get("button_seat", 0)
    stack_deltas = hand_data.get("stack_deltas", {})

    # Find hero
    hero_seat = get_hero_seat(hand_data, hero_user_id)
    if hero_seat is None:
        return None

    num_seats = len(seats)
    hero_position = get_position_name(hero_seat, button_seat, num_seats)

    # Get hero's hole cards
    hero_hand_cards = hole_cards.get(str(hero_seat), [])
    if isinstance(hero_hand_cards, list):
        hero_hand = "".join(hero_hand_cards)
    else:
        hero_hand = hero_hand_cards or "??"

    # 1. Hero participation - must have acted beyond posting blinds
    hero_actions = [
        a for a in actions
        if a.get("seat") == hero_seat and a.get("action") != "post_blind"
    ]

    if not hero_actions:
        return None  # Hero not in hand

    # Skip multiway pots: insight quality drops when >2 players see the flop.
    # Any seat with an action on the flop saw it (a flop fold still counts).
    flop_seats = {a["seat"] for a in actions if a.get("street") == "flop"}
    if len(flop_seats) > 2:
        return None

    # Did hero just fold preflop?
    if len(hero_actions) == 1 and hero_actions[0].get("action") == "fold":
        # Still include but with low score - might be an interesting fold
        participation_weight = 0.1
    else:
        participation_weight = 1.0

    # 2. Street reached by hero
    street_order = {"preflop": 1, "flop": 2, "turn": 3, "river": 4}
    hero_streets = [street_order.get(a.get("street", "preflop"), 1) for a in hero_actions]
    max_street_num = max(hero_streets) if hero_streets else 1

    street_names = {1: "preflop", 2: "flop", 3: "turn", 4: "river"}
    max_street = street_names[max_street_num]

    # Weight: river=1.0, turn=0.85, flop=0.6, preflop=0.3
    street_weights = {1: 0.3, 2: 0.6, 3: 0.85, 4: 1.0}
    street_weight = street_weights[max_street_num]

    # 3. Pot size (hero's delta relative to big blind)
    hero_delta = stack_deltas.get(str(hero_seat), 0)
    if hero_delta == 0:
        # Try user_id key
        hero_delta = stack_deltas.get(hero_user_id, 0)

    pot_bb = abs(hero_delta) / big_blind if big_blind > 0 else 0

    # Normalize pot weight: 5bb=0.5, 10bb=0.7, 20bb=1.0, 50bb+=1.5
    if pot_bb < 5:
        pot_weight = 0.3 + (pot_bb / 5) * 0.2  # 0.3 to 0.5
    elif pot_bb < 20:
        pot_weight = 0.5 + (pot_bb - 5) / 15 * 0.5  # 0.5 to 1.0
    else:
        pot_weight = min(1.0 + (pot_bb - 20) / 60, 1.5)  # 1.0 to 1.5

    # 4. Board texture interest
    texture = analyze_board_texture(board)

    texture_score = 1.0
    if texture["suits"] == "monotone":
        texture_score *= 1.2  # Flush possible, interesting
    elif texture["suits"] == "two_tone":
        texture_score *= 1.1

    if texture["connectedness"] == "connected":
        texture_score *= 1.15  # Straight possible
    elif texture["connectedness"] == "semi_connected":
        texture_score *= 1.05

    if texture["paired"]:
        texture_score *= 0.9  # Paired boards slightly less interesting

    texture_weight = min(texture_score, 1.3)

    # 5. Pot type complexity
    pot_type = get_pot_type(actions, hero_seat, big_blind)

    pot_type_weights = {
        "limped": 0.7,
        "single_raised": 1.0,
        "3bet": 1.3,
        "4bet": 1.4,
    }
    pot_type_weight = pot_type_weights.get(pot_type, 1.0)

    # 6. Action complexity - look for raises, check-raises, multi-way action
    action_types = [a.get("action", "").lower() for a in actions if a.get("street") != "preflop"]

    raises_postflop = sum(1 for a in action_types if a in ("raise", "raise_to", "bet"))
    complexity_weight = 1.0 + min(raises_postflop * 0.1, 0.3)  # Up to 1.3x

    # Final score
    final_score = (
        participation_weight *
        street_weight *
        pot_weight *
        texture_weight *
        pot_type_weight *
        complexity_weight
    )

    # Format board string
    board_str = " ".join(board) if board else ""
    profit_bb = hero_delta / big_blind if big_blind > 0 else 0

    return ScoredHand(
        hand_id=hand_id,
        score=round(final_score, 3),
        hero_position=hero_position,
        hero_hand=hero_hand,
        board=board_str,
        profit_bb=round(profit_bb, 1),
        max_street=max_street,
        pot_type=pot_type,
        breakdown={
            "participation": round(participation_weight, 2),
            "street": round(street_weight, 2),
            "pot_size": round(pot_weight, 2),
            "texture": round(texture_weight, 2),
            "pot_type": round(pot_type_weight, 2),
            "complexity": round(complexity_weight, 2),
        }
    )


def get_key_hand_count(session_hands: int) -> int:
    """
    Determine how many key hands to select based on session length.

    - 10-50 hands: 1 key hand
    - 50-100 hands: 2 key hands
    - 100+ hands: 3 key hands
    - <10 hands: 0 (session too short)
    """
    if session_hands < 10:
        return 0
    elif session_hands < 50:
        return 1
    elif session_hands < 100:
        return 2
    else:
        return 3


def select_key_hands(
    hands: list[dict],
    hero_user_id: str,
    max_hands: int = None,
    min_score: float = 0.3
) -> list[ScoredHand]:
    """
    Select the most interesting hands from a session.

    Args:
        hands: List of hand documents
        hero_user_id: The human player's user ID
        max_hands: Maximum key hands (if None, auto-calculated from session length)
        min_score: Minimum score threshold

    Returns:
        List of ScoredHand objects, sorted by score descending
    """
    # Auto-calculate max_hands based on session length
    if max_hands is None:
        max_hands = get_key_hand_count(len(hands))

    if max_hands == 0:
        return []

    scored = []

    for hand in hands:
        result = score_hand(hand, hero_user_id)
        if result and result.score >= min_score:
            scored.append(result)

    # Sort by score descending
    scored.sort(key=lambda x: x.score, reverse=True)

    return scored[:max_hands]


# === Testing utility ===

async def test_key_hands_for_user(user_id_prefix: str, limit: int = 5):
    """
    Test the key hands algorithm on a real user's session.

    Args:
        user_id_prefix: Prefix of user ID to search for (e.g., "rl0ve")
        limit: Max sessions to check
    """
    import firebase_admin
    from firebase_admin import firestore

    # Initialize Firebase if needed
    if not firebase_admin._apps:
        firebase_admin.initialize_app()

    db = firestore.client()

    # Find sessions for user
    print(f"\n🔍 Searching for sessions with user_id containing '{user_id_prefix}'...")

    sessions_ref = db.collection("sessions")
    all_sessions = list(sessions_ref.limit(100).stream())

    matching_sessions = []
    for doc in all_sessions:
        data = doc.to_dict()
        user_id = data.get("user_id", "")
        if user_id_prefix.lower() in user_id.lower():
            matching_sessions.append(data)

    if not matching_sessions:
        print(f"❌ No sessions found for user matching '{user_id_prefix}'")
        return

    print(f"✅ Found {len(matching_sessions)} sessions")

    # Take most recent session
    matching_sessions.sort(key=lambda x: x.get("ended_at", ""), reverse=True)
    session = matching_sessions[0]

    session_id = session.get("session_id", "?")
    user_id = session.get("user_id", "?")
    hand_ids = session.get("hand_ids", [])
    hands_played = session.get("hands_played", len(hand_ids))
    profit = session.get("profit_cents", 0)

    print(f"\n📊 Session: {session_id}")
    print(f"   User: {user_id[:30]}...")
    print(f"   Hands: {hands_played}")
    print(f"   Profit: {profit/100:.2f}")
    print(f"   Hand IDs: {len(hand_ids)}")

    if not hand_ids:
        print("❌ No hand IDs in session")
        return

    # Fetch hands
    print(f"\n📥 Fetching {len(hand_ids)} hands...")
    hands = []
    for hid in hand_ids:
        doc = db.collection("hands").document(hid).get()
        if doc.exists:
            hands.append(doc.to_dict())

    print(f"✅ Fetched {len(hands)} hands")

    # Score all hands
    print(f"\n🎯 Scoring hands for key hand selection...")
    key_hands = select_key_hands(hands, user_id, max_hands=5, min_score=0.1)

    print(f"\n{'='*60}")
    print(f"TOP {len(key_hands)} KEY HANDS")
    print(f"{'='*60}")

    for i, kh in enumerate(key_hands, 1):
        print(f"\n#{i} Score: {kh.score:.3f}")
        print(f"   Hand ID: {kh.hand_id}")
        print(f"   Position: {kh.hero_position} | Hand: {kh.hero_hand}")
        print(f"   Board: {kh.board or '(no board)'}")
        print(f"   Street: {kh.max_street} | Pot Type: {kh.pot_type}")
        print(f"   Profit: {kh.profit_bb:+.1f}bb")
        print(f"   Breakdown: {kh.breakdown}")

    return key_hands


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_key_hands_for_user("rl0ve"))
