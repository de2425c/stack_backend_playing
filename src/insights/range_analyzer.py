"""
Flopzilla-style range analyzer with equity calculations.

Analyzes a preflop range against a board to determine what proportion
of the range falls into each hand category.

Categories match Flopzilla:
- Made hands: straight, flush, set, trips, two_pair, overpair, top_pair,
              middle_pair, weak_pair, no_made_hand
- Draws: flush_draw, oesd, gutshot, backdoor_flush_draw
- Combo draws: flush_draw+pair, oesd+pair, gutshot+pair, etc.

Also includes equity calculation using treys library.
"""

from dataclasses import dataclass, field
from typing import Optional
from collections import Counter
import logging
import random

logger = logging.getLogger(__name__)


# =============================================================================
# Equity Calculation (using treys)
# =============================================================================

def _card_to_treys(card: "Card") -> int:
    """Convert our Card to treys format."""
    from treys import Card as TreysCard
    card_str = f"{card.rank}{card.suit}"
    return TreysCard.new(card_str)


def calculate_hand_vs_hand_equity(
    hero: tuple["Card", "Card"],
    villain: tuple["Card", "Card"],
    board: list["Card"],
    n_simulations: int = 1000,
) -> float:
    """
    Calculate hero's equity against a specific villain hand.

    Args:
        hero: Hero's two hole cards
        villain: Villain's two hole cards
        board: Board cards (3-5 cards)
        n_simulations: Number of Monte Carlo simulations

    Returns:
        Hero's equity as a float (0.0 to 1.0)
    """
    from treys import Evaluator, Deck

    evaluator = Evaluator()

    # Convert cards to treys format
    hero_treys = [_card_to_treys(c) for c in hero]
    villain_treys = [_card_to_treys(c) for c in villain]
    board_treys = [_card_to_treys(c) for c in board]

    # Cards already in play
    dead_cards = set(hero_treys + villain_treys + board_treys)

    # Remaining deck
    full_deck = Deck.GetFullDeck()
    remaining = [c for c in full_deck if c not in dead_cards]

    wins = 0.0
    cards_needed = 5 - len(board_treys)

    if cards_needed == 0:
        # River - just evaluate once
        hero_score = evaluator.evaluate(board_treys, hero_treys)
        villain_score = evaluator.evaluate(board_treys, villain_treys)
        if hero_score < villain_score:
            return 1.0
        elif hero_score == villain_score:
            return 0.5
        else:
            return 0.0

    for _ in range(n_simulations):
        runout = random.sample(remaining, cards_needed)
        full_board = board_treys + runout

        hero_score = evaluator.evaluate(full_board, hero_treys)
        villain_score = evaluator.evaluate(full_board, villain_treys)

        if hero_score < villain_score:
            wins += 1.0
        elif hero_score == villain_score:
            wins += 0.5

    return wins / n_simulations


def calculate_hand_vs_range_equity(
    hero: tuple["Card", "Card"],
    villain_range: dict[str, float],
    board: list["Card"],
    n_samples: int = 100,
    sims_per_matchup: int = 200,
) -> float:
    """
    Calculate hero's equity against a range.

    Uses weighted sampling from the range for efficiency.

    Args:
        hero: Hero's two hole cards
        villain_range: Dict of combo -> frequency (e.g., {"AhKs": 1.0, "9d8c": 0.45})
        board: Board cards (3-5 cards)
        n_samples: Number of villain hands to sample from range
        sims_per_matchup: Simulations per hero vs villain matchup

    Returns:
        Hero's equity as a float (0.0 to 1.0)
    """
    # Filter out combos that overlap with hero or board
    hero_cards = set(hero)
    board_cards = set(board)
    blocked = hero_cards | board_cards

    valid_combos = []
    weights = []

    for combo_str, freq in villain_range.items():
        if freq <= 0:
            continue
        try:
            villain_hand = parse_hand(combo_str)
            if villain_hand[0] not in blocked and villain_hand[1] not in blocked:
                valid_combos.append(villain_hand)
                weights.append(freq)
        except:
            continue

    if not valid_combos:
        return 0.5  # No valid combos, return neutral equity

    # Normalize weights
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.5

    # Sample from range (weighted)
    n_samples = min(n_samples, len(valid_combos))

    # If few combos, use all of them
    if len(valid_combos) <= n_samples:
        sampled_combos = valid_combos
        sampled_weights = weights
    else:
        # Weighted random sample
        indices = random.choices(range(len(valid_combos)), weights=weights, k=n_samples)
        sampled_combos = [valid_combos[i] for i in indices]
        sampled_weights = [weights[i] for i in indices]

    # Calculate weighted equity
    total_equity = 0.0
    total_sampled_weight = sum(sampled_weights)

    for villain_hand, weight in zip(sampled_combos, sampled_weights):
        equity = calculate_hand_vs_hand_equity(
            hero, villain_hand, board, n_simulations=sims_per_matchup
        )
        total_equity += equity * weight

    return total_equity / total_sampled_weight if total_sampled_weight > 0 else 0.5


# =============================================================================
# Card Representation
# =============================================================================

RANKS = "23456789TJQKA"
RANK_VALUES = {r: i for i, r in enumerate(RANKS)}  # '2'->0, 'A'->12
SUITS = "cdhs"


@dataclass(frozen=True)
class Card:
    """Immutable card representation."""
    rank: str  # '2'-'9', 'T', 'J', 'Q', 'K', 'A'
    suit: str  # 'c', 'd', 'h', 's'

    @property
    def rank_value(self) -> int:
        """Numeric rank value (2=0, 3=1, ..., A=12)."""
        return RANK_VALUES[self.rank]

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"

    @classmethod
    def from_string(cls, s: str) -> "Card":
        """Parse 'Ah' -> Card('A', 'h')."""
        if len(s) != 2:
            raise ValueError(f"Invalid card string: {s}")
        rank = s[0].upper()
        suit = s[1].lower()
        if rank not in RANKS:
            raise ValueError(f"Invalid rank: {rank}")
        if suit not in SUITS:
            raise ValueError(f"Invalid suit: {suit}")
        return cls(rank, suit)


def parse_hand(hand_str: str) -> tuple[Card, Card]:
    """Parse 'AhKs' -> (Card('A','h'), Card('K','s'))."""
    clean = hand_str.replace(" ", "")
    if len(clean) != 4:
        raise ValueError(f"Invalid hand string: {hand_str}")
    return (Card.from_string(clean[0:2]), Card.from_string(clean[2:4]))


def parse_board(board_str: str) -> list[Card]:
    """Parse 'Qc 7d 2h' or 'Qc7d2h' -> [Card, Card, Card]."""
    clean = board_str.replace(" ", "")
    if len(clean) % 2 != 0 or len(clean) < 6 or len(clean) > 10:
        raise ValueError(f"Invalid board string: {board_str}")
    return [Card.from_string(clean[i : i + 2]) for i in range(0, len(clean), 2)]


# =============================================================================
# Hand Classification
# =============================================================================

@dataclass
class HandAnalysis:
    """Analysis of a single hand against a board."""
    hand: tuple[Card, Card]
    board: list[Card]

    # Made hand (mutually exclusive - best one)
    made_hand: str = "no_made_hand"
    made_hand_detail: str = ""  # e.g., "top pair with Ace kicker"

    # Draws (can have multiple)
    has_flush_draw: bool = False
    has_oesd: bool = False
    has_gutshot: bool = False
    has_backdoor_flush: bool = False
    has_backdoor_straight: bool = False

    # Combo draws
    draws: list[str] = field(default_factory=list)
    combo_draws: list[str] = field(default_factory=list)


def analyze_hand(hand: tuple[Card, Card], board: list[Card]) -> HandAnalysis:
    """
    Analyze a hand against a board.

    Args:
        hand: Tuple of two hole cards
        board: List of 3-5 board cards

    Returns:
        HandAnalysis with categorization
    """
    analysis = HandAnalysis(hand=hand, board=board)
    all_cards = list(hand) + board

    # Get rank and suit information
    hero_ranks = [c.rank_value for c in hand]
    board_ranks = [c.rank_value for c in board]
    all_ranks = hero_ranks + board_ranks

    hero_suits = [c.suit for c in hand]
    board_suits = [c.suit for c in board]
    all_suits = hero_suits + board_suits

    # Count ranks and suits
    rank_counts = Counter(all_ranks)
    suit_counts = Counter(all_suits)
    board_rank_counts = Counter(board_ranks)

    # Sorted unique ranks for straight detection
    unique_ranks = sorted(set(all_ranks))

    # Is hero's hand a pocket pair?
    is_pocket_pair = hero_ranks[0] == hero_ranks[1]
    pocket_pair_rank = hero_ranks[0] if is_pocket_pair else None

    # Board characteristics
    top_board_rank = max(board_ranks)
    sorted_board_ranks = sorted(board_ranks, reverse=True)
    middle_board_rank = sorted_board_ranks[1] if len(sorted_board_ranks) > 1 else None
    bottom_board_rank = min(board_ranks)

    # ==========================================================================
    # MADE HANDS (check in order of strength, assign first match)
    # ==========================================================================

    made_hand = "no_made_hand"

    # Straight Flush (5 consecutive of same suit)
    if _has_straight_flush(all_cards):
        made_hand = "straight_flush"

    # Quads
    elif max(rank_counts.values()) >= 4:
        made_hand = "quads"

    # Full House (trips + pair or better)
    elif _has_full_house(rank_counts):
        made_hand = "full_house"

    # Flush (5+ of same suit)
    elif max(suit_counts.values()) >= 5:
        made_hand = "flush"

    # Straight (5 consecutive ranks)
    elif _has_straight(unique_ranks):
        made_hand = "straight"

    # Set (pocket pair matches board card = 3 of a kind)
    elif is_pocket_pair and pocket_pair_rank in board_ranks:
        made_hand = "set"

    # Trips (one hole card matches a board pair)
    elif _has_trips(hero_ranks, board_rank_counts):
        made_hand = "trips"

    # Two Pair
    elif _has_two_pair(rank_counts, hero_ranks, board_ranks):
        made_hand = "two_pair"

    # Overpair (pocket pair above top board card)
    elif is_pocket_pair and pocket_pair_rank > top_board_rank:
        made_hand = "overpair"

    # Top Pair (hole card matches top board card)
    elif top_board_rank in hero_ranks and not is_pocket_pair:
        made_hand = "top_pair"

    # Middle Pair
    elif middle_board_rank is not None and middle_board_rank in hero_ranks:
        made_hand = "middle_pair"

    # Weak Pair (bottom pair, underpair, or other pair)
    elif _has_weak_pair(hero_ranks, board_ranks, is_pocket_pair, pocket_pair_rank, top_board_rank):
        made_hand = "weak_pair"

    analysis.made_hand = made_hand

    # ==========================================================================
    # DRAWS (can have multiple, checked independently)
    # ==========================================================================

    draws = []

    # Flush Draw (4 cards of same suit, hero contributes)
    flush_draw_suit = _get_flush_draw_suit(hand, board, suit_counts)
    if flush_draw_suit:
        analysis.has_flush_draw = True
        draws.append("flush_draw")

    # Already have a flush? No draw needed
    if made_hand == "flush":
        analysis.has_flush_draw = False
        draws = [d for d in draws if d != "flush_draw"]

    # OESD (open-ended straight draw - 8 outs)
    if _has_oesd(hero_ranks, board_ranks, unique_ranks) and made_hand != "straight":
        analysis.has_oesd = True
        draws.append("oesd")

    # Gutshot (inside straight draw - 4 outs)
    elif _has_gutshot(hero_ranks, board_ranks, unique_ranks) and made_hand != "straight":
        analysis.has_gutshot = True
        draws.append("gutshot")

    # Backdoor Flush Draw (3 cards of same suit, hero contributes)
    if _has_backdoor_flush(hand, board, suit_counts) and not analysis.has_flush_draw:
        analysis.has_backdoor_flush = True
        draws.append("backdoor_flush")

    # Backdoor Straight Draw (3 connected cards with potential)
    # Only relevant if we don't have any other draws already
    has_any_draw = (analysis.has_oesd or analysis.has_gutshot or
                    analysis.has_flush_draw or analysis.has_backdoor_flush)
    if not has_any_draw:
        if _has_backdoor_straight(hero_ranks, board_ranks):
            analysis.has_backdoor_straight = True
            draws.append("backdoor_straight")

    analysis.draws = draws

    # ==========================================================================
    # COMBO DRAWS
    # ==========================================================================

    combo_draws = []
    has_pair = made_hand in ("top_pair", "middle_pair", "weak_pair", "overpair")

    if analysis.has_flush_draw:
        if analysis.has_oesd:
            combo_draws.append("flush_draw+oesd")
        if analysis.has_gutshot:
            combo_draws.append("flush_draw+gutshot")
        if has_pair:
            combo_draws.append("flush_draw+pair")

    if analysis.has_oesd and has_pair:
        combo_draws.append("oesd+pair")

    if analysis.has_gutshot and has_pair:
        combo_draws.append("gutshot+pair")

    analysis.combo_draws = combo_draws

    return analysis


# =============================================================================
# Helper Functions for Made Hands
# =============================================================================

def _has_straight_flush(cards: list[Card]) -> bool:
    """Check for 5 consecutive cards of the same suit."""
    # Group by suit
    by_suit: dict[str, list[int]] = {}
    for c in cards:
        by_suit.setdefault(c.suit, []).append(c.rank_value)

    for suit, ranks in by_suit.items():
        if len(ranks) >= 5:
            if _has_straight(sorted(set(ranks))):
                return True
    return False


def _has_straight(unique_ranks: list[int]) -> bool:
    """Check for 5 consecutive ranks. Handles A-low (wheel)."""
    ranks = list(unique_ranks)

    # Add ace-low (-1) if ace (12) is present for wheel detection
    if 12 in ranks:
        ranks = [-1] + ranks

    # Check for 5 consecutive
    for i in range(len(ranks) - 4):
        if ranks[i + 4] - ranks[i] == 4:
            # Verify all 5 are present (no gaps due to set conversion)
            window = set(range(ranks[i], ranks[i] + 5))
            if window <= set(ranks):
                return True
    return False


def _has_full_house(rank_counts: Counter) -> bool:
    """Check for trips + pair (or better like trips + trips)."""
    counts = sorted(rank_counts.values(), reverse=True)
    return len(counts) >= 2 and counts[0] >= 3 and counts[1] >= 2


def _has_trips(hero_ranks: list[int], board_rank_counts: Counter) -> bool:
    """Check if hero has trips (one card matches a board pair)."""
    for rank in hero_ranks:
        if board_rank_counts.get(rank, 0) >= 2:
            return True
    return False


def _has_two_pair(rank_counts: Counter, hero_ranks: list[int], board_ranks: list[int]) -> bool:
    """
    Check for two pair where hero contributes to at least one pair.

    Two pair scenarios:
    1. Both hero cards pair board cards (different ranks)
    2. Hero has pocket pair + pairs a board card
    3. Hero pairs one board card + board has a pair
    """
    pairs = [r for r, cnt in rank_counts.items() if cnt >= 2]
    if len(pairs) < 2:
        return False

    # Hero must contribute to at least one of the pairs
    hero_in_pairs = any(r in hero_ranks for r in pairs)
    return hero_in_pairs


def _has_weak_pair(
    hero_ranks: list[int],
    board_ranks: list[int],
    is_pocket_pair: bool,
    pocket_pair_rank: Optional[int],
    top_board_rank: int,
) -> bool:
    """
    Check for weak pair:
    - Pocket pair below top board card (underpair)
    - Pairing a bottom/middle board card
    - Any other pair situation not covered above
    """
    # Underpair
    if is_pocket_pair and pocket_pair_rank < top_board_rank:
        return True

    # Pairing any board card (that wasn't already caught as top/middle pair)
    for hero_rank in hero_ranks:
        if hero_rank in board_ranks:
            return True

    return False


# =============================================================================
# Helper Functions for Draws
# =============================================================================

def _get_flush_draw_suit(
    hand: tuple[Card, Card], board: list[Card], suit_counts: Counter
) -> Optional[str]:
    """
    Get the suit of a flush draw if present.
    Returns None if no flush draw.
    Hero must contribute at least one card to the flush draw.
    """
    hero_suits = [c.suit for c in hand]

    for suit, count in suit_counts.items():
        if count == 4:
            # Hero must have at least one card of this suit
            if suit in hero_suits:
                return suit
    return None


def _has_backdoor_flush(
    hand: tuple[Card, Card], board: list[Card], suit_counts: Counter
) -> bool:
    """
    Check for backdoor flush draw (3 to a flush, hero contributes).
    Only relevant on the flop.
    """
    if len(board) != 3:
        return False

    hero_suits = [c.suit for c in hand]

    for suit, count in suit_counts.items():
        if count == 3 and suit in hero_suits:
            return True
    return False


def _has_oesd(hero_ranks: list[int], board_ranks: list[int], unique_ranks: list[int]) -> bool:
    """
    Check for open-ended straight draw (8 outs).

    OESD = 4 consecutive ranks where completing on either end makes a straight.
    Hero must contribute at least one card to the draw.
    """
    ranks = sorted(unique_ranks)

    # Add ace-low for wheel draws
    if 12 in ranks:
        ranks = [-1] + ranks

    hero_set = set(hero_ranks)

    for i in range(len(ranks) - 3):
        # Check for 4 consecutive
        window = [ranks[i], ranks[i + 1], ranks[i + 2], ranks[i + 3]]
        if window[3] - window[0] == 3:
            # Check if 4 consecutive (no gaps)
            expected = set(range(window[0], window[0] + 4))
            actual = set(window)
            if expected == actual:
                # Verify hero contributes
                # Handle ace-low: if -1 in window, hero needs 12 (ace)
                hero_contribution = hero_set & actual
                if 12 in hero_set and -1 in actual:
                    hero_contribution.add(-1)

                if hero_contribution:
                    # Check it's truly open-ended (not at the edges)
                    low = window[0]
                    high = window[3]
                    # Can complete on low end (low-1 exists) and high end (high+1 exists)
                    # Edge cases:
                    # - Can't go below -1 (ace-low is the bottom)
                    # - Can't go above 12 (ace-high is the top)
                    can_complete_low = low > -1  # Can add a card below
                    can_complete_high = high < 12  # Can add a card above

                    # Special case: A234 is open-ended (can make wheel or 2345)
                    # Represented as -1,0,1,2 -> can complete with 3 (value 1+2=3... wait no)
                    # Actually -1,0,1,2 means A,2,3,4 -> needs 5 on high end
                    # For wheel: -1,0,1,2,3 = A,2,3,4,5

                    if can_complete_low and can_complete_high:
                        return True
    return False


def _has_gutshot(hero_ranks: list[int], board_ranks: list[int], unique_ranks: list[int]) -> bool:
    """
    Check for gutshot straight draw (4 outs).

    Gutshot = 4 cards spanning 5 ranks with exactly 1 gap.
    Hero must contribute at least one card to the draw.
    """
    ranks = sorted(unique_ranks)

    # Add ace-low for wheel draws
    if 12 in ranks:
        ranks = [-1] + ranks

    hero_set = set(hero_ranks)
    if 12 in hero_set:
        hero_set.add(-1)  # Ace can play low

    # Look for 4 cards within a 5-rank span (exactly 1 gap)
    for i in range(len(ranks)):
        for j in range(i + 3, len(ranks)):
            span = ranks[j] - ranks[i]
            if span == 4:  # 5-card span
                # Count how many ranks we have in this span
                window = [r for r in ranks if ranks[i] <= r <= ranks[j]]
                if len(window) == 4:
                    # Exactly 4 cards = 1 gap = gutshot
                    window_set = set(window)
                    # Verify hero contributes
                    if hero_set & window_set:
                        return True
    return False


def _has_backdoor_straight(hero_ranks: list[int], board_ranks: list[int]) -> bool:
    """
    Check for backdoor straight draw.

    Backdoor straight = 3 cards that could become a straight with 2 more cards.
    This is a simplified check for 3 cards within a 5-rank window.
    Only relevant on the flop.
    """
    if len(board_ranks) != 3:
        return False

    all_ranks = sorted(set(hero_ranks + board_ranks))

    # Add ace-low
    if 12 in all_ranks:
        all_ranks = [-1] + all_ranks

    hero_set = set(hero_ranks)
    if 12 in hero_set:
        hero_set.add(-1)

    # Look for 3 cards within a 5-rank span
    for i in range(len(all_ranks)):
        for j in range(i + 2, len(all_ranks)):
            span = all_ranks[j] - all_ranks[i]
            if span <= 4:  # Within a 5-card straight span
                window = [r for r in all_ranks if all_ranks[i] <= r <= all_ranks[j]]
                if len(window) >= 3:
                    window_set = set(window)
                    if hero_set & window_set:
                        return True
    return False


# =============================================================================
# Range Analysis
# =============================================================================

@dataclass
class RangeAnalysis:
    """Analysis of an entire range against a board."""

    # Made hand distribution (mutually exclusive, sums to 100%)
    made_hands: dict[str, dict] = field(default_factory=dict)
    # Structure: {"top_pair": {"pct": 17.8, "weighted_combos": 45.2, "examples": ["AhKs", "AcKd"]}}

    # Draw distribution (can overlap with made hands)
    draws: dict[str, dict] = field(default_factory=dict)

    # Combo draw distribution
    combo_draws: dict[str, dict] = field(default_factory=dict)

    # Total combos analyzed
    total_combos: int = 0
    total_weighted_combos: float = 0.0

    # Errors during analysis
    errors: list[str] = field(default_factory=list)


def analyze_range(
    range_combos: dict[str, float],
    board_str: str,
    max_examples: int = 5,
) -> RangeAnalysis:
    """
    Analyze a range against a board.

    Args:
        range_combos: Dict of combo -> frequency, e.g., {"AhKs": 1.0, "9d8c": 0.45}
        board_str: Board string, e.g., "Qc 7d 2h"
        max_examples: Max example hands to store per category

    Returns:
        RangeAnalysis with category breakdowns
    """
    result = RangeAnalysis()

    try:
        board = parse_board(board_str)
    except ValueError as e:
        result.errors.append(f"Invalid board: {e}")
        return result

    board_cards_set = set(board)

    # Initialize counters
    made_hand_counts: dict[str, dict] = {}
    draw_counts: dict[str, dict] = {}
    combo_draw_counts: dict[str, dict] = {}

    for combo_str, frequency in range_combos.items():
        try:
            hand = parse_hand(combo_str)
        except ValueError as e:
            result.errors.append(f"Invalid hand {combo_str}: {e}")
            continue

        # Skip if hand overlaps with board (card removal)
        if hand[0] in board_cards_set or hand[1] in board_cards_set:
            continue

        result.total_combos += 1
        result.total_weighted_combos += frequency

        # Analyze this hand
        analysis = analyze_hand(hand, board)

        # Accumulate made hand
        mh = analysis.made_hand
        if mh not in made_hand_counts:
            made_hand_counts[mh] = {"weighted": 0.0, "count": 0, "examples": []}
        made_hand_counts[mh]["weighted"] += frequency
        made_hand_counts[mh]["count"] += 1
        if len(made_hand_counts[mh]["examples"]) < max_examples:
            made_hand_counts[mh]["examples"].append(combo_str)

        # Accumulate draws
        for draw in analysis.draws:
            if draw not in draw_counts:
                draw_counts[draw] = {"weighted": 0.0, "count": 0, "examples": []}
            draw_counts[draw]["weighted"] += frequency
            draw_counts[draw]["count"] += 1
            if len(draw_counts[draw]["examples"]) < max_examples:
                draw_counts[draw]["examples"].append(combo_str)

        # Accumulate combo draws
        for combo_draw in analysis.combo_draws:
            if combo_draw not in combo_draw_counts:
                combo_draw_counts[combo_draw] = {"weighted": 0.0, "count": 0, "examples": []}
            combo_draw_counts[combo_draw]["weighted"] += frequency
            combo_draw_counts[combo_draw]["count"] += 1
            if len(combo_draw_counts[combo_draw]["examples"]) < max_examples:
                combo_draw_counts[combo_draw]["examples"].append(combo_str)

    # Convert to percentages
    total = result.total_weighted_combos
    if total > 0:
        for category, data in made_hand_counts.items():
            result.made_hands[category] = {
                "pct": round(100 * data["weighted"] / total, 2),
                "weighted_combos": round(data["weighted"], 1),
                "raw_combos": data["count"],
                "examples": data["examples"],
            }

        for category, data in draw_counts.items():
            result.draws[category] = {
                "pct": round(100 * data["weighted"] / total, 2),
                "weighted_combos": round(data["weighted"], 1),
                "raw_combos": data["count"],
                "examples": data["examples"],
            }

        for category, data in combo_draw_counts.items():
            result.combo_draws[category] = {
                "pct": round(100 * data["weighted"] / total, 2),
                "weighted_combos": round(data["weighted"], 1),
                "raw_combos": data["count"],
                "examples": data["examples"],
            }

    return result


def canonicalize_hand(combo: str) -> str:
    """
    Convert specific combo to canonical form.

    'AhKs' -> 'AKo'
    'AhKh' -> 'AKs'
    '7h7s' -> '77'
    """
    if len(combo) != 4:
        return combo

    r1, s1, r2, s2 = combo[0], combo[1], combo[2], combo[3]

    # Pocket pair
    if r1 == r2:
        return f"{r1}{r2}"

    # Order ranks (higher first)
    v1 = RANK_VALUES.get(r1.upper(), 0)
    v2 = RANK_VALUES.get(r2.upper(), 0)
    if v1 < v2:
        r1, r2 = r2, r1
        s1, s2 = s2, s1

    # Suited or offsuit
    suffix = "s" if s1 == s2 else "o"
    return f"{r1}{r2}{suffix}"


def get_example_hands(combos: list[str], max_examples: int = 5) -> list[str]:
    """
    Get canonical example hands from a list of combos.
    Deduplicates and returns most common types.
    """
    canonical = {}
    for combo in combos:
        canon = canonicalize_hand(combo)
        canonical[canon] = canonical.get(canon, 0) + 1

    # Sort by frequency, return top examples
    sorted_hands = sorted(canonical.keys(), key=lambda x: -canonical[x])
    return sorted_hands[:max_examples]


def format_comprehensive_analysis(
    spot_description: str,
    board: str,
    opener_pos: str,
    opener_analysis: RangeAnalysis,
    responder_pos: str,
    responder_action: str,
    responder_analysis: RangeAnalysis,
    hero_hand: str,
    hero_pos: str,
) -> str:
    """
    Format a comprehensive range analysis for the prompt.

    Returns ~300-400 tokens of detailed range breakdown.
    """
    from .range_analyzer import analyze_hand, parse_hand, parse_board as parse_board_fn

    lines = []
    lines.append("=== RANGE ANALYSIS ===")

    # Board texture
    board_cards = parse_board_fn(board)
    board_suits = [c.suit for c in board_cards]
    board_ranks = sorted([c.rank_value for c in board_cards], reverse=True)

    # Determine texture
    suit_counts = Counter(board_suits)
    max_suit = max(suit_counts.values())
    if max_suit >= 3:
        suit_texture = "monotone"
    elif max_suit == 2:
        suit_texture = "two-tone"
    else:
        suit_texture = "rainbow"

    # Connectedness
    gaps = [board_ranks[i] - board_ranks[i+1] for i in range(len(board_ranks)-1)]
    max_gap = max(gaps) if gaps else 0
    spread = board_ranks[0] - board_ranks[-1] if board_ranks else 0

    if max_gap <= 2 and spread <= 4:
        connect_texture = "connected"
    elif max_gap <= 4 and spread <= 6:
        connect_texture = "semi-connected"
    else:
        connect_texture = "disconnected"

    high_card = RANKS[board_ranks[0]] if board_ranks else "?"

    lines.append(f"Board: {board} ({suit_texture}, {connect_texture}, {high_card}-high)")
    lines.append(f"Spot: {responder_pos} {responder_action} vs {opener_pos} open")
    lines.append("")

    # Board texture summary
    lines.append("BOARD TEXTURE:")
    lines.append(f"  Suits: {suit_texture}" + (" (flush draws possible)" if max_suit == 2 else ""))
    lines.append(f"  Structure: {connect_texture}" + (" (straight draws possible)" if connect_texture != "disconnected" else ""))

    # Who does board favor?
    if board_ranks[0] >= 10:  # T or higher
        if responder_action == "3bet":
            lines.append(f"  Favors: {responder_pos} (3-bettor has more big cards)")
        else:
            lines.append(f"  Favors: {opener_pos} (opener has range advantage on high boards)")
    else:
        lines.append(f"  Favors: {responder_pos} (low board favors caller's range)")
    lines.append("")

    # Helper to format one player's range
    def format_range(pos: str, action: str, analysis: RangeAnalysis) -> list[str]:
        result = []
        total = analysis.total_weighted_combos
        result.append(f"{pos} {action.upper()} RANGE ({total:.0f} weighted combos):")

        # Made hands section
        result.append("  Made Hands:")
        made_order = ["straight", "flush", "set", "trips", "two_pair", "overpair",
                      "top_pair", "middle_pair", "weak_pair", "no_made_hand"]

        for cat in made_order:
            if cat in analysis.made_hands:
                data = analysis.made_hands[cat]
                pct = data["pct"]
                examples = get_example_hands(data["examples"], max_examples=4)
                examples_str = ", ".join(examples) if examples else "rare"

                # Format category name nicely
                cat_display = cat.replace("_", " ").title()
                if cat == "no_made_hand":
                    cat_display = "Air/No pair"

                result.append(f"    {cat_display} ({pct:.1f}%): {examples_str}")

        # Draws section (if any significant draws)
        significant_draws = {k: v for k, v in analysis.draws.items() if v["pct"] >= 3.0}
        if significant_draws:
            result.append("  Draws:")
            draw_order = ["flush_draw", "oesd", "gutshot", "backdoor_flush", "backdoor_straight"]
            for draw in draw_order:
                if draw in significant_draws:
                    data = significant_draws[draw]
                    pct = data["pct"]
                    examples = get_example_hands(data["examples"], max_examples=3)
                    examples_str = ", ".join(examples) if examples else ""
                    draw_display = draw.replace("_", " ").title()
                    result.append(f"    {draw_display} ({pct:.1f}%): {examples_str}")

        # Combo draws (if any)
        significant_combos = {k: v for k, v in analysis.combo_draws.items() if v["pct"] >= 2.0}
        if significant_combos:
            result.append("  Combo Draws:")
            for combo, data in sorted(significant_combos.items(), key=lambda x: -x[1]["pct"]):
                pct = data["pct"]
                examples = get_example_hands(data["examples"], max_examples=2)
                examples_str = ", ".join(examples) if examples else ""
                result.append(f"    {combo} ({pct:.1f}%): {examples_str}")

        return result

    # Format opener's range
    lines.extend(format_range(opener_pos, "open", opener_analysis))
    lines.append("")

    # Format responder's range
    lines.extend(format_range(responder_pos, responder_action, responder_analysis))
    lines.append("")

    # Range advantage summary
    lines.append("RANGE ADVANTAGE:")
    op_strong = sum(opener_analysis.made_hands.get(c, {}).get("pct", 0)
                    for c in ["overpair", "top_pair", "set", "two_pair", "trips", "straight", "flush"])
    re_strong = sum(responder_analysis.made_hands.get(c, {}).get("pct", 0)
                    for c in ["overpair", "top_pair", "set", "two_pair", "trips", "straight", "flush"])

    if re_strong > op_strong + 3:
        lines.append(f"  {responder_pos} has range advantage ({re_strong:.1f}% vs {op_strong:.1f}% strong hands)")
    elif op_strong > re_strong + 3:
        lines.append(f"  {opener_pos} has range advantage ({op_strong:.1f}% vs {re_strong:.1f}% strong hands)")
    else:
        lines.append(f"  Roughly equal ({op_strong:.1f}% vs {re_strong:.1f}% strong hands)")

    # Key differences
    op_overpair = opener_analysis.made_hands.get("overpair", {}).get("pct", 0)
    re_overpair = responder_analysis.made_hands.get("overpair", {}).get("pct", 0)
    if abs(op_overpair - re_overpair) > 1:
        if re_overpair > op_overpair:
            lines.append(f"  {responder_pos} has more overpairs ({re_overpair:.1f}% vs {op_overpair:.1f}%)")
        else:
            lines.append(f"  {opener_pos} has more overpairs ({op_overpair:.1f}% vs {re_overpair:.1f}%)")

    op_tp = opener_analysis.made_hands.get("top_pair", {}).get("pct", 0)
    re_tp = responder_analysis.made_hands.get("top_pair", {}).get("pct", 0)
    if abs(op_tp - re_tp) > 2:
        if re_tp > op_tp:
            lines.append(f"  {responder_pos} has more top pair ({re_tp:.1f}% vs {op_tp:.1f}%)")
        else:
            lines.append(f"  {opener_pos} has more top pair ({op_tp:.1f}% vs {re_tp:.1f}%)")

    lines.append("")

    # Hero's hand analysis
    if hero_hand:
        lines.append(f"HERO ({hero_hand}):")
        try:
            hero_cards = parse_hand(hero_hand)
            hero_analysis = analyze_hand(hero_cards, board_cards)

            # Category
            cat_display = hero_analysis.made_hand.replace("_", " ").title()
            lines.append(f"  Category: {cat_display}")

            # Draws
            if hero_analysis.draws:
                draws_str = ", ".join(d.replace("_", " ") for d in hero_analysis.draws)
                lines.append(f"  Draws: {draws_str}")

            # Determine villain's range based on hero's position
            from .range_lookup import RangeLookup
            lookup = RangeLookup()

            # Get the actual ranges for equity calculation
            if hero_pos == opener_pos:
                # Hero is opener, villain is responder
                villain_range_dict = None
                spot_string = f"{responder_pos} vs {opener_pos} {responder_action}"
                spot_ranges = lookup.get_ranges_for_spot(spot_string)
                if spot_ranges and spot_ranges.responder_range:
                    villain_range_dict = spot_ranges.responder_range
                villain_pos = responder_pos
            else:
                # Hero is responder, villain is opener
                villain_range_dict = None
                spot_string = f"{responder_pos} vs {opener_pos} {responder_action}"
                spot_ranges = lookup.get_ranges_for_spot(spot_string)
                if spot_ranges and spot_ranges.opener_range:
                    villain_range_dict = spot_ranges.opener_range
                villain_pos = opener_pos

            # Calculate actual equity vs range
            if villain_range_dict:
                try:
                    equity = calculate_hand_vs_range_equity(
                        hero_cards,
                        villain_range_dict,
                        board_cards,
                        n_samples=50,
                        sims_per_matchup=100,
                    )
                    lines.append(f"  Equity vs {villain_pos}: {equity*100:.0f}%")
                except Exception as eq_err:
                    logger.warning(f"Equity calculation failed: {eq_err}")

        except Exception as e:
            lines.append(f"  (Could not analyze: {e})")

    return "\n".join(lines)


def extract_preflop_spot(
    hero_position: str,
    pot_type: str,
    street_actions: list,
) -> Optional[tuple[str, str, str, str]]:
    """
    Extract preflop spot from hand data.

    Args:
        hero_position: Hero's position (BTN, BB, etc.)
        pot_type: "single raised", "3-bet", etc.
        street_actions: List of StreetAction objects

    Returns:
        (opener_pos, responder_pos, responder_action, spot_string) or None
    """
    import re

    if not street_actions:
        return None

    # Get preflop action string
    preflop_action = ""
    for sa in street_actions:
        if sa.street.lower() == "preflop":
            preflop_action = sa.actions
            break

    if not preflop_action:
        return None

    preflop_lower = preflop_action.lower()

    # Parse the action to find opener and responder
    # Patterns: "BTN raises to 2.5bb, BB calls"
    #           "BTN raises to 2.5bb, BB 3-bets to 9bb, BTN calls"

    # Find positions mentioned
    positions = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
    pos_pattern = r'\b(' + '|'.join(positions) + r')\b'
    found_positions = re.findall(pos_pattern, preflop_action, re.IGNORECASE)
    found_positions = [p.upper() for p in found_positions]

    if len(found_positions) < 2:
        return None

    # Determine pot type and extract spot
    if "3-bet" in preflop_lower or "3bet" in preflop_lower or pot_type == "3-bet":
        # 3-bet pot: find who opened and who 3-bet
        # Pattern: "X raises..., Y 3-bets..."
        opener_match = re.search(r'(\w+)\s+raises?', preflop_action, re.IGNORECASE)
        three_bettor_match = re.search(r'(\w+)\s+3-?bets?', preflop_action, re.IGNORECASE)

        if opener_match and three_bettor_match:
            opener = opener_match.group(1).upper()
            responder = three_bettor_match.group(1).upper()
            if opener in positions and responder in positions:
                spot_string = f"{responder} vs {opener} 3bet"
                return (opener, responder, "3bet", spot_string)

    # Single raised pot: find opener and caller
    # Pattern: "X raises..., Y calls"
    opener_match = re.search(r'(\w+)\s+raises?', preflop_action, re.IGNORECASE)
    caller_match = re.search(r'(\w+)\s+calls?', preflop_action, re.IGNORECASE)

    if opener_match and caller_match:
        opener = opener_match.group(1).upper()
        responder = caller_match.group(1).upper()
        if opener in positions and responder in positions:
            spot_string = f"{responder} vs {opener} call"
            return (opener, responder, "call", spot_string)

    return None


def build_range_analysis_for_hand(
    hero_position: str,
    hero_hand: str,
    pot_type: str,
    street_actions: list,
    board: str,
) -> Optional[str]:
    """
    Build comprehensive range analysis for a hand.

    Args:
        hero_position: Hero's position
        hero_hand: Hero's hole cards (e.g., "JsJc")
        pot_type: Pot type string
        street_actions: List of StreetAction objects
        board: Flop board string (e.g., "Kd 8c 4s")

    Returns:
        Formatted range analysis string, or None if unavailable
    """
    from .range_lookup import RangeLookup

    # Extract the preflop spot
    spot_info = extract_preflop_spot(hero_position, pot_type, street_actions)
    if not spot_info:
        return None

    opener_pos, responder_pos, responder_action, spot_string = spot_info

    # Lookup ranges
    lookup = RangeLookup()
    if not lookup.is_connected:
        return None

    spot_ranges = lookup.get_ranges_for_spot(spot_string)
    if not spot_ranges or not spot_ranges.opener_range or not spot_ranges.responder_range:
        return None

    # Analyze both ranges against the board
    try:
        opener_analysis = analyze_range(spot_ranges.opener_range, board)
        responder_analysis = analyze_range(spot_ranges.responder_range, board)
    except Exception as e:
        return None

    # Format comprehensive analysis
    return format_comprehensive_analysis(
        spot_description=spot_string,
        board=board,
        opener_pos=opener_pos,
        opener_analysis=opener_analysis,
        responder_pos=responder_pos,
        responder_action=responder_action,
        responder_analysis=responder_analysis,
        hero_hand=hero_hand,
        hero_pos=hero_position,
    )


def analyze_spot_vs_board(spot: str, board: str) -> dict:
    """
    High-level function: analyze a preflop spot against a board.

    Args:
        spot: e.g., "BB vs BTN call", "SB vs CO 3bet"
        board: e.g., "Qc 7d 2h"

    Returns:
        Dict with both players' range breakdowns on this board
    """
    from .range_lookup import RangeLookup

    lookup = RangeLookup()
    spot_ranges = lookup.get_ranges_for_spot(spot)

    if not spot_ranges:
        return {"error": f"Spot not found: {spot}"}

    result = {
        "spot": spot_ranges.spot_description,
        "board": board,
        "opener": {
            "position": spot_ranges.opener_position,
            "analysis": None,
        },
        "responder": {
            "position": spot_ranges.responder_position,
            "action": spot_ranges.responder_action,
            "analysis": None,
        },
    }

    # Analyze opener's range
    opener_analysis = analyze_range(spot_ranges.opener_range, board)
    result["opener"]["analysis"] = {
        "made_hands": opener_analysis.made_hands,
        "draws": opener_analysis.draws,
        "combo_draws": opener_analysis.combo_draws,
        "total_weighted": opener_analysis.total_weighted_combos,
    }

    # Analyze responder's range (if exists)
    if spot_ranges.responder_range:
        resp_analysis = analyze_range(spot_ranges.responder_range, board)
        result["responder"]["analysis"] = {
            "made_hands": resp_analysis.made_hands,
            "draws": resp_analysis.draws,
            "combo_draws": resp_analysis.combo_draws,
            "total_weighted": resp_analysis.total_weighted_combos,
        }

    return result


def summarize_range_advantage(spot: str, board: str) -> str:
    """
    Generate a natural language summary of range advantage.

    Args:
        spot: e.g., "BB vs BTN call"
        board: e.g., "Qc 7d 2h"

    Returns:
        Human-readable summary of who has range advantage and why
    """
    data = analyze_spot_vs_board(spot, board)
    if "error" in data:
        return data["error"]

    opener = data["opener"]
    responder = data["responder"]
    op_mh = opener["analysis"]["made_hands"]
    re_mh = responder["analysis"]["made_hands"]

    lines = [f"Board: {board}"]
    lines.append(f"{opener['position']} open vs {responder['position']} {responder['action']}")
    lines.append("")

    # Compare key categories
    categories = ["overpair", "top_pair", "set", "two_pair", "flush_draw", "oesd"]
    for cat in categories:
        op_pct = op_mh.get(cat, {}).get("pct", 0)
        re_pct = re_mh.get(cat, {}).get("pct", 0)
        if op_pct > 0 or re_pct > 0:
            lines.append(f"  {cat}: {opener['position']} {op_pct}% vs {responder['position']} {re_pct}%")

    # Determine advantage
    op_strong = sum(op_mh.get(c, {}).get("pct", 0) for c in ["overpair", "top_pair", "set", "two_pair"])
    re_strong = sum(re_mh.get(c, {}).get("pct", 0) for c in ["overpair", "top_pair", "set", "two_pair"])

    lines.append("")
    if op_strong > re_strong + 3:
        lines.append(f"→ {opener['position']} has range advantage ({op_strong:.1f}% vs {re_strong:.1f}% strong hands)")
    elif re_strong > op_strong + 3:
        lines.append(f"→ {responder['position']} has range advantage ({re_strong:.1f}% vs {op_strong:.1f}% strong hands)")
    else:
        lines.append(f"→ Roughly equal ranges ({op_strong:.1f}% vs {re_strong:.1f}% strong hands)")

    return "\n".join(lines)


def format_range_analysis(analysis: RangeAnalysis) -> str:
    """Format range analysis as a human-readable string."""
    lines = []

    lines.append(f"Total: {analysis.total_combos} combos ({analysis.total_weighted_combos:.1f} weighted)")
    lines.append("")

    # Made hands (sorted by strength order)
    made_hand_order = [
        "straight_flush", "quads", "full_house", "flush", "straight",
        "set", "trips", "two_pair", "overpair", "top_pair",
        "middle_pair", "weak_pair", "no_made_hand"
    ]

    lines.append("=== MADE HANDS ===")
    for mh in made_hand_order:
        if mh in analysis.made_hands:
            data = analysis.made_hands[mh]
            examples = ", ".join(data["examples"][:3])
            lines.append(f"  {mh:15} {data['pct']:5.1f}%  ({examples})")

    # Draws
    if analysis.draws:
        lines.append("")
        lines.append("=== DRAWS ===")
        for draw, data in sorted(analysis.draws.items(), key=lambda x: -x[1]["pct"]):
            examples = ", ".join(data["examples"][:3])
            lines.append(f"  {draw:15} {data['pct']:5.1f}%  ({examples})")

    # Combo draws
    if analysis.combo_draws:
        lines.append("")
        lines.append("=== COMBO DRAWS ===")
        for cd, data in sorted(analysis.combo_draws.items(), key=lambda x: -x[1]["pct"]):
            examples = ", ".join(data["examples"][:3])
            lines.append(f"  {cd:20} {data['pct']:5.1f}%  ({examples})")

    return "\n".join(lines)
