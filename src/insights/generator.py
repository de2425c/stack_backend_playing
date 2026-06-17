"""Generate poker insights using Claude."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .schema import InsightRequest, InsightResponse, StreetAction, HandInsightRequest
from .prompts.reasoning import (
    REASONING_SYSTEM_PROMPT,
    INSIGHT_SYSTEM_PROMPT,
    HU_REASONING_SYSTEM_PROMPT,
    HU_INSIGHT_SYSTEM_PROMPT,
    SEARCH_JANDA_TOOL,
)


# Tool definitions for agentic RAG
SEARCH_CONCEPTS_TOOL = {
    "name": "search_concepts",
    "description": "Search for relevant poker concepts. Returns concept names and key insights. Use when you need strategic context about a specific poker concept like '3-betting ranges', 'c-bet sizing', 'pot control', etc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What poker concept to search for, e.g., '3-betting ranges', 'c-bet sizing on wet boards', 'bluff catching river'"
            }
        },
        "required": ["query"]
    }
}

SEARCH_TEXTBOOK_TOOL = {
    "name": "search_textbook",
    "description": "Search poker textbook for detailed explanations and theory. Use when you need deeper context or specific strategic guidance beyond concept summaries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search in the textbook, e.g., 'turn barreling strategy', 'blockers in bluffing'"
            }
        },
        "required": ["query"]
    }
}

SEARCH_TERMS_TOOL = {
    "name": "search_terms",
    "description": "Look up poker term definitions from glossary. Use to find precise definitions of terms like 'fold equity', 'SPR', 'polarized range', 'equity realization', etc. Returns term name, definition, and detailed explanation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The poker term to look up, e.g., 'fold equity', 'c-bet', 'pot odds'"
            }
        },
        "required": ["query"]
    }
}


import re
from pathlib import Path


class TermExtractor:
    """Extract poker terms from text by matching against glossary."""

    def __init__(self, terms_path: str | None = None):
        """
        Load terms and build matching patterns.

        Args:
            terms_path: Path to local_content.json. If None, uses default location.
        """
        if terms_path is None:
            terms_path = Path(__file__).parent / "content_admin" / "local_content.json"

        with open(terms_path) as f:
            self.terms = json.load(f)

        # Build lookup: lowercase pattern -> (term_id, canonical_name)
        # Include the term name and common variations
        self.patterns = {}
        for term_id, term_data in self.terms.items():
            name = term_data["name"]
            # Add the full name
            self._add_pattern(name.lower(), term_id, name)

            # Add variations based on common patterns
            # e.g., "Continuation Bet (C-bet)" -> also match "c-bet", "cbet"
            if "(" in name:
                # Extract abbreviation from parentheses
                abbrev = name[name.find("(")+1:name.find(")")]
                self._add_pattern(abbrev.lower(), term_id, abbrev)
                # Also without hyphen
                self._add_pattern(abbrev.lower().replace("-", ""), term_id, abbrev)
                # Base name without parenthetical
                base = name[:name.find("(")].strip()
                self._add_pattern(base.lower(), term_id, base)

            # Handle hyphenated terms (match with/without hyphen)
            if "-" in term_id:
                # "fold-equity" -> "fold equity"
                self._add_pattern(term_id.replace("-", " "), term_id, name)

            # Common abbreviations
            abbrev_map = {
                "stack-to-pot-ratio": ["spr"],
                "out-of-position": ["oop"],
                "in-position": ["ip"],
                "expected-value": ["ev", "+ev", "-ev"],
                "tight-aggressive": ["tag"],
                "loose-aggressive": ["lag"],
                "open-ended-straight-draw": ["oesd"],
                "minimum-defense-frequency": ["mdf"],
                "heads-up-pot": ["hu", "heads up", "heads-up"],
                "big-blind": ["bb"],
                "small-blind": ["sb"],
                "under-the-gun": ["utg"],
                "continuation-bet": ["c-bet", "cbet", "c bet"],
                "3-bet": ["3bet", "three-bet", "three bet"],
                "4-bet": ["4bet", "four-bet", "four bet"],
            }
            if term_id in abbrev_map:
                for abbrev in abbrev_map[term_id]:
                    self._add_pattern(abbrev, term_id, abbrev.upper() if len(abbrev) <= 3 else abbrev)

        # Sort patterns by length (longest first) for greedy matching
        self.sorted_patterns = sorted(self.patterns.keys(), key=len, reverse=True)

        # Build regex pattern for word boundary matching
        # Escape special regex chars in patterns
        escaped = [re.escape(p) for p in self.sorted_patterns]
        self.regex = re.compile(r'\b(' + '|'.join(escaped) + r')\b', re.IGNORECASE)

    def _add_pattern(self, pattern: str, term_id: str, display: str, add_plural: bool = True):
        """Add a pattern if not already present (first one wins for priority)."""
        pattern = pattern.lower().strip()
        if pattern and pattern not in self.patterns:
            self.patterns[pattern] = (term_id, display)

            # Also add plural form if it ends in a word (not abbreviation)
            if add_plural and len(pattern) > 3 and pattern[-1].isalpha():
                plural = None
                if pattern.endswith('y') and not pattern.endswith(('ay', 'ey', 'oy', 'uy')):
                    # e.g., "equity" -> "equities" - skip these, they're rarely pluralized in poker context
                    pass
                elif pattern.endswith('s') or pattern.endswith('x') or pattern.endswith('ch') or pattern.endswith('sh'):
                    plural = pattern + "es"
                else:
                    plural = pattern + "s"

                if plural and plural not in self.patterns:
                    self.patterns[plural] = (term_id, display + "s" if display[-1].isalpha() else display)

    def extract(self, text: str) -> dict[str, str]:
        """
        Extract poker terms from text.

        Returns:
            Dict mapping term_id -> matched text as it appears in the insight
        """
        found = {}
        for match in self.regex.finditer(text):
            matched_text = match.group(0)
            pattern_key = matched_text.lower()
            if pattern_key in self.patterns:
                term_id, _ = self.patterns[pattern_key]
                # Store the actual matched text (preserving case from insight)
                if term_id not in found:
                    found[term_id] = matched_text
        return found


# Global term extractor instance (lazy loaded)
_term_extractor: TermExtractor | None = None


def get_term_extractor() -> TermExtractor:
    """Get or create the global term extractor."""
    global _term_extractor
    if _term_extractor is None:
        _term_extractor = TermExtractor()
    return _term_extractor


RANK_NAMES = {
    'A': 'Ace', 'K': 'King', 'Q': 'Queen', 'J': 'Jack', 'T': 'Ten',
    '9': 'Nine', '8': 'Eight', '7': 'Seven', '6': 'Six', '5': 'Five',
    '4': 'Four', '3': 'Three', '2': 'Two'
}
RANK_VALUES = {
    'A': 14, 'K': 13, 'Q': 12, 'J': 11, 'T': 10,
    '9': 9, '8': 8, '7': 7, '6': 6, '5': 5, '4': 4, '3': 3, '2': 2
}
SUIT_NAMES = {'h': 'hearts', 'd': 'diamonds', 'c': 'clubs', 's': 'spades'}
SUIT_SYMBOLS = {'h': '♥', 'd': '♦', 'c': '♣', 's': '♠'}


def analyze_board_texture(board: str) -> dict:
    """
    Analyze board texture for strategic context.

    Returns dict with:
        - suits: "monotone", "two-tone", "rainbow"
        - flush_draw: suit if flush draw possible, None otherwise
        - connectedness: "disconnected", "semi-connected", "connected"
        - high_card: highest card on board
        - paired: True if board is paired
        - straight_draws: description of straight possibilities
    """
    # Parse board
    board_clean = board.replace(" ", "").replace("-", "")
    cards = []
    for i in range(0, len(board_clean), 2):
        rank = board_clean[i]
        suit = board_clean[i + 1]
        cards.append((rank, suit))

    if not cards:
        return {}

    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    values = sorted([RANK_VALUES.get(r, 0) for r in ranks], reverse=True)

    # Suit analysis
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1

    max_suit_count = max(suit_counts.values())
    flush_suit = None
    for s, count in suit_counts.items():
        if count >= 2:
            flush_suit = s

    if max_suit_count >= 3:
        suit_texture = "monotone"
    elif max_suit_count == 2:
        suit_texture = "two-tone"
    else:
        suit_texture = "rainbow"

    # Connectedness (for flop)
    if len(values) >= 3:
        gaps = []
        for i in range(len(values) - 1):
            gaps.append(values[i] - values[i + 1])

        max_gap = max(gaps)
        total_spread = values[0] - values[-1]

        if max_gap <= 2 and total_spread <= 4:
            connectedness = "connected"
        elif max_gap <= 3 or total_spread <= 6:
            connectedness = "semi-connected"
        else:
            connectedness = "disconnected"
    else:
        connectedness = "unknown"

    # Paired board
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    paired = max(rank_counts.values()) >= 2

    # High card
    high_card = max(ranks, key=lambda r: RANK_VALUES.get(r, 0))

    # Straight draw analysis
    straight_desc = []
    if connectedness == "connected":
        straight_desc.append("many straight draws possible")
    elif connectedness == "semi-connected":
        straight_desc.append("some straight draws possible")

    # Check for specific patterns
    if len(values) >= 3:
        # Broadway heavy
        if values[0] >= 10 and values[1] >= 10:
            straight_desc.append("broadway cards")
        # Low connected
        if values[-1] <= 6 and connectedness != "disconnected":
            straight_desc.append("low connected")

    return {
        "suits": suit_texture,
        "flush_draw": SUIT_NAMES.get(flush_suit) if flush_suit and max_suit_count >= 2 else None,
        "connectedness": connectedness,
        "high_card": RANK_NAMES.get(high_card, high_card),
        "paired": paired,
        "straight_draws": ", ".join(straight_desc) if straight_desc else "limited",
    }


def get_pot_type(street_actions: list) -> str:
    """Determine pot type from preflop action."""
    if not street_actions:
        return "unknown"

    preflop = street_actions[0].actions.lower() if street_actions else ""

    if "4-bet" in preflop or "4bet" in preflop:
        return "4-bet pot"
    elif "3-bet" in preflop or "3bet" in preflop:
        return "3-bet pot"
    elif "raises" in preflop or "raise" in preflop:
        return "single raised pot"
    elif "limp" in preflop or "calls" in preflop:
        return "limped pot"
    else:
        return "raised pot"


def count_players(street_actions: list) -> int:
    """Count players who saw the flop."""
    if not street_actions:
        return 2

    preflop = street_actions[0].actions.lower() if street_actions else ""

    # Count "calls" and add 1 for the raiser/limper
    calls = preflop.count("call")
    if "raises" in preflop or "raise" in preflop:
        return calls + 1  # raiser + callers
    else:
        return calls + 1  # limpers


def describe_hand(hero_hand: str, board: str) -> str:
    """
    Describe hero's hand with strategic context about strength and draws.
    """
    # Parse hero cards
    cards = []
    for i in range(0, len(hero_hand), 2):
        rank = hero_hand[i]
        suit = hero_hand[i + 1]
        cards.append((rank, suit))

    # Parse board cards
    board_cards = []
    board_clean = board.replace(" ", "").replace("-", "")
    for i in range(0, len(board_clean), 2):
        rank = board_clean[i]
        suit = board_clean[i + 1]
        board_cards.append((rank, suit))

    if not cards:
        return "No hand"

    hero_ranks = [r for r, _ in cards]
    hero_suits = [s for _, s in cards]
    board_ranks = [r for r, _ in board_cards]
    board_suits = [s for _, s in board_cards]

    hero_values = sorted([RANK_VALUES.get(r, 0) for r in hero_ranks], reverse=True)
    board_values = sorted([RANK_VALUES.get(r, 0) for r in board_ranks], reverse=True)

    all_cards = cards + board_cards

    # Rank counts: combined / hero / board
    all_rank_counts: dict[str, int] = {}
    for r, _ in all_cards:
        all_rank_counts[r] = all_rank_counts.get(r, 0) + 1
    hero_rank_counts: dict[str, int] = {}
    for r in hero_ranks:
        hero_rank_counts[r] = hero_rank_counts.get(r, 0) + 1
    board_rank_counts: dict[str, int] = {}
    for r in board_ranks:
        board_rank_counts[r] = board_rank_counts.get(r, 0) + 1

    # Sorted by count desc, then value desc
    sorted_ranks = sorted(
        all_rank_counts.items(),
        key=lambda x: (-x[1], -RANK_VALUES.get(x[0], 0)),
    )

    suit_counts: dict[str, int] = {}
    for _, s in all_cards:
        suit_counts[s] = suit_counts.get(s, 0) + 1

    made_hands: list[str] = []
    draws: list[str] = []

    def _hand_value_to_rank_char(val: int) -> str:
        for r, v in RANK_VALUES.items():
            if v == val:
                return r
        return str(val)

    # === STRAIGHT FLUSH / ROYAL FLUSH ===
    sf_high: int | None = None
    for suit, count in suit_counts.items():
        if count < 5 or suit not in hero_suits:
            continue
        suited_vals = sorted(
            {RANK_VALUES.get(r, 0) for r, s in all_cards if s == suit},
            reverse=True,
        )
        if 14 in suited_vals:
            suited_vals_with_wheel = suited_vals + [1]
        else:
            suited_vals_with_wheel = suited_vals
        for i in range(len(suited_vals_with_wheel) - 4):
            if suited_vals_with_wheel[i] - suited_vals_with_wheel[i + 4] == 4:
                hi = suited_vals_with_wheel[i]
                lo = suited_vals_with_wheel[i + 4]
                straight_set = set(range(lo, hi + 1))
                if 1 in straight_set:
                    straight_set.discard(1)
                    straight_set.add(14)
                hero_in_sf = any(
                    RANK_VALUES.get(hr, 0) in straight_set and hs == suit
                    for hr, hs in cards
                )
                if hero_in_sf:
                    sf_high = hi
                    break
        if sf_high is not None:
            break

    if sf_high == 14:
        made_hands.append("royal flush")
    elif sf_high is not None:
        high_char = _hand_value_to_rank_char(sf_high)
        made_hands.append(f"straight flush ({RANK_NAMES.get(high_char, high_char)} high)")

    # === QUADS ===
    elif sorted_ranks and sorted_ranks[0][1] == 4:
        quad_rank = sorted_ranks[0][0]
        if hero_rank_counts.get(quad_rank, 0) >= 1:
            made_hands.append(f"quads ({RANK_NAMES[quad_rank]}s)")
        else:
            made_hands.append(f"quads on board ({RANK_NAMES[quad_rank]}s)")

    # === FULL HOUSE ===
    elif (
        sorted_ranks
        and sorted_ranks[0][1] == 3
        and len(sorted_ranks) >= 2
        and sorted_ranks[1][1] >= 2
    ):
        trips_rank = sorted_ranks[0][0]
        pair_rank = sorted_ranks[1][0]
        hero_in_trips = hero_rank_counts.get(trips_rank, 0) >= 1
        hero_in_pair = hero_rank_counts.get(pair_rank, 0) >= 1
        if hero_in_trips or hero_in_pair:
            made_hands.append(
                f"full house ({RANK_NAMES[trips_rank]}s full of {RANK_NAMES[pair_rank]}s)"
            )
        else:
            made_hands.append(
                f"full house on board ({RANK_NAMES[trips_rank]}s full of {RANK_NAMES[pair_rank]}s)"
            )

    # === FLUSH ===
    elif max(suit_counts.values(), default=0) >= 5:
        flush_suit = max(suit_counts, key=lambda s: suit_counts[s])
        if flush_suit in hero_suits:
            made_hands.append(f"flush in {SUIT_NAMES[flush_suit]}")
        else:
            made_hands.append(f"flush on board ({SUIT_NAMES[flush_suit]})")

    else:
        # === STRAIGHT ===
        all_values_set = {RANK_VALUES.get(r, 0) for r in [c[0] for c in all_cards]}
        sorted_vals = sorted(all_values_set, reverse=True)
        if 14 in all_values_set:
            sorted_vals = sorted_vals + [1]
        straight_high: int | None = None
        for i in range(len(sorted_vals) - 4):
            if sorted_vals[i] - sorted_vals[i + 4] == 4:
                hi = sorted_vals[i]
                lo = sorted_vals[i + 4]
                straight_set = set(range(lo, hi + 1))
                hero_set = set(hero_values)
                if 1 in straight_set and 14 in hero_set:
                    hero_check = hero_set | {1}
                else:
                    hero_check = hero_set
                if hero_check & straight_set:
                    straight_high = hi
                    break

        if straight_high is not None:
            high_char = _hand_value_to_rank_char(straight_high)
            made_hands.append(f"straight ({RANK_NAMES.get(high_char, high_char)} high)")

        # === THREE OF A KIND ===
        elif sorted_ranks and sorted_ranks[0][1] == 3:
            trips_rank = sorted_ranks[0][0]
            hc = hero_rank_counts.get(trips_rank, 0)
            bc = board_rank_counts.get(trips_rank, 0)
            if hc == 2:
                made_hands.append(f"set of {RANK_NAMES[trips_rank].lower()}s")
            elif hc == 1 and bc == 2:
                made_hands.append(f"trips ({RANK_NAMES[trips_rank]}s)")
            else:
                made_hands.append(f"three of a kind on board ({RANK_NAMES[trips_rank]}s)")

        # === TWO PAIR / ONE PAIR ===
        elif sorted_ranks and sorted_ranks[0][1] == 2:
            pair_ranks = [r for r, c in sorted_ranks if c == 2]
            hero_pair_ranks = [r for r in pair_ranks if hero_rank_counts.get(r, 0) >= 1]

            if len(pair_ranks) >= 2 and hero_pair_ranks:
                top, bot = pair_ranks[0], pair_ranks[1]
                made_hands.append(
                    f"two pair ({RANK_NAMES[top]}s and {RANK_NAMES[bot]}s)"
                )
            elif hero_pair_ranks:
                pair_rank = hero_pair_ranks[0]
                pair_value = RANK_VALUES.get(pair_rank, 0)
                hero_pair_count = hero_rank_counts.get(pair_rank, 0)

                if hero_pair_count == 2:
                    # Pocket pair (no board match for this rank)
                    max_board = board_values[0] if board_values else 0
                    if not board_values:
                        made_hands.append(
                            f"pocket pair ({RANK_NAMES[pair_rank]}{RANK_NAMES[pair_rank]})"
                        )
                    elif pair_value > max_board:
                        made_hands.append(
                            f"overpair ({RANK_NAMES[pair_rank]}{RANK_NAMES[pair_rank]})"
                        )
                    else:
                        underpair_desc = (
                            f"underpair ({RANK_NAMES[pair_rank]}{RANK_NAMES[pair_rank]})"
                        )
                        overcards = sum(1 for v in board_values if v > pair_value)
                        if overcards >= 2:
                            underpair_desc += f" - {overcards} overcards on board"
                        made_hands.append(underpair_desc)
                else:
                    # Hero paired the board (one of hero's cards matches a board card)
                    other_hero_rank = next(
                        (r for r in hero_ranks if r != pair_rank), None
                    )
                    kicker_value = (
                        RANK_VALUES.get(other_hero_rank, 0) if other_hero_rank else 0
                    )
                    kicker_name = (
                        RANK_NAMES.get(other_hero_rank, "") if other_hero_rank else ""
                    )

                    if board_values and pair_value == board_values[0]:
                        if kicker_value >= 14:
                            made_hands.append(
                                f"top pair top kicker ({RANK_NAMES[pair_rank]}s with {kicker_name} kicker)"
                            )
                        elif kicker_value >= 11:
                            made_hands.append(
                                f"top pair good kicker ({RANK_NAMES[pair_rank]}s with {kicker_name} kicker)"
                            )
                        else:
                            made_hands.append(
                                f"top pair weak kicker ({RANK_NAMES[pair_rank]}s with {kicker_name} kicker)"
                            )
                    elif len(board_values) >= 2 and pair_value == board_values[1]:
                        made_hands.append(f"second pair ({RANK_NAMES[pair_rank]}s)")
                    else:
                        made_hands.append(f"bottom pair ({RANK_NAMES[pair_rank]}s)")
            # else: pair(s) entirely on board, hero doesn't share — fall through to no made hand

    # === DRAWS ===
    # Skip flush/straight draws when hero already has a strong made hand
    strong_made = any(
        h.startswith(("quads", "full house", "flush", "straight", "trips", "set"))
        or "royal flush" in h
        for h in made_hands
    )

    if not strong_made:
        # Flush draw - 4 to a flush is a flush draw, 3 to a flush is backdoor
        for suit, count in suit_counts.items():
            if suit not in hero_suits:
                continue
            if count == 4:
                # 4 cards of same suit = flush draw (need 1 more)
                draws.append(f"flush draw ({SUIT_NAMES[suit]})")
            elif count == 3 and len(board_cards) <= 3:
                # 3 cards of same suit on flop = backdoor flush draw
                draws.append(f"backdoor flush draw ({SUIT_NAMES[suit]})")

        # Straight draw detection (simplified)
        all_unique = sorted(set(hero_values + board_values), reverse=True)
        # Check for OESD or gutshot
        for target in range(14, 4, -1):  # Check each possible straight
            straight_cards = [target - i for i in range(5)]
            # Handle wheel (A-2-3-4-5)
            if 1 in straight_cards:
                straight_cards = [14 if c == 1 else c for c in straight_cards]

            have = sum(1 for c in straight_cards if c in all_unique)
            hero_contributes = any(v in straight_cards for v in hero_values)

            if have == 4 and hero_contributes:
                missing = [c for c in straight_cards if c not in all_unique][0]
                # Is it OESD or gutshot?
                if missing == straight_cards[0] or missing == straight_cards[-1]:
                    if "open-ended straight draw" not in draws:
                        draws.append("open-ended straight draw")
                else:
                    if "gutshot straight draw" not in draws:
                        draws.append("gutshot straight draw")
                break

    # Overcards (if no made hand)
    if not made_hands and board_values:
        overcards = [r for r, v in zip(hero_ranks, hero_values) if v > board_values[0]]
        if len(overcards) == 2:
            draws.append("two overcards")
        elif len(overcards) == 1:
            draws.append(f"one overcard ({RANK_NAMES[overcards[0]]})")

    # === BUILD DESCRIPTION ===
    parts = []

    if made_hands:
        parts.append("Made hand: " + ", ".join(made_hands))
    if draws:
        parts.append("Draws: " + ", ".join(draws))
    if not made_hands and not draws:
        parts.append("No made hand or significant draws (air)")

    return " | ".join(parts)


SYSTEM_PROMPT = """You are a poker coach. Teach ONE concept about the dynamics of this spot.

Output ONLY the insight (1-2 sentences). No preamble.

Focus on:
- How the board texture affects both players' ranges
- How the pot type (limped/raised/3-bet) shapes the dynamics
- Position and information advantages
- What hands want to do in this spot (build pot, protect, see showdown)

Avoid: EV numbers, solver frequencies, generic advice like "call because pot odds" """


HU_HAND_ANALYSIS_SYSTEM_PROMPT = """You are a heads-up poker coach reviewing a HU cash-game hand played by your student (Hero).

CONTEXT: HU no-limit hold'em. Only two players (BTN/SB and BB). BTN opens wide (~70-90%);
BB defends wide (~50%+). Limped pots are normal. Aggression is high.

YOUR TASK:
Identify the most significant decision and teach ONE HU-specific concept. 1-2 sentences max.

Focus on WHY the spot works the way it does in HU — wide-range dynamics, IP/OOP equity
realization, stack-depth tier (deep / mid / short / push-fold), board texture vs HU ranges.

Never invoke positions other than BTN/BB. Never reason multiway. Don't import 6-max
intuitions (a "low connected board" hits HU ranges very differently than 6-max).

MARKING TERMS:
When using a poker term, mark it: {{term-id}}
Example: "Without {{initiative}} and OOP, you give up most of your {{equity-realization}}."

Avoid:
- 6-max framing ("as the EP raiser…")
- Result commentary (variance)
- Forcing jargon that doesn't fit the situation

Only use poker terms when they PRECISELY describe the situation."""

HAND_ANALYSIS_SYSTEM_PROMPT = """You are a poker coach reviewing a hand played by your student (Hero).

YOUR TASK:
Identify the MOST SIGNIFICANT decision and teach ONE concept. Be brief: 1-2 sentences max.

Focus on WHY this spot works the way it does - range dynamics, board texture, or position.

MARKING TERMS:
When using a poker term from search_terms, mark it: {{term-id}}
Example: "Without {{initiative}}, you lack {{fold-equity}}."

Avoid:
- Result commentary (variance)
- Generic advice
- Preambles - just give the insight
- Forcing poker jargon that doesn't fit (e.g., "turning a bluff into a value bet" is nonsensical)

Only use poker terms when they PRECISELY describe the situation. Plain language is better than misused jargon."""

AGENTIC_SYSTEM_PROMPT = """You are a poker coach. Teach ONE concept about the DYNAMICS of this poker spot.

SEARCHING STRATEGY:
- Search for the STRATEGIC SITUATION: pot type, board texture, position dynamics
- Good searches: "limped pot multiway dynamics", "3-bet pot c-betting IP", "turn play after flop check-check", "two-tone board texture strategy"
- Bad searches: "top pair calling", "KJ on QT3" (too hand-specific)
- Search 0-1 times. Only if you need strategic context.
- Use search_terms to look up precise definitions of poker terms you want to use.

YOUR GOAL:
Explain WHY this spot works the way it does. Focus on:
- Range interactions: Who has the advantage on this board? Why?
- Board texture: How does it affect what each player can have?
- Pot type dynamics: Limped pots have wide ranges. 3-bet pots have defined ranges.
- What different hand types want to do here

The insight should help the player understand similar spots in the future - not just this exact hand.

Good examples:
- "In limped pots, no one has a range advantage - the board texture alone determines who can credibly represent strength. On K-J-8, both blinds can have two pairs and straights that a single raiser couldn't."
- "Two-tone boards with connected cards favor the caller's range in single-raised pots - they have more suited connectors and suited one-gappers that hit these textures hard."
- "As the preflop aggressor, your perceived range on A-high dry boards is heavily weighted toward big aces and overpairs, giving you significant credibility to barrel multiple streets."

Bad examples:
- "You should call here because of pot odds" (generic, doesn't teach)
- "The solver recommends checking 62%" (we don't care about solver)
- "Top pair is strong so bet for value" (too simplistic)
- "You're turning your bluff into a value bet" (nonsensical - bluffs and value bets are opposites)

TERM ACCURACY:
- Only use poker terms when they PRECISELY fit the situation
- Never force jargon just because it sounds sophisticated
- If a term doesn't naturally describe the situation, use plain language instead
- Common misuses: "fold equity" when opponent won't fold, "value bet" when betting for protection

OUTPUT FORMAT:
Return a JSON object with two fields:
{
  "insight": "Your 1-2 sentence insight here",
  "terms": {
    "term-id": "exact text used in insight",
    ...
  }
}

The "terms" dict maps term IDs (from search_terms results) to the exact phrase you used in your insight.
Only include terms that actually appear in your insight text. If no terms apply, use an empty dict."""


def build_user_prompt(request: InsightRequest, include_solver: bool = False) -> str:
    """Build the user prompt from an InsightRequest.

    Args:
        request: The insight request
        include_solver: If True, include solver frequencies (default False)
    """
    lines = []

    # Pot type and players
    pot_type = get_pot_type(request.street_actions)
    num_players = count_players(request.street_actions)
    multiway = "multiway" if num_players > 2 else "heads-up"

    lines.append(f"=== SPOT DYNAMICS ===")
    lines.append(f"Pot type: {pot_type} ({multiway}, {num_players} players)")
    lines.append(f"Street: {request.street}")
    lines.append(f"Hero position: {request.hero_position}")
    lines.append(f"Villain position: {request.villain_position}")
    if request.pot_size_bb:
        lines.append(f"Pot: {request.pot_size_bb:.1f}bb | Effective stack: {request.stack_size_bb:.1f}bb")
    lines.append("")

    # Board texture analysis
    board_info = analyze_board_texture(request.board)
    lines.append(f"=== BOARD TEXTURE ===")
    lines.append(f"Board: {request.board}")
    if board_info:
        lines.append(f"High card: {board_info.get('high_card', 'N/A')}")
        lines.append(f"Suits: {board_info.get('suits', 'N/A')}")
        if board_info.get('flush_draw'):
            lines.append(f"Flush draw possible: {board_info['flush_draw']}")
        lines.append(f"Connectedness: {board_info.get('connectedness', 'N/A')}")
        lines.append(f"Straight draws: {board_info.get('straight_draws', 'N/A')}")
        if board_info.get('paired'):
            lines.append("Board is PAIRED")
    lines.append("")

    # Hero's hand
    lines.append(f"=== HERO'S HAND ===")
    lines.append(f"Hole cards: {request.hero_hand}")
    lines.append(f"{describe_hand(request.hero_hand, request.board)}")
    lines.append("")

    # Action history
    lines.append(f"=== ACTION ===")
    for sa in request.street_actions:
        if sa.cards:
            lines.append(f"{sa.street.upper()} [{sa.cards}]: {sa.actions}")
        else:
            lines.append(f"{sa.street.upper()}: {sa.actions}")
    lines.append("")
    lines.append(f"Decision point: {request.action_sequence}")

    # Optional solver data (off by default)
    if include_solver and request.action_frequencies:
        lines.append("")
        lines.append(f"=== SOLVER REFERENCE ===")
        for action, freq in sorted(request.action_frequencies.items(), key=lambda x: -x[1]):
            if freq > 0.05:  # Only show actions with >5%
                lines.append(f"  {action}: {freq*100:.0f}%")

    return "\n".join(lines)


def build_hand_prompt(request: HandInsightRequest) -> str:
    """Build the user prompt from a HandInsightRequest (full hand analysis)."""
    lines = []

    lines.append("=== HAND SUMMARY ===")
    lines.append(f"Hero position: {request.hero_position}")
    lines.append(f"Hero's hand: {request.hero_hand}")
    lines.append(f"Players: {request.num_players}")
    lines.append(f"Pot type: {request.pot_type}")
    lines.append("")

    # Build per-street boards from street_actions
    street_boards = {}
    cumulative_cards = []
    for sa in request.street_actions:
        if sa.cards:
            # Parse cards from this street (space-separated)
            new_cards = sa.cards.strip().split()
            cumulative_cards.extend(new_cards)
            street_boards[sa.street] = " ".join(cumulative_cards)

    # Hero's hand analysis PER STREET
    if request.hero_hand and street_boards:
        lines.append("=== HERO'S HAND BY STREET ===")
        lines.append(f"Hole cards: {request.hero_hand}")
        lines.append("")

        for street in ["flop", "turn", "river"]:
            if street in street_boards:
                board_at_street = street_boards[street]
                hand_desc = describe_hand(request.hero_hand, board_at_street)
                board_texture = analyze_board_texture(board_at_street)

                texture_str = ""
                if board_texture:
                    parts = []
                    if board_texture.get('suits'):
                        parts.append(board_texture['suits'])
                    if board_texture.get('paired'):
                        parts.append("paired")
                    if parts:
                        texture_str = f" ({', '.join(parts)})"

                lines.append(f"{street.upper()} [{board_at_street}]{texture_str}:")
                lines.append(f"  {hand_desc}")
                lines.append("")
    elif request.hero_hand and request.board:
        # Fallback to final board only
        lines.append("=== HERO'S HAND ANALYSIS ===")
        lines.append(describe_hand(request.hero_hand, request.board))
        lines.append("")

    # === RANGE ANALYSIS (flop only) ===
    # Add comprehensive range analysis if we have a flop
    flop_board = street_boards.get("flop")
    if flop_board and request.num_players == 2:  # Only for heads-up pots
        try:
            from .range_analyzer import build_range_analysis_for_hand

            range_analysis = build_range_analysis_for_hand(
                hero_position=request.hero_position,
                hero_hand=request.hero_hand,
                pot_type=request.pot_type,
                street_actions=request.street_actions,
                board=flop_board,
            )

            if range_analysis:
                lines.append(range_analysis)
                lines.append("")
        except Exception as e:
            # Silently skip range analysis if it fails
            pass

    # Action history
    lines.append("=== ACTION BY STREET ===")
    for sa in request.street_actions:
        if sa.cards:
            lines.append(f"{sa.street.upper()} [{sa.cards}]: {sa.actions}")
        else:
            lines.append(f"{sa.street.upper()}: {sa.actions}")
    lines.append("")

    # Hero's decisions
    if request.hero_decisions:
        lines.append("=== HERO'S DECISIONS ===")
        for i, decision in enumerate(request.hero_decisions, 1):
            lines.append(f"{i}. {decision.street.upper()}: Hero {decision.action_taken}")
            lines.append(f"   Facing: {decision.facing} | Pot before: {decision.pot_before_bb:.1f}bb")
            if decision.to_call_bb > 0:
                lines.append(
                    f"   To call {decision.to_call_bb:.1f}bb into {decision.pot_before_bb:.1f}bb "
                    f"→ pot odds {decision.pot_odds}, needs {decision.required_equity_pct:.1f}% equity to break even"
                )
                if decision.villain_all_in:
                    lines.append(
                        "   NOTE: Villain is ALL-IN — there are no future streets and no implied odds; "
                        "the call is a pure equity-vs-required-equity decision."
                    )
        lines.append("")

    # Result (for context, but coach shouldn't focus on it)
    lines.append("=== RESULT ===")
    result = "won" if request.hero_won else "lost"
    lines.append(f"Hero {result} {abs(request.profit_bb):.1f}bb")
    if request.went_to_showdown:
        lines.append("(Went to showdown)")

    return "\n".join(lines)


class InsightGenerator:
    """Generate poker insights using Claude API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        concepts_path: str | None = None,
        use_vector_search: bool = False,
        pinecone_index: str = "poker-rag"
    ):
        """
        Initialize the insight generator.

        Args:
            api_key: Anthropic API key. If not provided, reads from ANTHROPIC_API_KEY env var.
            model: Model to use for generation.
            concepts_path: Path to concepts JSON file for tag-based RAG (legacy).
            use_vector_search: If True, use Pinecone vector search for RAG.
            pinecone_index: Pinecone index name (if use_vector_search=True).
        """
        from anthropic import Anthropic

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self.client = Anthropic(api_key=self.api_key)
        self.model = model

        # Initialize vector store or concept matcher
        self.vector_store = None
        self.concept_matcher = None

        if use_vector_search:
            from .vector_store import PokerVectorStore
            self.vector_store = PokerVectorStore(index_name=pinecone_index)
        elif concepts_path:
            from .concept_matcher import ConceptMatcher
            self.concept_matcher = ConceptMatcher(concepts_path)

    def generate(self, request: InsightRequest, include_solver: bool = False) -> InsightResponse:
        """
        Generate an insight for the given request.

        Args:
            request: InsightRequest with all the poker situation data.
            include_solver: If True, include solver frequencies in prompt (default False)

        Returns:
            InsightResponse with the generated insight.
        """
        user_prompt = build_user_prompt(request, include_solver=include_solver)

        # Use agentic approach if vector store is available
        if self.vector_store:
            return self._generate_agentic(request, user_prompt)

        # Legacy approach: inject RAG context or use concept matcher
        rag_context = ""
        if self.concept_matcher:
            rag_context = self.concept_matcher.get_concept_context(request)

        if rag_context:
            user_prompt = rag_context + "\n\n" + user_prompt

        message = self.client.messages.create(
            model=self.model,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        insight_text = message.content[0].text if message.content else ""

        # Extract terms from the insight text
        extractor = get_term_extractor()
        terms = extractor.extract(insight_text)

        return InsightResponse(
            insight=insight_text,
            model_used=self.model,
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
            terms=terms,
        )

    def _extract_marked_terms(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Extract {{term-id}} markers from text and return clean text + terms dict.

        Returns:
            (clean_text, terms_dict) where terms_dict maps term_id -> display text
        """
        import re
        terms = {}

        # Find all {{term-id}} patterns
        pattern = r'\{\{([^}]+)\}\}'

        def replace_match(match):
            term_id = match.group(1)
            # The display text is just the term-id made readable
            # e.g., "fold-equity" -> "fold equity"
            display_text = term_id.replace('-', ' ')
            terms[term_id] = display_text
            return display_text

        clean_text = re.sub(pattern, replace_match, text)
        return clean_text, terms

    def generate_hand_insight(
        self,
        request: HandInsightRequest,
        is_hu: bool = False,
    ) -> InsightResponse:
        """
        Generate an insight for a complete hand using two-pass architecture.

        Pass 1: Structured reasoning analysis (hidden from user)
        Pass 2: Distill reasoning into concise coaching insight

        Args:
            request: HandInsightRequest with full hand data.
            is_hu: When True, route through HU-tuned prompts and Janda queries.
                Defaults to inferring from request.num_players == 2 so callers
                that haven't been updated still get the HU prompts.
        """
        # Belt-and-suspenders: even if caller didn't pass is_hu, num_players==2
        # is an unambiguous HU signal.
        if not is_hu and request.num_players == 2:
            is_hu = True

        user_prompt = build_hand_prompt(request)

        # Use two-pass agentic approach if vector store is available
        if self.vector_store:
            return self._generate_two_pass(request, user_prompt, is_hu=is_hu)

        # Simple single-pass approach without RAG (fallback)
        system_prompt = HU_HAND_ANALYSIS_SYSTEM_PROMPT if is_hu else HAND_ANALYSIS_SYSTEM_PROMPT
        message = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = message.content[0].text.strip() if message.content else ""

        # Extract marked terms {{term-id}} and get clean text
        insight_text, marked_terms = self._extract_marked_terms(raw_text)

        # Also run auto-detect as fallback
        extractor = get_term_extractor()
        auto_terms = extractor.extract(insight_text)

        # Merge: marked terms take priority
        terms = {**auto_terms, **marked_terms}

        return InsightResponse(
            insight=insight_text,
            model_used=self.model,
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
            terms=terms,
        )

    def _generate_two_pass(
        self,
        request: HandInsightRequest,
        hand_prompt: str,
        is_hu: bool = False,
    ) -> InsightResponse:
        """
        Two-pass insight generation with structured reasoning.

        Pass 1: Generate detailed analysis using 5-step framework
        Pass 2: Distill analysis into user-facing insight

        When is_hu=True both passes use HU-tuned system prompts and the Janda
        pre-fetch is biased toward heads-up theory.
        """
        start_time = time.time()
        total_input_tokens = 0
        total_output_tokens = 0

        insight_system_prompt = HU_INSIGHT_SYSTEM_PROMPT if is_hu else INSIGHT_SYSTEM_PROMPT

        # Pass 1: Reasoning
        reasoning, p1_in, p1_out = self._generate_reasoning(request, hand_prompt, is_hu=is_hu)
        total_input_tokens += p1_in
        total_output_tokens += p1_out

        # Pass 2: Insight
        t2 = time.time()
        insight_prompt = f"{hand_prompt}\n\n=== ANALYSIS ===\n{reasoning}"

        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            system=insight_system_prompt,
            messages=[{"role": "user", "content": insight_prompt}],
            tools=[SEARCH_TERMS_TOOL],
        )
        print(f"[TIMING] Pass2 API call 1: {time.time()-t2:.1f}s ({'HU' if is_hu else '6max'})")

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Handle potential tool use in pass 2
        messages = [{"role": "user", "content": insight_prompt}]
        max_iterations = 3

        for _ in range(max_iterations):
            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = self._execute_tools(response.content)
                messages.append({"role": "user", "content": tool_results})

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=300,
                    system=insight_system_prompt,
                    messages=messages,
                    tools=[SEARCH_TERMS_TOOL],
                )
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

        # Extract insight from response
        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text = block.text.strip()
                break

        # Parse JSON response (may be wrapped in markdown code block)
        insight_text = raw_text
        claude_terms: dict[str, str] = {}

        # Strip markdown code block if present
        json_text = raw_text
        if "```json" in raw_text:
            json_text = raw_text.split("```json", 1)[1]
            if "```" in json_text:
                json_text = json_text.split("```", 1)[0]
        elif "```" in raw_text:
            # Handle plain ``` blocks
            parts = raw_text.split("```")
            if len(parts) >= 2:
                json_text = parts[1]

        json_start = json_text.find('{')
        if json_start != -1:
            json_text = json_text[json_start:]
            # Find matching closing brace
            brace_count = 0
            end_pos = 0
            for i, c in enumerate(json_text):
                if c == '{':
                    brace_count += 1
                elif c == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i + 1
                        break
            if end_pos > 0:
                json_text = json_text[:end_pos]

            try:
                parsed = json.loads(json_text)
                if isinstance(parsed, dict) and "insight" in parsed:
                    insight_text = parsed.get("insight", raw_text)
                    claude_terms = parsed.get("terms", {})
                    if not isinstance(claude_terms, dict):
                        claude_terms = {}
            except json.JSONDecodeError:
                pass

        # Extract terms from insight text
        extractor = get_term_extractor()
        extracted_terms = extractor.extract(insight_text)
        terms = {**extracted_terms, **claude_terms}

        elapsed = time.time() - start_time

        return InsightResponse(
            insight=insight_text,
            model_used=self.model,
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
            terms=terms,
        )

    def _generate_reasoning(
        self,
        hand: HandInsightRequest,
        hand_prompt: str,
        is_hu: bool = False,
    ) -> tuple[str, int, int]:
        """
        Pass 1: Generate structured reasoning analysis.

        Pre-fetches textbook content based on situation, allows additional
        searches via tool during reasoning. When is_hu=True the Janda query
        is biased toward heads-up theory and the HU reasoning prompt is used.
        """
        import time as t
        t0 = t.time()

        # Pre-fetch relevant Janda content based on hand situation
        from .query_builder import build_janda_query

        janda_query = build_janda_query(hand, is_hu=is_hu)
        textbook_results = self.vector_store.search_janda(
            janda_query.query,
            filters=janda_query.filters,
            top_k=3
        )
        print(f"[TIMING] Pinecone search: {t.time()-t0:.1f}s")

        # Format textbook context
        context_str = self._format_janda_results(textbook_results)

        # Build reasoning prompt with pre-fetched context
        reasoning_prompt = (
            f"{hand_prompt}\n\n"
            f"=== RELEVANT TEXTBOOK CONTEXT ===\n"
            f"{context_str}"
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": reasoning_prompt}]
        tools = [SEARCH_JANDA_TOOL]

        reasoning_system_prompt = HU_REASONING_SYSTEM_PROMPT if is_hu else REASONING_SYSTEM_PROMPT

        total_input_tokens = 0
        total_output_tokens = 0
        max_iterations = 5  # Allow more tool calls during reasoning

        for iter_num in range(max_iterations):
            t1 = t.time()
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                system=reasoning_system_prompt,
                messages=messages,
                tools=tools,
            )
            print(f"[TIMING] Pass1 API call {iter_num+1}: {t.time()-t1:.1f}s (stop={response.stop_reason})")

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "end_turn":
                # Extract reasoning text
                reasoning = ""
                for block in response.content:
                    if block.type == "text":
                        reasoning = block.text.strip()
                        break
                print(f"[TIMING] Pass1 total: {t.time()-t0:.1f}s")
                return reasoning, total_input_tokens, total_output_tokens

            if response.stop_reason == "tool_use":
                # Handle tool calls
                messages.append({"role": "assistant", "content": response.content})
                tool_results = self._execute_reasoning_tools(response.content)
                messages.append({"role": "user", "content": tool_results})

        # Fallback if max iterations reached
        return "Unable to complete reasoning analysis.", total_input_tokens, total_output_tokens

    def _format_janda_results(self, results: list[dict]) -> str:
        """Format Janda search results for context injection."""
        if not results:
            return "No relevant textbook excerpts found."

        sections = []
        for r in results:
            title = r.get("title", "Untitled")
            text = r.get("text", "")[:800]  # Truncate for context window
            source = f"Janda Part {r.get('part', '?')}: {r.get('name', '')}"
            score = r.get("score", 0)

            sections.append(
                f"### {title}\n"
                f"*Source: {source} (relevance: {score:.2f})*\n\n"
                f"{text}\n"
            )

        return "\n---\n".join(sections)

    def _execute_reasoning_tools(self, content: Any) -> list[dict]:
        """Execute tool calls from reasoning pass (search_janda only)."""
        results = []

        for block in content:
            if block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                tool_id = block.id

                if tool_name == "search_janda":
                    query = tool_input.get("query", "")
                    search_results = self.vector_store.search_janda(query, top_k=3)

                    formatted = []
                    for r in search_results:
                        formatted.append({
                            "title": r.get("title", ""),
                            "excerpt": r.get("text", "")[:800],
                            "source": f"Janda Part {r.get('part', '?')}: {r.get('name', '')}",
                            "relevance": round(r.get("score", 0), 3)
                        })
                    result_content = json.dumps(formatted, indent=2)
                else:
                    result_content = json.dumps({"error": f"Unknown tool: {tool_name}"})

                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_content
                })

        return results

    def _generate_hand_agentic(self, request: HandInsightRequest, user_prompt: str) -> InsightResponse:
        """Generate hand insight using agentic RAG with tool calling."""
        messages = [{"role": "user", "content": user_prompt}]
        tools = [SEARCH_CONCEPTS_TOOL, SEARCH_TEXTBOOK_TOOL, SEARCH_TERMS_TOOL]

        total_input_tokens = 0
        total_output_tokens = 0
        max_iterations = 5

        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=150,
                system=HAND_ANALYSIS_SYSTEM_PROMPT,
                tools=tools,
                messages=messages
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "end_turn":
                raw_text = ""
                for block in response.content:
                    if block.type == "text":
                        raw_text = block.text.strip()
                        break

                # Extract marked terms {{term-id}} and get clean text
                insight_text, marked_terms = self._extract_marked_terms(raw_text)

                # Also run auto-detect as fallback
                extractor = get_term_extractor()
                auto_terms = extractor.extract(insight_text)

                # Merge: marked terms take priority
                terms = {**auto_terms, **marked_terms}

                return InsightResponse(
                    insight=insight_text,
                    model_used=self.model,
                    prompt_tokens=total_input_tokens,
                    completion_tokens=total_output_tokens,
                    terms=terms,
                )

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = self._execute_tools(response.content)
                messages.append({"role": "user", "content": tool_results})

        return InsightResponse(
            insight="Unable to generate insight for this hand.",
            model_used=self.model,
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
        )

    def _generate_agentic(self, request: InsightRequest, user_prompt: str) -> InsightResponse:
        """
        Generate insight using agentic RAG with tool calling.

        Claude decides what to search and when based on the situation.
        """
        messages = [{"role": "user", "content": user_prompt}]
        tools = [SEARCH_CONCEPTS_TOOL, SEARCH_TEXTBOOK_TOOL, SEARCH_TERMS_TOOL]

        total_input_tokens = 0
        total_output_tokens = 0
        max_iterations = 5

        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=300,
                system=AGENTIC_SYSTEM_PROMPT,
                tools=tools,
                messages=messages
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            # Check if we're done (model produced final text)
            if response.stop_reason == "end_turn":
                # Extract text from response
                raw_text = ""
                for block in response.content:
                    if block.type == "text":
                        raw_text = block.text
                        break

                # Parse JSON response to extract insight and terms
                insight_text = raw_text
                claude_terms = {}

                # Try to extract JSON from the response (may have preamble text)
                # Find the first '{' and try parsing from there
                json_start = raw_text.find('{')
                if json_start != -1:
                    json_text = raw_text[json_start:]
                    try:
                        parsed = json.loads(json_text)
                        if isinstance(parsed, dict) and "insight" in parsed:
                            insight_text = parsed.get("insight", raw_text)
                            claude_terms = parsed.get("terms", {})
                            if not isinstance(claude_terms, dict):
                                claude_terms = {}
                    except json.JSONDecodeError:
                        # JSON didn't parse cleanly, use raw text
                        pass

                # Always extract terms from the insight text
                extractor = get_term_extractor()
                extracted_terms = extractor.extract(insight_text)

                # Merge: extracted terms first, then Claude's explicit terms override
                terms = {**extracted_terms, **claude_terms}

                return InsightResponse(
                    insight=insight_text,
                    model_used=self.model,
                    prompt_tokens=total_input_tokens,
                    completion_tokens=total_output_tokens,
                    terms=terms,
                )

            # Handle tool use
            if response.stop_reason == "tool_use":
                # Add assistant message with tool calls
                messages.append({"role": "assistant", "content": response.content})

                # Execute tools and build results
                tool_results = self._execute_tools(response.content)
                messages.append({"role": "user", "content": tool_results})

        # Fallback if max iterations reached
        return InsightResponse(
            insight="Unable to generate insight for this situation.",
            model_used=self.model,
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
        )

    def _execute_tools(self, content) -> list[dict]:
        """Execute tool calls and return results."""
        results = []

        for block in content:
            if block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                tool_id = block.id

                if tool_name == "search_concepts":
                    query = tool_input.get("query", "")
                    search_results = self.vector_store.search_concepts(query, top_k=5)
                    # Format results for Claude
                    formatted = []
                    for r in search_results:
                        formatted.append({
                            "name": r["name"],
                            "insight": r["insight"],
                            "relevance": round(r["score"], 3)
                        })
                    result_content = json.dumps(formatted, indent=2)

                elif tool_name == "search_textbook":
                    query = tool_input.get("query", "")
                    search_results = self.vector_store.search_textbook(query, top_k=3)
                    # Format results for Claude
                    formatted = []
                    for r in search_results:
                        formatted.append({
                            "chapter": r["chapter"],
                            "excerpt": r["text"][:600],  # Truncate for context window
                            "relevance": round(r["score"], 3)
                        })
                    result_content = json.dumps(formatted, indent=2)

                elif tool_name == "search_terms":
                    query = tool_input.get("query", "")
                    search_results = self.vector_store.search_terms(query, top_k=5)
                    # Format results for Claude
                    formatted = []
                    for r in search_results:
                        formatted.append({
                            "term_id": r["term_id"],
                            "name": r["name"],
                            "definition": r["blurb"],
                            "details": r["body"][:500] if r["body"] else None,
                            "relevance": round(r["score"], 3)
                        })
                    result_content = json.dumps(formatted, indent=2)

                elif tool_name == "search_janda":
                    query = tool_input.get("query", "")
                    search_results = self.vector_store.search_janda(query, top_k=3)
                    # Format results for Claude
                    formatted = []
                    for r in search_results:
                        formatted.append({
                            "title": r.get("title", ""),
                            "excerpt": r.get("text", "")[:800],
                            "source": f"Janda Part {r.get('part', '?')}: {r.get('name', '')}",
                            "relevance": round(r.get("score", 0), 3)
                        })
                    result_content = json.dumps(formatted, indent=2)

                else:
                    result_content = json.dumps({"error": f"Unknown tool: {tool_name}"})

                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_content
                })

        return results

    def generate_batch(self, requests: list[InsightRequest]) -> list[InsightResponse]:
        """
        Generate insights for multiple requests.

        Args:
            requests: List of InsightRequests.

        Returns:
            List of InsightResponses in the same order.
        """
        return [self.generate(req) for req in requests]
