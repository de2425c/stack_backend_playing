"""Real test cases from Firestore spot_candidates and hands."""

from .schema import InsightRequest, StreetAction

# =============================================================================
# FROM SPOT_CANDIDATES (pre-built decision points)
# =============================================================================

# Test case 1: Underpair facing turn bet in 3-bet pot
# 9s9c on Ac5c4d8h, BB vs BTN, facing 14.2bb bet
TEST_CASE_1 = InsightRequest(
    hero_hand="9s9c",
    board="Ac5c4d8h",
    hero_position="BB",
    villain_position="BTN",
    street="turn",
    street_actions=[
        StreetAction(street="preflop", cards="", actions="BTN opens 2.5bb, BB 3-bets to 13bb, BTN calls"),
        StreetAction(street="flop", cards="Ac-5c-4d", actions="BB bets 8.6bb, BTN calls"),
        StreetAction(street="turn", cards="8h", actions="BB checks, BTN bets 14.2bb, BB to act"),
    ],
    action_sequence="BB checks → BTN bets 14.2bb → BB to act",
    available_actions=["Fold", "Call", "All-in"],
    action_frequencies={"Fold": 0.07, "Call": 0.93, "All-in": 0.0},
    ev_by_action={"Fold": 0.0, "Call": -0.09, "All-in": -8.66},
    optimal_action="Call",
    pot_size_bb=57.4,
    stack_size_bb=65.4,
    hand_category="underpair",
    board_texture="dry",
)

# Test case 2: Gutshot on flop, deciding whether to donk bet
# 8h7h on 6c4d2h, BB vs HJ, first to act
TEST_CASE_2 = InsightRequest(
    hero_hand="8h7h",
    board="6c4d2h",
    hero_position="BB",
    villain_position="HJ",
    street="flop",
    street_actions=[
        StreetAction(street="preflop", cards="", actions="HJ raises 2.5bb, BB calls"),
        StreetAction(street="flop", cards="6c-4d-2h", actions="BB to act"),
    ],
    action_sequence="BB to act",
    available_actions=["Check", "Bet 1.6bb", "Bet 3.8bb", "Bet 6.2bb"],
    action_frequencies={"Check": 0.10, "Bet 1.6bb": 0.48, "Bet 3.8bb": 0.39, "Bet 6.2bb": 0.03},
    ev_by_action={"Check": 2.76, "Bet 1.6bb": 2.78, "Bet 3.8bb": 2.76, "Bet 6.2bb": 2.68},
    optimal_action="Bet 1.6bb",
    pot_size_bb=5.0,
    stack_size_bb=97.5,
    hand_category="gutshot",
    board_texture="wet",
)

# =============================================================================
# FROM HANDS COLLECTION (real game hands with bot solver data)
# =============================================================================

# Test case 3: Top pair weak kicker on dry board - bet or check?
# Ac3h on AhQd6c, BB vs CO limper, first to act on flop
# Solver: check 35%, bet small 16%, bet large 49% - mixed strategy spot
TEST_CASE_3 = InsightRequest(
    hero_hand="Ac3h",
    board="Ah Qd 6c",
    hero_position="BB",
    villain_position="CO",
    street="flop",
    street_actions=[
        StreetAction(street="preflop", cards="", actions="CO limps 1bb, BTN folds, SB folds, BB checks"),
        StreetAction(street="flop", cards="Ah-Qd-6c", actions="BB to act"),
    ],
    action_sequence="BB to act (heads up vs limper)",
    available_actions=["Check", "Bet 33%", "Bet 75%", "Bet 125%"],
    action_frequencies={"Check": 0.35, "Bet 33%": 0.16, "Bet 75%": 0.0, "Bet 125%": 0.49},
    ev_by_action={},  # Not available from bot data
    optimal_action="Bet 125%",
    pot_size_bb=2.5,
    stack_size_bb=100.0,
    hand_category="top_pair_weak_kicker",
    board_texture="dry",
)

# Test case 4: TPTK on turn after checking flop - raise not call
# KQo 3-bet pot, flop 7d6s4d (whiffed), turn Qh gives top pair
# SB bets, solver says RAISE (52% small, 34% medium, 13% big) - never call
TEST_CASE_4 = InsightRequest(
    hero_hand="KdQc",
    board="7d 6s 4d Qh",
    hero_position="BB",
    villain_position="SB",
    street="turn",
    street_actions=[
        StreetAction(street="preflop", cards="", actions="SB raises 3bb, BB 3-bets 7.25bb, SB calls"),
        StreetAction(street="flop", cards="7d-6s-4d", actions="SB checks, BB checks"),
        StreetAction(street="turn", cards="Qh", actions="SB bets 10.9bb, BB to act"),
    ],
    action_sequence="SB bets 10.9bb → BB to act",
    available_actions=["Fold", "Call", "Raise 25bb", "Raise 50bb", "All-in"],
    action_frequencies={"Fold": 0.0, "Call": 0.0, "Raise 25bb": 0.52, "Raise 50bb": 0.35, "All-in": 0.13},
    ev_by_action={},  # Not available
    optimal_action="Raise 25bb",
    pot_size_bb=25.4,
    stack_size_bb=96.0,
    hand_category="top_pair_top_kicker",
    board_texture="wet",
)

ALL_TEST_CASES = [TEST_CASE_1, TEST_CASE_2, TEST_CASE_3, TEST_CASE_4]
SPOT_CANDIDATES_CASES = [TEST_CASE_1, TEST_CASE_2]
HANDS_COLLECTION_CASES = [TEST_CASE_3, TEST_CASE_4]


if __name__ == "__main__":
    from .generator import build_user_prompt

    for i, tc in enumerate(ALL_TEST_CASES, 1):
        print(f"\n{'='*60}")
        print(f"TEST CASE {i}")
        print('='*60)
        print(build_user_prompt(tc))
