"""Main preflop grading logic."""

import logging
from typing import Optional, TYPE_CHECKING

from .models import Grade, GradedDecision
from .range_lookup import RangeLookup, POSITIONS_6MAX
from .confidence import calculate_confidence

if TYPE_CHECKING:
    from ...persistence import FirestoreClient

logger = logging.getLogger(__name__)

# Confidence threshold for flagging mistakes. Anything below this is downgraded
# to GOOD ("low confidence — not enough data to evaluate") so we only surface
# clear-cut errors. Tightened from 0.80 on 2026-05-17 because the 0.80 floor
# was letting marginal spots through alongside genuine blunders, diluting the
# top-3 list.
CONFIDENCE_THRESHOLD = 0.85


def get_position_from_seat(seat_index: int, button_seat: int, num_seats: int) -> str:
    """
    Calculate position name from seat index.

    Args:
        seat_index: Player's seat index (0-based)
        button_seat: Button's seat index
        num_seats: Total number of seats at the table

    Returns:
        Position name like "BTN", "SB", "BB", "UTG", etc.
    """
    # Calculate position relative to button (0 = BTN, 1 = SB, etc.)
    relative_pos = (seat_index - button_seat) % num_seats

    # Map relative position to name based on table size
    if num_seats == 2:
        # Heads-up: BTN/SB and BB
        positions = ["BTN", "BB"]
    elif num_seats <= 6:
        # 6-max or smaller
        positions = ["BTN", "SB", "BB", "UTG", "HJ", "CO"][:num_seats]
    else:
        # 8-max or larger
        positions = ["BTN", "SB", "BB", "UTG", "UTG1", "LJ", "HJ", "CO"][:num_seats]

    if relative_pos < len(positions):
        return positions[relative_pos]
    return f"P{relative_pos}"


class PreflopGrader:
    """Grades preflop decisions against GTO ranges."""

    def __init__(self, firestore_client: "FirestoreClient"):
        self.range_lookup = RangeLookup(firestore_client)

    def _calculate_effective_stack_bb(self, seats: list[dict], big_blind: int) -> float:
        """
        Calculate effective stack size in big blinds.

        For heads-up or multiway, this is the minimum of all players' starting stacks.
        This determines which stack-size-specific ranges to use.

        Args:
            seats: List of seat data with starting_stack
            big_blind: Big blind in cents

        Returns:
            Effective stack in big blinds
        """
        if not seats or big_blind <= 0:
            return 100.0  # Default to deep stack

        stacks = []
        for seat in seats:
            stack = seat.get("starting_stack", 0)
            if stack > 0:
                stacks.append(stack)

        if not stacks:
            return 100.0

        effective_stack_cents = min(stacks)
        return effective_stack_cents / big_blind

    def grade_hand(self, hand_log: dict, user_id: str) -> list[GradedDecision]:
        """
        Grade all preflop decisions for a user in a hand.

        Args:
            hand_log: Hand data from Firestore
            user_id: The user to grade

        Returns:
            List of graded decisions (may be empty if no gradable actions)
        """
        decisions = []

        # Extract hand info
        actions = hand_log.get("actions", [])
        seats = hand_log.get("seats", [])
        hole_cards = hand_log.get("hole_cards", {})
        button_seat = hand_log.get("button_seat", 0)
        big_blind = hand_log.get("big_blind", 200)  # cents
        hand_id = hand_log.get("hand_id", "")

        # Find user's seat
        user_seat = None
        for seat in seats:
            if seat.get("user_id") == user_id:
                user_seat = seat.get("seat_index")
                break

        if user_seat is None:
            return decisions

        # Get user's hole cards
        user_hand = hole_cards.get(str(user_seat))
        if not user_hand:
            logger.debug(f"[GRADER] No hole cards for user seat {user_seat}")
            return decisions

        # Convert to string format
        if isinstance(user_hand, list):
            user_hand = "".join(user_hand)

        num_seats = len(seats)
        user_position = get_position_from_seat(user_seat, button_seat, num_seats)

        # Calculate effective stack in BB for stack-size-dependent ranges
        effective_bb = self._calculate_effective_stack_bb(seats, big_blind)

        # Build the preflop action sequence
        preflop_sequence = self._extract_preflop_sequence(actions, seats, button_seat)

        # Grade each of the user's preflop decisions
        for i, action in enumerate(actions):
            # Only grade preflop
            if action.get("street") != "preflop":
                continue

            # Only grade user's decisions
            if action.get("seat") != user_seat:
                continue

            # Skip blinds
            action_type = action.get("action", "").lower()
            if action_type == "post_blind":
                continue

            # Build spot context for this decision
            prior_actions = preflop_sequence[:self._find_action_index(preflop_sequence, i, actions)]
            grade = self._grade_action(
                action=action,
                hand=user_hand,
                position=user_position,
                prior_actions=prior_actions,
                hand_id=hand_id,
                big_blind=big_blind,
                num_seats=num_seats,
                effective_bb=effective_bb
            )

            if grade:
                decisions.append(grade)

        return decisions

    def _extract_preflop_sequence(
        self,
        actions: list[dict],
        seats: list[dict],
        button_seat: int
    ) -> list[dict]:
        """
        Extract the preflop action sequence with position info.

        Returns list of dicts with:
            - action: "fold", "call", "raise", etc.
            - position: "UTG", "BTN", etc.
            - amount: sizing in cents (if applicable)
            - original_index: index in full actions list
        """
        sequence = []
        num_seats = len(seats)

        for i, action in enumerate(actions):
            if action.get("street") != "preflop":
                continue

            action_type = action.get("action", "").lower()
            if action_type == "post_blind":
                continue

            seat = action.get("seat")
            position = get_position_from_seat(seat, button_seat, num_seats)

            sequence.append({
                "action": action_type,
                "position": position,
                "amount": action.get("amount"),
                "original_index": i
            })

        return sequence

    def _find_action_index(
        self,
        preflop_sequence: list[dict],
        action_index: int,
        all_actions: list[dict]
    ) -> int:
        """Find index in preflop_sequence for a given action index."""
        for i, seq_action in enumerate(preflop_sequence):
            if seq_action.get("original_index") == action_index:
                return i
        return len(preflop_sequence)

    def _build_spot_path(self, prior_actions: list[dict], num_seats: int = 8, effective_bb: float = 100) -> list[str]:
        """
        Build the Firestore spot path from prior actions.

        Args:
            prior_actions: Actions that happened before user's decision
            num_seats: Number of seats at the table (2 for HU)
            effective_bb: Effective stack size in big blinds

        Returns:
            Spot path like ["BTN_RFI", "SB_3B"] or [] for RFI
        """
        if not prior_actions:
            return []

        path = []

        # Find the opener (first raise)
        opener_pos = None
        for action in prior_actions:
            action_type = action["action"].lower()  # Normalize to lowercase
            if action_type in ("bet", "raise", "raise_to"):
                opener_pos = self.range_lookup.map_position(action["position"], num_seats)
                path.append(f"{opener_pos}_RFI")
                break

        if not opener_pos:
            return []

        # Find subsequent raises (3-bets, 4-bets)
        raise_count = 1  # We already counted the open
        for action in prior_actions:
            action_type = action["action"].lower()
            if action_type in ("bet", "raise", "raise_to"):
                if raise_count == 1:
                    # This is the open, already handled
                    raise_count += 1
                    continue

                pos = self.range_lookup.map_position(action["position"], num_seats)
                if raise_count == 2:
                    path.append(f"{pos}_3B")
                elif raise_count == 3:
                    path.append(f"{pos}_4B")
                raise_count += 1
            elif action_type == "call":
                pos = self.range_lookup.map_position(action["position"], num_seats)
                path.append(f"{pos}_C")

        return path

    def _grade_action(
        self,
        action: dict,
        hand: str,
        position: str,
        prior_actions: list[dict],
        hand_id: str,
        big_blind: int,
        num_seats: int = 8,
        effective_bb: float = 100
    ) -> Optional[GradedDecision]:
        """
        Grade a single preflop action.

        Args:
            action: The action to grade
            hand: User's hole cards
            position: User's position
            prior_actions: Actions before this one
            hand_id: Hand ID for reference
            big_blind: Big blind in cents
            num_seats: Number of seats at the table (2 for HU)
            effective_bb: Effective stack size in big blinds

        Returns:
            GradedDecision or None if can't be graded
        """
        action_type = action.get("action", "").lower()
        amount = action.get("amount", 0)

        # Normalize action types
        if action_type in ("bet", "raise_to", "raise"):
            action_taken = "Raise"
        elif action_type == "call":
            action_taken = "Call"
        elif action_type == "fold":
            action_taken = "Fold"
        else:
            return None

        # Detect limpers ahead of hero. A "limper" is specifically a call that
        # occurs BEFORE any raise in the preflop sequence — a call after a
        # raise is a cold-call, not a limp. This distinction matters because
        # cold-call spots have their own nodes in the GTO tree
        # (BTN_RFI → SB_C → BB_?), but limp/iso/overlimp spots are not in the
        # tree at all.
        has_limper_before_hero = False
        for a in prior_actions:
            act = a["action"]
            if act == "call":
                has_limper_before_hero = True
                break
            if act in ("bet", "raise", "raise_to"):
                break

        # Skip grading any decision that follows a limp. We don't have iso /
        # overlimp / fold-vs-limp solver data, and the previous code grafted
        # these onto the RFI tree — which then graded every overlimp as a
        # MISTAKE ("Call X is never correct, GTO: Raise"). That was a false
        # positive: the call frequency wasn't 0%, we just had no data.
        # Returning None here keeps these spots out of the user-facing
        # mistake count entirely until proper limp/iso ranges are loaded.
        if has_limper_before_hero:
            logger.debug(
                f"[GRADER] Skipping {position} {action_taken} {hand}: limper(s) "
                f"ahead, no solver data for limp/iso spots"
            )
            return None

        # Determine if this is RFI or facing action
        # RFI = everyone folded before us
        has_raise_before = any(a["action"] in ("raise", "raise_to", "bet") for a in prior_actions)
        is_rfi = len(prior_actions) == 0 or all(a["action"] == "fold" for a in prior_actions)
        has_limper = False  # always False here — limp spots returned above

        # Build a display-friendly action label that captures the level of the
        # decision (open, 3bet, call-vs-3bet, limp, etc.). action_taken stays
        # canonical for GTO lookup; action_label is what we surface to users.
        prior_raise_count = sum(
            1 for a in prior_actions if a["action"] in ("bet", "raise", "raise_to")
        )
        is_hu = num_seats == 2
        # HU prefix: position-stamp every label so BTN open vs BB defend is
        # unambiguous in the post-session summary. (6-max keeps the original
        # generic labels — position is already shown alongside the label.)
        hu_prefix = f"{position} " if is_hu else ""
        if action_taken == "Raise":
            if prior_raise_count == 0:
                if has_limper:
                    action_label = f"{hu_prefix}iso-raise" if is_hu else "Iso-raise"
                else:
                    action_label = f"{hu_prefix}open" if is_hu else "Open"
            elif prior_raise_count == 1:
                action_label = f"{hu_prefix}3bet" if is_hu else "3bet"
            elif prior_raise_count == 2:
                action_label = f"{hu_prefix}4bet" if is_hu else "4bet"
            else:
                action_label = f"{hu_prefix}{prior_raise_count + 1}bet" if is_hu else f"{prior_raise_count + 1}bet"
        elif action_taken == "Call":
            if prior_raise_count == 0:
                action_label = f"{hu_prefix}limp" if is_hu else "Limp"
            elif prior_raise_count == 1:
                # HU: BB facing BTN open is a "defend", not a generic "Call vs Open"
                if is_hu and position == "BB":
                    action_label = "BB defend"
                else:
                    action_label = f"{hu_prefix}call vs open" if is_hu else "Call vs Open"
            elif prior_raise_count == 2:
                action_label = f"{hu_prefix}call vs 3bet" if is_hu else "Call vs 3bet"
            elif prior_raise_count == 3:
                action_label = f"{hu_prefix}call vs 4bet" if is_hu else "Call vs 4bet"
            else:
                action_label = f"{hu_prefix}call vs {prior_raise_count}-bet" if is_hu else f"Call vs {prior_raise_count}-bet"
        else:  # Fold
            if prior_raise_count == 0:
                action_label = f"{hu_prefix}fold" if is_hu else "Fold"
            elif prior_raise_count == 1:
                action_label = f"{hu_prefix}fold vs open" if is_hu else "Fold vs Open"
            elif prior_raise_count == 2:
                action_label = f"{hu_prefix}fold vs 3bet" if is_hu else "Fold vs 3bet"
            else:
                action_label = f"{hu_prefix}fold vs raise" if is_hu else "Fold vs raise"

        # Debug logging (uncomment for debugging)
        # if has_raise_before and action_type in ("bet", "raise_to", "raise"):
        #     print(f"[GRADER] 3bet+ detected: {position} {hand}, prior_actions={prior_actions}")

        # Get GTO frequencies
        mapped_position = self.range_lookup.map_position(position, num_seats)
        if is_rfi:
            gto_freqs = self.range_lookup.get_rfi_frequencies(position, hand, num_seats, effective_bb)
            spot_path = []
            tree_sizing = 2.5  # Standard open size
        else:
            spot_path = self._build_spot_path(prior_actions, num_seats, effective_bb)
            gto_freqs = self.range_lookup.get_gto_frequencies(position, spot_path, hand, num_seats, effective_bb)
            # Get sizing for the action, not the current spot
            tree_sizing = 0  # Default, will be set based on action

        if not gto_freqs:
            logger.debug(f"[GRADER] No GTO data for {position} at {spot_path}")
            return None

        # Get frequency for the action taken
        action_frequency = gto_freqs.get(action_taken, 0)

        # Calculate game sizing in BB (our action amount)
        game_sizing_bb = (amount / big_blind) if amount and big_blind else 0

        # Calculate facing sizing (the bet we're responding to)
        facing_sizing_bb = 0
        if prior_actions:
            # Find the last raise we're facing
            for pa in reversed(prior_actions):
                if pa["action"] in ("bet", "raise", "raise_to") and pa.get("amount"):
                    facing_sizing_bb = pa["amount"] / big_blind
                    break

        # Get tree sizing for comparison
        if action_taken == "Raise" and not is_rfi:
            # For 3-bet/4-bet, get our expected raise sizing
            num_raises = sum(1 for a in prior_actions if a["action"] in ("bet", "raise", "raise_to"))
            if num_raises == 1:
                raise_path = spot_path + [f"{mapped_position}_3B"]
            elif num_raises == 2:
                raise_path = spot_path + [f"{mapped_position}_4B"]
            else:
                raise_path = spot_path
            tree_sizing = self.range_lookup.get_spot_sizing(raise_path, num_seats, effective_bb)
        elif action_taken in ("Fold", "Call") and spot_path:
            # For fold/call, tree_sizing is what bet sizing the solver studied
            tree_sizing = self.range_lookup.get_spot_sizing(spot_path, num_seats, effective_bb)
        else:
            tree_sizing = 0

        # Calculate confidence with action-aware sizing logic
        spot_depth = len(spot_path)
        confidence = calculate_confidence(
            game_sizing_bb=game_sizing_bb,
            tree_sizing_bb=tree_sizing,
            gto_frequency=action_frequency,
            spot_depth=spot_depth,
            action_taken=action_taken,
            facing_sizing_bb=facing_sizing_bb
        )

        # Debug logging (uncomment for debugging)
        # if action_taken == "Raise" and spot_depth > 0:
        #     print(f"[GRADER] {position} {action_taken} {hand} at {spot_path}: "
        #           f"freq={action_frequency:.2f}, conf={confidence:.2f}, gto={gto_freqs}")

        # Determine grade
        grade, reasoning = self._determine_grade(
            action_taken=action_taken,
            action_label=action_label,
            gto_frequency=action_frequency,
            gto_freqs=gto_freqs,
            confidence=confidence,
            hand=hand,
            position=position,
        )

        spot_path_str = "/".join(spot_path) if spot_path else "RFI"

        return GradedDecision(
            hand_id=hand_id,
            street="preflop",
            action_taken=action_taken,
            gto_frequency=action_frequency,
            confidence=confidence,
            grade=grade,
            reasoning=reasoning,
            position=position,
            hand=hand,
            spot_path=spot_path_str,
            action_label=action_label,
        )

    def _determine_grade(
        self,
        action_taken: str,
        action_label: str,
        gto_frequency: float,
        gto_freqs: dict[str, float],
        confidence: float,
        hand: str,
        position: str
    ) -> tuple[Grade, str]:
        """
        Determine the grade and reasoning for an action.

        Returns:
            Tuple of (Grade, reasoning string)
        """
        # FIRST: Check for clear mistakes (0% frequency) - these are mistakes
        # regardless of confidence level
        if gto_frequency == 0:
            # Find what they should have done
            best_action = max(gto_freqs.items(), key=lambda x: x[1])
            return (
                Grade.MISTAKE,
                f"{action_label} {hand} from {position} is never correct "
                f"(GTO: {best_action[0]} {best_action[1]:.0%})"
            )

        # Check if they folded a hand that's always played (100% frequency)
        if action_taken == "Fold":
            non_fold_freqs = {k: v for k, v in gto_freqs.items() if k != "Fold"}
            if non_fold_freqs:
                total_play_freq = sum(non_fold_freqs.values())
                if total_play_freq >= 0.99:  # Always play this hand
                    best_action = max(non_fold_freqs.items(), key=lambda x: x[1])
                    return (
                        Grade.MISTAKE,
                        f"Folding {hand} from {position} is a mistake "
                        f"(GTO: {best_action[0]} {best_action[1]:.0%})"
                    )

        # Check if they raised a hand that should always be folded
        if action_taken == "Raise":
            fold_freq = gto_freqs.get("Fold", 0)
            if fold_freq >= 0.99:  # Always fold this hand
                return (
                    Grade.MISTAKE,
                    f"{action_label} {hand} from {position} is a mistake "
                    f"(GTO: Fold 100%)"
                )

        # For marginal plays, apply confidence threshold
        if confidence < CONFIDENCE_THRESHOLD:
            return (
                Grade.GOOD,
                f"Low confidence ({confidence:.2f}) - not enough data to evaluate"
            )

        # Action is acceptable
        return (
            Grade.GOOD,
            f"{action_label} {hand} from {position} is acceptable "
            f"(GTO: {gto_frequency:.0%})"
        )
