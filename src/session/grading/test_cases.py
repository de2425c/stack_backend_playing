"""Test cases for preflop grading validation."""

# Diverse test scenarios to validate confidence and grading logic
TEST_CASES = [
    # =========================================================================
    # CLEAR MISTAKES - should always flag
    # =========================================================================
    {
        "name": "Fold AA UTG",
        "position": "UTG",
        "hand": "AhAs",
        "spot": "RFI",
        "action": "fold",
        "sizing": None,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.9",
        "description": "Folding aces preflop is always wrong",
    },
    {
        "name": "Open 72o UTG",
        "position": "UTG",
        "hand": "7h2c",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.5,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.9",
        "description": "72o is the worst hand, never open from UTG",
    },
    {
        "name": "Fold KK to 3-bet",
        "position": "BTN",
        "hand": "KhKs",
        "spot": "BTN_RFI/SB_3B",
        "action": "fold",
        "sizing": None,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.8",
        "description": "KK should never fold to a single 3-bet",
    },
    {
        "name": "Fold AA to 3-bet",
        "position": "CO",
        "hand": "AcAd",
        "spot": "CO_RFI/BTN_3B",
        "action": "fold",
        "sizing": None,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.9",
        "description": "AA should never fold to any 3-bet",
    },

    # =========================================================================
    # CLEAR GOOD PLAYS
    # =========================================================================
    {
        "name": "Open AA BTN",
        "position": "BTN",
        "hand": "AhAc",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.5,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.9",
        "description": "Opening AA is always correct",
    },
    {
        "name": "Fold 72o UTG",
        "position": "UTG",
        "hand": "7h2c",
        "spot": "RFI",
        "action": "fold",
        "sizing": None,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.9",
        "description": "Folding 72o from UTG is always correct",
    },
    {
        "name": "Open KQs CO",
        "position": "CO",
        "hand": "KhQh",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.5,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.9",
        "description": "KQs is a standard open from CO",
    },
    {
        "name": "3-bet QQ vs UTG",
        "position": "BTN",
        "hand": "QhQs",
        "spot": "UTG_RFI",
        "action": "raise",
        "sizing": 9.0,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.8",
        "description": "QQ is a standard 3-bet vs UTG open",
    },

    # =========================================================================
    # MIXED STRATEGIES - should NOT flag as mistake (low confidence)
    # =========================================================================
    {
        "name": "Fold ATs to 3-bet (mixed)",
        "position": "CO",
        "hand": "AhTh",
        "spot": "CO_RFI/BTN_3B",
        "action": "fold",
        "sizing": None,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.5",
        "description": "ATs vs 3-bet is a mixed spot, both fold and call valid",
    },
    {
        "name": "Call with KQo vs 3-bet (mixed)",
        "position": "BTN",
        "hand": "KhQc",
        "spot": "BTN_RFI/SB_3B",
        "action": "call",
        "sizing": 9.0,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.6",
        "description": "KQo vs 3-bet is a mixed spot",
    },
    {
        "name": "Open 76s UTG (marginal)",
        "position": "UTG",
        "hand": "7h6h",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.5,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.6",
        "description": "76s from UTG is borderline, mixed strategy",
    },

    # =========================================================================
    # SIZING MISMATCHES - lower confidence
    # =========================================================================
    {
        "name": "Open 72o BTN with weird sizing",
        "position": "BTN",
        "hand": "7h2c",
        "spot": "RFI",
        "action": "raise",
        "sizing": 5.0,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.7",
        "description": "72o is wrong regardless of sizing, but confidence lower",
    },
    {
        "name": "3-bet with 54s (borderline) weird sizing",
        "position": "SB",
        "hand": "5h4h",
        "spot": "BTN_RFI",
        "action": "raise",
        "sizing": 15.0,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.5",
        "description": "54s is borderline 3-bet, weird sizing lowers confidence",
    },
    {
        "name": "Open AA with min-raise",
        "position": "BTN",
        "hand": "AhAs",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.0,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.8",
        "description": "AA is always good even with non-standard sizing",
    },

    # =========================================================================
    # BB DEFENSE SPOTS
    # =========================================================================
    {
        "name": "Fold AA in BB vs raise",
        "position": "BB",
        "hand": "AhAs",
        "spot": "BTN_RFI",
        "action": "fold",
        "sizing": None,
        "expected_grade": "MISTAKE",
        "expected_confidence": ">0.9",
        "description": "Never fold AA in BB",
    },
    {
        "name": "Call with 72o in BB vs min-raise",
        "position": "BB",
        "hand": "7h2c",
        "spot": "BTN_RFI",
        "action": "call",
        "sizing": 2.0,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.6",
        "description": "72o might be in calling range vs very small open",
    },
    {
        "name": "3-bet KK from BB",
        "position": "BB",
        "hand": "KhKs",
        "spot": "CO_RFI",
        "action": "raise",
        "sizing": 12.0,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.9",
        "description": "KK should always 3-bet from BB",
    },

    # =========================================================================
    # DEEPER SPOTS - lower confidence due to spot complexity
    # =========================================================================
    {
        "name": "Fold QQ to 4-bet",
        "position": "BTN",
        "hand": "QhQs",
        "spot": "BTN_RFI/SB_3B/BTN_4B",
        "action": "fold",
        "sizing": None,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.6",
        "description": "Deep spot, QQ vs 4-bet is complex",
    },
    {
        "name": "5-bet shove AA",
        "position": "CO",
        "hand": "AhAs",
        "spot": "CO_RFI/BTN_3B/CO_4B/BTN_5B",
        "action": "raise",
        "sizing": 100.0,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.5",
        "description": "Very deep spot, low confidence but AA is always good",
    },

    # =========================================================================
    # EDGE CASES
    # =========================================================================
    {
        "name": "Open with exactly threshold hand",
        "position": "HJ",
        "hand": "KdJd",
        "spot": "RFI",
        "action": "raise",
        "sizing": 2.5,
        "expected_grade": "GOOD",
        "expected_confidence": ">0.7",
        "description": "KJs is standard open from HJ",
    },
    {
        "name": "Fold AKo to 3-bet (can be mixed)",
        "position": "UTG",
        "hand": "AhKc",
        "spot": "UTG_RFI/BTN_3B",
        "action": "fold",
        "sizing": None,
        "expected_grade": "GOOD",
        "expected_confidence": "<0.8",
        "description": "AKo facing 3-bet from BTN after UTG open can be mixed",
    },
]


def run_test_case(grader, test_case: dict) -> dict:
    """
    Run a single test case against the grader.

    Args:
        grader: PreflopGrader instance
        test_case: Test case dict

    Returns:
        Result dict with pass/fail and details
    """
    # Build mock hand data
    hand_log = _build_mock_hand_log(test_case)

    # Grade the hand
    grades = grader.grade_hand(hand_log, "test_user")

    if not grades:
        return {
            "name": test_case["name"],
            "passed": False,
            "error": "No grades returned",
            "expected_grade": test_case["expected_grade"],
            "actual_grade": None,
        }

    grade = grades[0]

    # Check grade
    expected_grade = test_case["expected_grade"]
    actual_grade = grade.grade.name

    grade_match = actual_grade == expected_grade

    # Check confidence
    expected_conf = test_case["expected_confidence"]
    actual_conf = grade.confidence

    if expected_conf.startswith(">"):
        threshold = float(expected_conf[1:])
        conf_match = actual_conf > threshold
    elif expected_conf.startswith("<"):
        threshold = float(expected_conf[1:])
        conf_match = actual_conf < threshold
    else:
        conf_match = abs(actual_conf - float(expected_conf)) < 0.1

    passed = grade_match and conf_match

    return {
        "name": test_case["name"],
        "passed": passed,
        "expected_grade": expected_grade,
        "actual_grade": actual_grade,
        "expected_confidence": expected_conf,
        "actual_confidence": f"{actual_conf:.2f}",
        "reasoning": grade.reasoning,
    }


def _build_mock_hand_log(test_case: dict) -> dict:
    """Build a mock hand log from a test case."""
    position = test_case["position"]
    hand = test_case["hand"]
    action = test_case["action"]
    sizing = test_case["sizing"]
    spot = test_case["spot"]

    # Map position to seat index (assume 6-max)
    position_to_seat = {
        "BTN": 0,
        "SB": 1,
        "BB": 2,
        "UTG": 3,
        "HJ": 4,
        "CO": 5,
    }
    user_seat = position_to_seat.get(position, 0)

    # Build prior actions from spot path
    actions = []
    if spot != "RFI":
        spot_parts = spot.split("/")
        for part in spot_parts:
            if "_RFI" in part:
                pos = part.replace("_RFI", "")
                seat = _position_to_seat_8max(pos)
                actions.append({
                    "seat": seat,
                    "action": "raise_to",
                    "amount": 250,  # 2.5bb
                    "street": "preflop",
                })
            elif "_3B" in part:
                pos = part.replace("_3B", "")
                seat = _position_to_seat_8max(pos)
                actions.append({
                    "seat": seat,
                    "action": "raise_to",
                    "amount": 900,  # 9bb 3-bet
                    "street": "preflop",
                })
            elif "_4B" in part:
                pos = part.replace("_4B", "")
                seat = _position_to_seat_8max(pos)
                actions.append({
                    "seat": seat,
                    "action": "raise_to",
                    "amount": 2200,  # 22bb 4-bet
                    "street": "preflop",
                })
            elif "_C" in part:
                pos = part.replace("_C", "")
                seat = _position_to_seat_8max(pos)
                actions.append({
                    "seat": seat,
                    "action": "call",
                    "amount": 250,
                    "street": "preflop",
                })

    # Add user's action
    amount = int(sizing * 100) if sizing else None
    actions.append({
        "seat": user_seat,
        "action": _normalize_action(action),
        "amount": amount,
        "street": "preflop",
    })

    return {
        "hand_id": f"test_{test_case['name'].replace(' ', '_')}",
        "button_seat": 0,
        "big_blind": 100,  # $1 BB
        "seats": [
            {"seat_index": i, "user_id": "test_user" if i == user_seat else f"opponent_{i}"}
            for i in range(6)
        ],
        "hole_cards": {str(user_seat): [hand[:2], hand[2:]]},
        "actions": actions,
    }


def _position_to_seat_8max(pos_8max: str) -> int:
    """Map 8-max position name to seat index."""
    # Using 6-max seat layout but 8-max naming for ranges
    mapping = {
        "UTG": 3,
        "UTG1": 3,
        "LJ": 3,
        "HJ": 4,
        "CO": 5,
        "BTN": 0,
        "SB": 1,
        "BB": 2,
    }
    return mapping.get(pos_8max, 0)


def _normalize_action(action: str) -> str:
    """Normalize action name to hand log format."""
    if action == "raise":
        return "raise_to"
    return action


def run_all_tests(grader) -> dict:
    """
    Run all test cases and return summary.

    Args:
        grader: PreflopGrader instance

    Returns:
        Summary dict with pass/fail counts and details
    """
    results = []
    passed = 0
    failed = 0

    for test_case in TEST_CASES:
        result = run_test_case(grader, test_case)
        results.append(result)
        if result["passed"]:
            passed += 1
        else:
            failed += 1

    return {
        "total": len(TEST_CASES),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
