"""Confidence score calculation for preflop grading."""


def sizing_match_score(
    game_sizing_bb: float,
    tree_sizing_bb: float,
    action_taken: str = None,
    facing_sizing_bb: float = None
) -> float:
    """
    Calculate how well the game sizing matches the tree sizing.

    For RAISE actions: compares our raise size to solver's expected size.
    For FOLD/CALL actions: considers how the facing bet compares to what solver studied.

    Key insight: If solver says "fold vs 9bb 3-bet" and villain bets 12bb,
    folding is even MORE correct (we're getting worse odds).

    Args:
        game_sizing_bb: The actual sizing in the game (our action, in BB)
        tree_sizing_bb: The expected sizing in the GTO tree (in BB)
        action_taken: "Fold", "Call", or "Raise"
        facing_sizing_bb: The bet we're facing (for fold/call decisions)

    Returns:
        Score from 0.2 to 1.0 based on sizing match/direction
    """
    # For raises: compare our sizing to expected
    if action_taken == "Raise":
        if tree_sizing_bb == 0:
            return 1.0
        if game_sizing_bb == 0:
            return 0.5

        ratio = game_sizing_bb / tree_sizing_bb
        deviation = abs(1 - ratio)

        if deviation <= 0.15:  # Within 15%
            return 1.0
        elif deviation <= 0.30:  # Within 30%
            return 0.85
        elif deviation <= 0.50:  # Within 50%
            return 0.6
        else:  # More than 50% off
            return 0.3

    # For fold/call: consider how facing sizing compares to tree
    if action_taken in ("Fold", "Call") and facing_sizing_bb and tree_sizing_bb:
        ratio = facing_sizing_bb / tree_sizing_bb

        if action_taken == "Fold":
            # Larger facing bet → fold is MORE correct (better confidence)
            if ratio >= 1.0:
                # Facing bigger bet than tree - fold is even more justified
                return 1.0
            elif ratio >= 0.7:
                # Facing slightly smaller bet - fold still reasonable
                return 0.9
            else:
                # Facing much smaller bet - fold might be too tight
                return 0.6

        elif action_taken == "Call":
            # Larger facing bet → call is LESS correct (worse odds)
            if ratio <= 1.0:
                # Facing same or smaller bet - call is solid
                return 1.0
            elif ratio <= 1.3:
                # Facing somewhat larger bet - call might be loose
                return 0.7
            elif ratio <= 1.5:
                # Facing significantly larger bet - call is questionable
                return 0.5
            else:
                # Facing much larger bet - call could be a mistake
                return 0.3

    # Default: no sizing info, full confidence
    return 1.0


def frequency_clarity_score(gto_frequency: float) -> float:
    """
    Calculate how clear the GTO answer is based on frequency.

    High confidence when GTO says "always" or "never".
    Low confidence for mixed strategies.

    Args:
        gto_frequency: The GTO frequency for the action (0-1)

    Returns:
        Score from 0.2 to 1.0 based on clarity
    """
    # Never do this action
    if gto_frequency == 0:
        return 1.0
    # Almost never (< 5%)
    elif gto_frequency < 0.05:
        return 0.9
    # Rarely (< 20%)
    elif gto_frequency < 0.20:
        return 0.5
    # Mixed strategy (20-80%) - low confidence
    elif gto_frequency <= 0.80:
        return 0.2
    # Usually (> 80% but < 95%)
    elif gto_frequency < 0.95:
        return 0.5
    # Almost always (>= 95%)
    elif gto_frequency < 1.0:
        return 0.9
    # Always do this action
    else:
        return 1.0


def spot_depth_score(spot_depth: int) -> float:
    """
    Calculate confidence based on spot depth.

    Shallower spots (like RFI) are more standard.
    Deeper spots (like 4-bet pots) have more variance, but we still
    want to catch clear mistakes.

    Args:
        spot_depth: Number of actions in the preflop sequence
            - 0: RFI (raise first in)
            - 1: Facing RFI (call or 3-bet)
            - 2: Facing 3-bet
            - 3+: Deeper spots (4-bet, etc.)

    Returns:
        Score from 0.6 to 1.0 based on spot standardness
    """
    if spot_depth == 0:
        return 1.0
    elif spot_depth == 1:
        return 0.95
    elif spot_depth == 2:
        return 0.85
    else:
        return 0.7  # Still decent confidence for clear mistakes


def calculate_confidence(
    game_sizing_bb: float,
    tree_sizing_bb: float,
    gto_frequency: float,
    spot_depth: int,
    action_taken: str = None,
    facing_sizing_bb: float = None
) -> float:
    """
    Calculate overall confidence score for a grading decision.

    Args:
        game_sizing_bb: The actual sizing in the game (in big blinds)
        tree_sizing_bb: The expected sizing in the GTO tree (in big blinds)
        gto_frequency: The GTO frequency for the action taken (0-1)
        spot_depth: Number of actions deep in the preflop sequence
        action_taken: "Fold", "Call", or "Raise" (optional, for directional logic)
        facing_sizing_bb: The bet we're facing (optional, for fold/call decisions)

    Returns:
        Confidence score from 0 to 1
    """
    sizing_factor = sizing_match_score(
        game_sizing_bb,
        tree_sizing_bb,
        action_taken,
        facing_sizing_bb
    )
    clarity_factor = frequency_clarity_score(gto_frequency)
    spot_factor = spot_depth_score(spot_depth)

    return sizing_factor * clarity_factor * spot_factor
