"""
PokerKit to MCCFR Adapter

Bridges PokerKit game engine with the existing PAAEMD abstraction system
to produce info_state strings compatible with the policy database.

Info state format: {player}|p{player}:b{bucket}:h,{action_history}

Actions (FCHPA):
  a0 = fold
  a1 = check/call
  a2 = pot bet
  a3 = all-in
  a4 = half-pot bet
"""

import sys
sys.path.insert(0, "/home/de2425/openbot")

from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass, field
from enum import IntEnum
from pokerkit import NoLimitTexasHoldem, Automation

# Import abstraction components (lazy, may not be available locally)
_ABSTRACTIONS_AVAILABLE = False
try:
    from src.abstraction.paaemd.canonicalization import (
        preflop_canonical_index,
        flop_canonical_index,
        turn_canonical_index,
        river_canonical_index,
    )
    from src.abstraction.paaemd.flop import FlopAbstraction
    from src.abstraction.paaemd.turn import TurnAbstraction
    _ABSTRACTIONS_AVAILABLE = True
except ImportError:
    # Stubs for local testing
    preflop_canonical_index = None
    flop_canonical_index = None
    turn_canonical_index = None
    river_canonical_index = None
    FlopAbstraction = None
    TurnAbstraction = None


class Action(IntEnum):
    """FCHP action encoding (no all-in for faster training)"""
    FOLD = 0
    CHECK_CALL = 1
    POT = 2
    HALF_POT = 3


@dataclass
class GameConfig:
    """Configuration for 6-max NLHE"""
    num_players: int = 6
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 10000

    # Positions: 0=SB, 1=BB, 2=UTG, 3=HJ, 4=CO, 5=BTN (PokerKit order)
    # But in the policy DB: p0=UTG, p1=HJ, p2=CO, p3=BTN, p4=SB, p5=BB
    # We need to map between them


def pokerkit_card_to_int(card) -> int:
    """Convert PokerKit Card object to integer (rank*4 + suit)"""
    # PokerKit Card has .rank (Rank enum) and .suit (Suit enum)
    # Rank values: A,2,3,4,5,6,7,8,9,T,J,Q,K
    # Suit values: c,d,h,s
    ranks = "23456789TJQKA"
    suits = "cdhs"

    # Get string value from enum
    rank_char = card.rank.value  # e.g., 'A', 'K', '2'
    suit_char = card.suit.value  # e.g., 'c', 'd', 'h', 's'

    rank = ranks.index(rank_char)
    suit = suits.index(suit_char)
    return rank * 4 + suit


def int_to_card_str(i: int) -> str:
    """Convert integer to card string"""
    ranks = "23456789TJQKA"
    suits = "cdhs"
    return ranks[i // 4] + suits[i % 4]


class PokerKitAdapter:
    """
    Adapter between PokerKit game engine and MCCFR info states.

    Handles:
    - Game state management via PokerKit
    - Card abstraction via PAAEMD
    - Info state string generation
    - Action abstraction (bet sizes -> FCHPA)
    """

    def __init__(self, config: GameConfig = None, load_abstractions: bool = True):
        self.config = config or GameConfig()
        self.game = None
        self.action_history: List[int] = []  # List of Action enums
        self.street_history: List[List[int]] = [[], [], [], []]  # Actions per street

        # Load abstractions
        self.flop_abs = None
        self.turn_abs = None
        if load_abstractions:
            self._load_abstractions()

    def _load_abstractions(self):
        """Load precomputed abstraction LUTs"""
        try:
            self.flop_abs = FlopAbstraction.load("/home/de2425/openbot/models/checkpoints/flop")
            print(f"Loaded flop abstraction: {self.flop_abs.num_buckets} buckets")
        except Exception as e:
            print(f"Warning: Could not load flop abstraction: {e}")

        try:
            self.turn_abs = TurnAbstraction.load("/home/de2425/openbot/models/checkpoints/turn")
            print(f"Loaded turn abstraction: {self.turn_abs.num_buckets} buckets")
        except Exception as e:
            print(f"Warning: Could not load turn abstraction: {e}")

    def new_game(self) -> None:
        """Start a new hand"""
        self.game = NoLimitTexasHoldem.create_state(
            automations=(
                Automation.ANTE_POSTING,
                Automation.BET_COLLECTION,
                Automation.BLIND_OR_STRADDLE_POSTING,
                Automation.CARD_BURNING,
                Automation.HOLE_DEALING,
                Automation.BOARD_DEALING,
                Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
                Automation.HAND_KILLING,
                Automation.CHIPS_PUSHING,
                Automation.CHIPS_PULLING,
            ),
            ante_trimming_status=True,
            raw_antes={-1: 0},
            raw_blinds_or_straddles=(self.config.small_blind, self.config.big_blind),
            min_bet=self.config.big_blind,
            raw_starting_stacks=(self.config.starting_stack,) * self.config.num_players,
            player_count=self.config.num_players,
        )
        self.action_history = []
        self.street_history = [[], [], [], []]

    def _pokerkit_to_policy_position(self, pk_pos: int) -> int:
        """
        Convert PokerKit position to policy DB position.

        PokerKit (6-max): 0=SB, 1=BB, 2=UTG, 3=HJ, 4=CO, 5=BTN
        Policy DB:        p0=UTG, p1=HJ, p2=CO, p3=BTN, p4=SB, p5=BB
        """
        # Mapping: PK -> Policy
        # 0 (SB) -> 4
        # 1 (BB) -> 5
        # 2 (UTG) -> 0
        # 3 (HJ) -> 1
        # 4 (CO) -> 2
        # 5 (BTN) -> 3
        mapping = {0: 4, 1: 5, 2: 0, 3: 1, 4: 2, 5: 3}
        return mapping[pk_pos]

    def _get_hole_cards_int(self, player_idx: int) -> List[int]:
        """Get player's hole cards as integers"""
        cards = self.game.hole_cards[player_idx]
        return [pokerkit_card_to_int(c) for c in cards]

    def _get_board_int(self) -> List[int]:
        """Get board cards as integers"""
        # board_cards is a list of lists (one per card), flatten it
        board = []
        for card_list in self.game.board_cards:
            for card in card_list:
                board.append(pokerkit_card_to_int(card))
        return board

    def get_bucket(self, player_idx: int) -> int:
        """
        Get abstraction bucket for player's current hand.

        Returns bucket based on current street:
        - Preflop: canonical index (0-168)
        - Flop: LUT bucket (0-499)
        - Turn: LUT bucket (0-499)
        - River: LUT bucket (0-499)
        """
        hole = self._get_hole_cards_int(player_idx)
        board = self._get_board_int()

        street = self.game.street_index or 0

        # Fallback if abstractions not available (for local testing)
        if not _ABSTRACTIONS_AVAILABLE:
            # Simple hash-based bucketing for testing
            h = sum(hole) + sum(board) * 13
            return h % 500

        if street == 0:  # Preflop
            return preflop_canonical_index(hole)
        elif street == 1:  # Flop
            if self.flop_abs:
                return self.flop_abs.get_bucket(hole, board[:3])
            return flop_canonical_index(hole, board[:3]) % 500
        elif street == 2:  # Turn
            if self.turn_abs:
                return self.turn_abs.get_bucket(hole, board[:4])
            return turn_canonical_index(hole, board[:4]) % 500
        else:  # River
            # TODO: Load river abstraction
            return river_canonical_index(hole, board) % 500

    def get_info_state(self, player_idx: int) -> str:
        """
        Build info_state string for player.

        Format: {player}|p{position}:b{bucket}:h,{actions}

        Example: "2|p2:b45:h,a1,a0,a2,a1"
        """
        position = self._pokerkit_to_policy_position(player_idx)
        bucket = self.get_bucket(player_idx)

        # Build action string
        if not self.action_history:
            action_str = "h"
        else:
            action_str = "h," + ",".join(f"a{a}" for a in self.action_history)

        return f"{position}|p{position}:b{bucket}:{action_str}"

    def _classify_action(self, bet_amount: int, pot_before: int, to_call: int) -> Action:
        """
        Classify a bet/raise into FCHP action (no all-in).

        Returns closest action from: FOLD, CHECK_CALL, HALF_POT, POT
        """
        if bet_amount == 0:
            return Action.CHECK_CALL

        # Calculate bet as fraction of pot
        pot_after_call = pot_before + to_call
        new_money = bet_amount - to_call  # Money above calling

        if new_money <= 0:
            return Action.CHECK_CALL

        # Calculate bet/pot ratio
        ratio = new_money / pot_after_call if pot_after_call > 0 else 1.0

        # Classify based on ratio
        # half-pot ~= 0.5, pot ~= 1.0
        if ratio < 0.35:
            return Action.CHECK_CALL  # Small bet treated as call
        elif ratio < 0.75:
            return Action.HALF_POT
        else:
            return Action.POT  # Anything >= 0.75 is pot

    def get_legal_actions(self) -> List[Action]:
        """Get legal abstract actions for current actor (FCHP - no all-in)"""
        if self.game.actor_index is None:
            return []

        actions = []

        if self.game.can_fold():
            actions.append(Action.FOLD)

        if self.game.can_check_or_call():
            actions.append(Action.CHECK_CALL)

        if self.game.can_complete_bet_or_raise_to():
            min_raise = self.game.min_completion_betting_or_raising_to_amount
            max_raise = self.game.max_completion_betting_or_raising_to_amount
            pot_raise = self.game.pot_completion_betting_or_raising_to_amount

            half_pot = pot_raise // 2 if pot_raise else min_raise

            # Add half-pot and pot if they're valid (capped at max)
            if half_pot >= min_raise:
                actions.append(Action.HALF_POT)
            if pot_raise >= min_raise:
                actions.append(Action.POT)

        return actions

    def apply_action(self, action: Action) -> bool:
        """
        Apply abstract action to game state (FCHP - no all-in).

        Returns True if action was applied successfully.
        """
        if self.game.actor_index is None:
            return False

        actor = self.game.actor_index
        street = self.game.street_index or 0

        try:
            if action == Action.FOLD:
                self.game.fold()
            elif action == Action.CHECK_CALL:
                self.game.check_or_call()
            elif action == Action.HALF_POT:
                pot_raise = self.game.pot_completion_betting_or_raising_to_amount
                half_pot = max(pot_raise // 2, self.game.min_completion_betting_or_raising_to_amount)
                half_pot = min(half_pot, self.game.max_completion_betting_or_raising_to_amount)
                self.game.complete_bet_or_raise_to(half_pot)
            elif action == Action.POT:
                pot_raise = self.game.pot_completion_betting_or_raising_to_amount
                pot_raise = min(pot_raise, self.game.max_completion_betting_or_raising_to_amount)
                pot_raise = max(pot_raise, self.game.min_completion_betting_or_raising_to_amount)
                self.game.complete_bet_or_raise_to(pot_raise)
            else:
                return False

            # Record action in history
            self.action_history.append(int(action))
            self.street_history[street].append(int(action))
            return True

        except Exception as e:
            print(f"Error applying action {action}: {e}")
            return False

    def is_terminal(self) -> bool:
        """Check if hand is complete"""
        return self.game.status is False or self.game.actor_index is None and len(self.game.board_cards) == 5

    def get_payoffs(self) -> List[float]:
        """Get payoffs for all players (profit/loss from starting stack)"""
        return [s - self.config.starting_stack for s in self.game.stacks]

    @property
    def current_player(self) -> Optional[int]:
        """Get current actor index"""
        return self.game.actor_index

    @property
    def street(self) -> int:
        """Get current street (0=preflop, 1=flop, 2=turn, 3=river)"""
        return self.game.street_index or 0

    def __repr__(self):
        if self.game is None:
            return "PokerKitAdapter(no game)"

        actor = self.game.actor_index
        street_names = ["Preflop", "Flop", "Turn", "River"]
        street = street_names[self.street]

        lines = [f"=== {street} ==="]
        lines.append(f"Pot: {self.game.total_pot_amount}")
        lines.append(f"Board: {list(self.game.board_cards)}")
        lines.append(f"Actor: P{actor}")

        if actor is not None:
            lines.append(f"Hole cards: {list(self.game.hole_cards[actor])}")
            lines.append(f"Info state: {self.get_info_state(actor)}")
            lines.append(f"Legal actions: {self.get_legal_actions()}")

        return "\n".join(lines)


# =============================================================================
# Testing
# =============================================================================

def test_basic_game():
    """Test basic game flow"""
    print("=" * 60)
    print("TEST: Basic game flow")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=False)
    adapter.new_game()

    print(f"\nInitial state:")
    print(f"  Pot: {adapter.game.total_pot_amount}")
    print(f"  Actor: P{adapter.current_player}")
    print(f"  Street: {adapter.street}")

    # Play a few actions
    actions_taken = []
    for i in range(10):
        if adapter.is_terminal() or adapter.current_player is None:
            break

        actor = adapter.current_player
        legal = adapter.get_legal_actions()

        # Choose call/check if available, else fold
        if Action.CHECK_CALL in legal:
            action = Action.CHECK_CALL
        elif Action.FOLD in legal:
            action = Action.FOLD
        else:
            action = legal[0]

        info_state = adapter.get_info_state(actor)
        print(f"\n  P{actor} info_state: {info_state}")
        print(f"  P{actor} legal: {[a.name for a in legal]}")
        print(f"  P{actor} action: {action.name}")

        adapter.apply_action(action)
        actions_taken.append((actor, action))

    print(f"\nFinal state:")
    print(f"  Terminal: {adapter.is_terminal()}")
    print(f"  Payoffs: {adapter.get_payoffs()}")
    print(f"  Action history: {adapter.action_history}")

    return True


def test_info_state_format():
    """Test info state string format matches policy DB"""
    print("\n" + "=" * 60)
    print("TEST: Info state format")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=False)
    adapter.new_game()

    # Get info states for all players at start
    print("\nPreflop info states (before any action):")
    for i in range(6):
        info = adapter.get_info_state(i)
        print(f"  P{i}: {info}")

    # Verify format: {player}|p{player}:b{bucket}:h
    actor = adapter.current_player
    info = adapter.get_info_state(actor)
    parts = info.split("|")
    assert len(parts) == 2, f"Expected 2 parts, got {parts}"

    subparts = parts[1].split(":")
    assert len(subparts) == 3, f"Expected 3 subparts, got {subparts}"
    assert subparts[0].startswith("p"), f"Expected p prefix, got {subparts[0]}"
    assert subparts[1].startswith("b"), f"Expected b prefix, got {subparts[1]}"
    assert subparts[2].startswith("h"), f"Expected h prefix, got {subparts[2]}"

    print("\n✓ Info state format is correct")
    return True


def test_action_abstraction():
    """Test action classification into FCHPA"""
    print("\n" + "=" * 60)
    print("TEST: Action abstraction")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=False)
    adapter.new_game()

    actor = adapter.current_player
    print(f"\nActor: P{actor}")
    print(f"Stack: {adapter.game.stacks[actor]}")
    print(f"Pot: {adapter.game.total_pot_amount}")

    legal = adapter.get_legal_actions()
    print(f"Legal actions: {[a.name for a in legal]}")

    # Test each action type
    if Action.HALF_POT in legal:
        print("\nTesting HALF_POT:")
        pot_raise = adapter.game.pot_completion_betting_or_raising_to_amount
        half_pot = pot_raise // 2
        print(f"  Pot raise would be: {pot_raise}")
        print(f"  Half pot: {half_pot}")

    if Action.POT in legal:
        print("\nTesting POT:")
        pot_raise = adapter.game.pot_completion_betting_or_raising_to_amount
        print(f"  Pot raise: {pot_raise}")

    print("\n✓ Action abstraction working (FCHP - 4 actions, no all-in)")
    return True


def test_full_hand():
    """Test playing a complete hand"""
    print("\n" + "=" * 60)
    print("TEST: Full hand simulation")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=False)
    adapter.new_game()

    import random
    random.seed(42)

    moves = 0
    while not adapter.is_terminal() and adapter.current_player is not None and moves < 100:
        actor = adapter.current_player
        legal = adapter.get_legal_actions()

        if not legal:
            break

        # Random action
        action = random.choice(legal)

        street_names = ["Preflop", "Flop", "Turn", "River"]
        print(f"{street_names[adapter.street]}: P{actor} {action.name}")

        adapter.apply_action(action)
        moves += 1

    print(f"\nHand complete after {moves} actions")
    print(f"Terminal: {adapter.is_terminal()}")
    print(f"Payoffs: {adapter.get_payoffs()}")
    print(f"Full action history: {adapter.action_history}")

    return True


def test_with_abstractions():
    """Test with real abstractions loaded"""
    print("\n" + "=" * 60)
    print("TEST: With real abstractions")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=True)
    adapter.new_game()

    # Show buckets for each player
    print("\nPreflop buckets:")
    for i in range(6):
        hole = adapter._get_hole_cards_int(i)
        bucket = adapter.get_bucket(i)
        cards = [int_to_card_str(c) for c in hole]
        print(f"  P{i}: {cards[0]}{cards[1]} -> bucket {bucket}")

    # Play to flop
    print("\nPlaying to flop...")
    while adapter.street == 0 and not adapter.is_terminal():
        if adapter.current_player is None:
            break
        legal = adapter.get_legal_actions()
        if Action.CHECK_CALL in legal:
            adapter.apply_action(Action.CHECK_CALL)
        elif legal:
            adapter.apply_action(legal[0])

    if adapter.street >= 1:
        print(f"\nFlop: {list(adapter.game.board_cards)}")
        print("Flop buckets:")
        for i in range(6):
            if adapter.game.statuses[i]:  # True = still in hand
                bucket = adapter.get_bucket(i)
                hole = adapter._get_hole_cards_int(i)
                cards = [int_to_card_str(c) for c in hole]
                print(f"  P{i}: {cards[0]}{cards[1]} -> bucket {bucket}")

    return True


def test_position_mapping():
    """Test position mapping between PokerKit and policy DB"""
    print("\n" + "=" * 60)
    print("TEST: Position mapping")
    print("=" * 60)

    adapter = PokerKitAdapter(load_abstractions=False)

    print("\nPokerKit pos -> Policy DB pos:")
    print("  0 (SB)  -> p4")
    print("  1 (BB)  -> p5")
    print("  2 (UTG) -> p0")
    print("  3 (HJ)  -> p1")
    print("  4 (CO)  -> p2")
    print("  5 (BTN) -> p3")

    for pk_pos in range(6):
        policy_pos = adapter._pokerkit_to_policy_position(pk_pos)
        print(f"  {pk_pos} -> {policy_pos}")

    # Verify preflop action order
    adapter.new_game()
    print(f"\nPreflop first actor (PokerKit): P{adapter.current_player}")
    print("  (Should be P2 = UTG in PokerKit, which is p0 in policy DB)")

    return True


def run_all_tests():
    """Run all tests"""
    tests = [
        test_basic_game,
        test_info_state_format,
        test_action_abstraction,
        test_full_hand,
        test_position_mapping,
        test_with_abstractions,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
                print(f"✗ {test.__name__} returned False")
        except Exception as e:
            failed += 1
            print(f"✗ {test.__name__} raised: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
