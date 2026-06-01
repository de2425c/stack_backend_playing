"""Convert Firestore hand documents to InsightRequest format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .schema import InsightRequest, HandInsightRequest, HeroDecision, StreetAction


# Position order from button (6-max)
POSITIONS_6MAX = ["BTN", "SB", "BB", "UTG", "HJ", "CO"]
POSITIONS_2MAX = ["BTN", "BB"]  # Heads-up


def get_position(seat: int, button: int, n_players: int) -> str:
    """Get position name from seat index."""
    pos_idx = (seat - button) % n_players
    if pos_idx < len(POSITIONS_6MAX):
        return POSITIONS_6MAX[pos_idx]
    return f"P{pos_idx}"


def parse_action_code(code: str, pot_bb: float, stack_bb: float, bb_cents: int, facing_bet: bool = False) -> str:
    """Convert action code to human-readable format.

    Args:
        facing_bet: Whether hero is facing an open bet (affects Check vs Call)
    """
    # Specific action codes
    if code == "c":
        return "Call" if facing_bet else "Check"
    if code == "f":
        return "Fold"
    if code.startswith("b"):
        # Bet amount in cents, e.g., "b996"
        amount = int(code[1:])
        amount_bb = amount / bb_cents
        return f"Bet {amount_bb:.1f}bb"
    if code.startswith("r"):
        # Raise to amount in cents
        amount = int(code[1:])
        amount_bb = amount / bb_cents
        return f"Raise {amount_bb:.1f}bb"

    # Numeric codes (0-4 style)
    if code == "0":
        return "Fold"
    if code == "1":
        return "Call" if facing_bet else "Check"
    if code == "2":
        return "Bet 33%"
    if code == "3":
        return "Bet 75%"
    if code == "4":
        return "Bet 125%"

    return code


def format_action(action: dict, bb_cents: int, positions: dict[int, str], has_bet_this_street: bool = False, street: str = "") -> str:
    """Format a single action for display.

    Args:
        has_bet_this_street: Whether there's been a bet on this street already.
                            Used to distinguish "bets" from "raises".
        street: Current street (preflop actions are always "raises" since blinds are posted).
    """
    seat = action["seat"]
    pos = positions.get(seat, f"Seat{seat}")
    act = action["action"]
    amount = action.get("amount")
    is_preflop = street == "preflop" or action.get("street") == "preflop"
    all_in = bool(action.get("is_all_in")) or act == "all_in"
    suffix = " (all-in)" if all_in else ""

    if act == "post_blind":
        if amount == bb_cents // 2:
            return f"{pos} posts SB"
        return f"{pos} posts BB"
    elif act == "fold":
        return f"{pos} folds"
    elif act == "check":
        return f"{pos} checks"
    elif act == "call":
        return f"{pos} calls{suffix}"
    elif act == "bet":
        amount_bb = (amount or 0) / bb_cents
        if is_preflop:
            return f"{pos} raises to {amount_bb:.1f}bb{suffix}"
        return f"{pos} bets {amount_bb:.1f}bb{suffix}"
    elif act == "raise_to":
        amount_bb = (amount or 0) / bb_cents
        if is_preflop or has_bet_this_street:
            return f"{pos} raises to {amount_bb:.1f}bb{suffix}"
        return f"{pos} bets {amount_bb:.1f}bb{suffix}"
    elif act == "all_in":
        amount_bb = (amount or 0) / bb_cents
        return f"{pos} all-in {amount_bb:.1f}bb"

    return f"{pos} {act}"


def build_street_actions(
    actions: list[dict],
    board: list[str],
    bb_cents: int,
    positions: dict[int, str],
    up_to_index: int,
    target_street: str
) -> list[StreetAction]:
    """Build StreetAction list from actions up to a given index."""
    street_actions = []
    current_street = None
    current_actions = []
    has_bet_this_street = False

    for i, action in enumerate(actions):
        if i >= up_to_index:
            break

        street = action["street"]

        # New street started - save previous street as completed
        if street != current_street:
            if current_street and current_actions:
                cards = ""
                if current_street == "flop":
                    cards = "-".join(board[:3])
                elif current_street == "turn" and len(board) >= 4:
                    cards = board[3]
                elif current_street == "river" and len(board) >= 5:
                    cards = board[4]

                street_actions.append(StreetAction(
                    street=current_street,
                    cards=cards,
                    actions=", ".join(current_actions)
                ))

            current_street = street
            current_actions = []
            has_bet_this_street = False

        # Skip blinds posting for cleaner output
        if action["action"] == "post_blind":
            continue

        current_actions.append(format_action(action, bb_cents, positions, has_bet_this_street, street))

        # Track if there's been a bet/raise this street
        if action["action"] in ["bet", "raise_to"]:
            has_bet_this_street = True

    # Handle remaining actions from last processed street
    if current_street and current_actions:
        # If we're on a different street than target, this street is complete
        if current_street != target_street:
            cards = ""
            if current_street == "flop":
                cards = "-".join(board[:3])
            elif current_street == "turn" and len(board) >= 4:
                cards = board[3]
            elif current_street == "river" and len(board) >= 5:
                cards = board[4]

            street_actions.append(StreetAction(
                street=current_street,
                cards=cards,
                actions=", ".join(current_actions)
            ))

    # Add target street entry
    cards = ""
    if target_street == "flop":
        cards = "-".join(board[:3])
    elif target_street == "turn" and len(board) >= 4:
        cards = board[3]
    elif target_street == "river" and len(board) >= 5:
        cards = board[4]

    # Get actions on target street before the decision point
    target_actions = []
    has_bet_target_street = False
    for i, action in enumerate(actions):
        if i >= up_to_index:
            break
        if action["street"] == target_street and action["action"] != "post_blind":
            target_actions.append(format_action(action, bb_cents, positions, has_bet_target_street, target_street))
            if action["action"] in ["bet", "raise_to"]:
                has_bet_target_street = True

    if target_actions:
        street_actions.append(StreetAction(
            street=target_street,
            cards=cards,
            actions=", ".join(target_actions) + ", Hero to act"
        ))
    else:
        street_actions.append(StreetAction(
            street=target_street,
            cards=cards,
            actions="Hero first to act"
        ))

    return street_actions


def get_board_for_street(board: list[str], street: str) -> str:
    """Get board cards visible on a given street."""
    if street == "preflop":
        return ""
    elif street == "flop":
        return " ".join(board[:3])
    elif street == "turn":
        return " ".join(board[:4])
    else:  # river
        return " ".join(board[:5])


def convert_hand_to_insight_request(
    hand_data: dict,
    action_index: int
) -> Optional[InsightRequest]:
    """
    Convert a Firestore hand document to InsightRequest.

    Args:
        hand_data: The hand document from Firestore
        action_index: Index of the action to analyze (decision_metadata optional)

    Returns:
        InsightRequest or None if conversion fails
    """
    actions = hand_data.get("actions", [])
    if action_index >= len(actions):
        return None

    target_action = actions[action_index]
    meta = target_action.get("decision_metadata")  # May be None - that's OK

    # Basic info
    board = hand_data.get("board", [])
    bb_cents = hand_data.get("big_blind") or 200
    button = hand_data.get("button_seat", 0)
    seats = hand_data.get("seats", [])
    hole_cards = hand_data.get("hole_cards", {})
    n_players = len(seats)

    # Build position lookup
    positions = {}
    for s in seats:
        seat_idx = s["seat_index"]
        positions[seat_idx] = get_position(seat_idx, button, n_players)

    # Hero info
    hero_seat = target_action["seat"]
    hero_position = positions[hero_seat]

    # Get hero hand - try decision_metadata first, then hole_cards
    hero_hand = ""
    if meta:
        hero_hand_list = meta.get("bot_hand", [])
        hero_hand = "".join(hero_hand_list) if hero_hand_list else ""
    if not hero_hand and str(hero_seat) in hole_cards:
        hero_hand = "".join(hole_cards[str(hero_seat)])

    # Find villain (last player to act before hero, or opener)
    villain_position = "BTN"  # Default
    for i in range(action_index - 1, -1, -1):
        prev_action = actions[i]
        if prev_action["seat"] != hero_seat and prev_action["action"] not in ["post_blind", "fold"]:
            villain_position = positions[prev_action["seat"]]
            break

    # Street and board
    street = target_action["street"]
    board_str = get_board_for_street(board, street)

    # Pot and stack - use metadata if available, otherwise estimate
    if meta:
        pot_cents = meta.get("pot", 0)
        stack_cents = meta.get("effective_stack", 0)
    else:
        # Estimate pot from actions up to this point
        pot_cents = 0
        for i in range(action_index + 1):
            act = actions[i]
            if act.get("amount"):
                pot_cents += act["amount"]
        # Estimate stack from starting stack minus contributions
        stack_cents = 20000  # Default 100bb at 1/2

    pot_bb = pot_cents / bb_cents
    stack_bb = stack_cents / bb_cents

    # Determine if hero is facing a bet (affects Check vs Call naming)
    facing_bet = False
    for i in range(action_index - 1, -1, -1):
        prev = actions[i]
        if prev["street"] != street:
            break
        if prev["action"] in ["bet", "raise_to"]:
            facing_bet = True
            break

    # Parse action probabilities (only if metadata exists)
    action_frequencies = {}
    available_actions = []
    optimal_action = "Unknown"

    if meta:
        raw_probs = meta.get("bot_action_probs", {})
        for code, prob in raw_probs.items():
            action_name = parse_action_code(code, pot_bb, stack_bb, bb_cents, facing_bet)
            action_frequencies[action_name] = prob
            if prob > 0.001:  # Only include actions with meaningful probability
                available_actions.append(action_name)

        # Sort by probability to get optimal action
        sorted_actions = sorted(action_frequencies.items(), key=lambda x: -x[1])
        optimal_action = sorted_actions[0][0] if sorted_actions else "Unknown"
    else:
        # Without metadata, describe what action was taken
        taken_action = target_action["action"]
        amount = target_action.get("amount") or 0
        if taken_action == "fold":
            optimal_action = "Fold"
        elif taken_action == "check":
            optimal_action = "Check"
        elif taken_action == "call":
            optimal_action = "Call"
        elif taken_action in ["bet", "raise_to"]:
            amount_bb = amount / bb_cents
            optimal_action = f"{'Raise' if facing_bet or street == 'preflop' else 'Bet'} {amount_bb:.1f}bb"
        available_actions = [optimal_action]

    # Build action history
    street_actions = build_street_actions(actions, board, bb_cents, positions, action_index, street)

    # Build action sequence description - only show current street actions
    action_desc = f"{hero_position} to act"

    # Find actions on current street before hero's decision (iterate forwards for proper tracking)
    current_street_actions = []
    has_bet_for_action_seq = False
    for i in range(action_index):
        action = actions[i]
        if action["street"] != street:
            continue
        if action["action"] not in ["post_blind"]:
            current_street_actions.append(format_action(action, bb_cents, positions, has_bet_for_action_seq, street))
        if action["action"] in ["bet", "raise_to"]:
            has_bet_for_action_seq = True

    if current_street_actions:
        action_desc = " → ".join(current_street_actions) + f" → {hero_position} to act"
    else:
        action_desc = f"{hero_position} first to act"

    return InsightRequest(
        hero_hand=hero_hand,
        board=board_str,
        hero_position=hero_position,
        villain_position=villain_position,
        street=street,
        street_actions=street_actions,
        action_sequence=action_desc,
        available_actions=available_actions,
        action_frequencies=action_frequencies,
        ev_by_action={},  # Not available from bot data
        optimal_action=optimal_action,
        pot_size_bb=pot_bb,
        stack_size_bb=stack_bb,
    )


def find_interesting_decisions(hand_data: dict, min_mixed_actions: int = 2) -> list[int]:
    """
    Find action indices with interesting (mixed strategy) decisions.

    Args:
        hand_data: The hand document
        min_mixed_actions: Minimum number of actions with >15% probability

    Returns:
        List of action indices with interesting decisions
    """
    interesting = []
    actions = hand_data.get("actions", [])

    for i, action in enumerate(actions):
        meta = action.get("decision_metadata")
        if not meta:
            continue

        # Skip preflop for now (less interesting for teaching)
        if action["street"] == "preflop":
            continue

        probs = meta.get("bot_action_probs", {})
        significant = [k for k, v in probs.items() if v > 0.15]

        if len(significant) >= min_mixed_actions:
            interesting.append(i)

    return interesting


def convert_hand_to_full_insight_request(
    hand_data: dict,
    hero_user_id: str
) -> Optional[HandInsightRequest]:
    """
    Convert a Firestore hand document to HandInsightRequest for full-hand analysis.

    Args:
        hand_data: The hand document from Firestore
        hero_user_id: The user ID of the hero (human player)

    Returns:
        HandInsightRequest or None if conversion fails
    """
    actions = hand_data.get("actions", [])
    if not actions:
        return None

    # Basic info
    board = hand_data.get("board", [])
    bb_cents = hand_data.get("big_blind") or 200
    button = hand_data.get("button_seat", 0)
    seats = hand_data.get("seats", [])
    hole_cards = hand_data.get("hole_cards", {})
    winners = hand_data.get("winners", [])
    n_players = len(seats)

    # Find hero's seat
    hero_seat = None
    for s in seats:
        if s.get("user_id") == hero_user_id:
            hero_seat = s["seat_index"]
            break

    if hero_seat is None:
        return None

    # Build position lookup
    positions_list = POSITIONS_2MAX if n_players == 2 else POSITIONS_6MAX
    positions = {}
    for s in seats:
        seat_idx = s["seat_index"]
        positions[seat_idx] = get_position(seat_idx, button, n_players)

    hero_position = positions[hero_seat]

    # Get hero's hole cards
    hero_hand = ""
    if str(hero_seat) in hole_cards:
        hero_hand = "".join(hole_cards[str(hero_seat)])

    # Determine pot type from preflop action
    pot_type = "single raised"
    preflop_raises = 0
    for action in actions:
        if action["street"] != "preflop":
            break
        if action["action"] in ["bet", "raise_to"]:
            preflop_raises += 1

    if preflop_raises == 0:
        pot_type = "limped"
    elif preflop_raises == 1:
        pot_type = "single raised"
    elif preflop_raises == 2:
        pot_type = "3-bet"
    elif preflop_raises >= 3:
        pot_type = "4-bet+"

    # Build street actions
    street_order = ["preflop", "flop", "turn", "river"]
    street_actions_list = []

    for street in street_order:
        street_acts = []
        has_bet_this_street = False

        for action in actions:
            if action["street"] != street:
                continue
            if action["action"] == "post_blind":
                continue

            street_acts.append(format_action(action, bb_cents, positions, has_bet_this_street, street))
            if action["action"] in ["bet", "raise_to"]:
                has_bet_this_street = True

        if street_acts:
            cards = ""
            if street == "flop" and len(board) >= 3:
                cards = " ".join(board[:3])
            elif street == "turn" and len(board) >= 4:
                cards = board[3]
            elif street == "river" and len(board) >= 5:
                cards = board[4]

            street_actions_list.append(StreetAction(
                street=street,
                cards=cards,
                actions=", ".join(street_acts)
            ))

    # Build hero's decisions with proper per-seat-per-street contribution tracking.
    # `amount` semantics: for call/bet/raise_to it is the seat's total contribution
    # for the current street, NOT the incremental chips moved this action.
    hero_decisions = []
    seat_street_contrib: dict[tuple[int, str], int] = {}
    pot_chips = 0  # Total chips in the pot, summed across all completed actions

    for i, action in enumerate(actions):
        seat = action["seat"]
        street = action["street"]
        act = action["action"]
        amount = action.get("amount") or 0

        # Compute chips moved by THIS action and update per-seat street contribution.
        prev_contrib = seat_street_contrib.get((seat, street), 0)
        if act == "post_blind":
            chips_moved = amount
            seat_street_contrib[(seat, street)] = prev_contrib + amount
        elif act in ("call", "bet", "raise_to", "all_in") and amount > 0:
            # `amount` is the new total contribution for this seat on this street
            chips_moved = max(0, amount - prev_contrib)
            seat_street_contrib[(seat, street)] = max(prev_contrib, amount)
        else:
            chips_moved = 0  # fold, check, or call with no recorded amount

        if seat != hero_seat or act == "post_blind":
            pot_chips += chips_moved
            continue

        # Hero is acting now. Snapshot state BEFORE applying hero's chips.
        pot_before = pot_chips
        max_other_contrib = max(
            (c for (s, st), c in seat_street_contrib.items()
             if st == street and s != hero_seat),
            default=0,
        )
        hero_prev_contrib = prev_contrib  # before this action
        to_call_chips = max(0, max_other_contrib - hero_prev_contrib)

        # Look back for facing-action description and detect villain all-in shove
        facing = "first to act"
        villain_all_in = False
        for j in range(i - 1, -1, -1):
            prev = actions[j]
            if prev["street"] != street:
                break
            if prev["seat"] != hero_seat and prev["action"] not in ["post_blind", "fold"]:
                prev_amount = prev.get("amount") or 0
                if prev["action"] in ["bet", "raise_to", "all_in"]:
                    facing = f"a {prev_amount / bb_cents:.1f}bb bet"
                    if prev.get("is_all_in") or prev["action"] == "all_in":
                        villain_all_in = True
                        facing += " (all-in)"
                elif prev["action"] == "check":
                    facing = "a check"
                elif prev["action"] == "call":
                    facing = "a call"
                break

        # Pot odds + required equity (only meaningful when there's something to call)
        if to_call_chips > 0:
            pot_offered = pot_before  # chips already in pot before hero's call
            ratio = pot_offered / to_call_chips if to_call_chips else 0.0
            pot_odds = f"{ratio:.2f}:1"
            required_eq_pct = to_call_chips / (pot_offered + to_call_chips) * 100
        else:
            pot_odds = ""
            required_eq_pct = 0.0

        # Format hero's action
        if act == "fold":
            action_taken = "folds"
        elif act == "check":
            action_taken = "checks"
        elif act == "call":
            action_taken = f"calls {to_call_chips / bb_cents:.1f}bb"
        elif act in ["bet", "raise_to", "all_in"]:
            action_taken = f"{'raises to' if facing != 'first to act' else 'bets'} {amount / bb_cents:.1f}bb"
            if action.get("is_all_in") or act == "all_in":
                action_taken += " (all-in)"
        else:
            action_taken = act

        hero_decisions.append(HeroDecision(
            street=street,
            action_taken=action_taken,
            pot_before_bb=pot_before / bb_cents,
            facing=facing,
            position_vs_villain="in position",
            to_call_bb=to_call_chips / bb_cents,
            pot_odds=pot_odds,
            required_equity_pct=required_eq_pct,
            villain_all_in=villain_all_in,
        ))

        pot_chips += chips_moved

    # Determine result
    hero_won = any(w.get("seat") == hero_seat for w in winners)

    # Calculate profit
    hero_invested = 0
    hero_won_amount = 0
    for action in actions:
        if action["seat"] == hero_seat:
            hero_invested += action.get("amount") or 0

    for w in winners:
        if w.get("seat") == hero_seat:
            hero_won_amount += w.get("amount") or 0

    profit_cents = hero_won_amount - hero_invested
    profit_bb = profit_cents / bb_cents

    # Final pot
    final_pot = sum((action.get("amount") or 0) for action in actions)

    # Went to showdown?
    went_to_showdown = len(board) == 5 and len([a for a in actions if a["action"] == "fold"]) < n_players - 1

    return HandInsightRequest(
        hero_position=hero_position,
        hero_hand=hero_hand,
        num_players=n_players,
        pot_type=pot_type,
        board=" ".join(board) if board else "",
        street_actions=street_actions_list,
        hero_decisions=hero_decisions,
        hero_won=hero_won,
        profit_bb=profit_bb,
        final_pot_bb=final_pot / bb_cents,
        went_to_showdown=went_to_showdown
    )
