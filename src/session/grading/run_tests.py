#!/usr/bin/env python3
"""
Integration test script for preflop grading.

Run this script to verify the grading system works with Firestore.

Usage:
    python -m src.session.grading.run_tests

Requires:
    - GOOGLE_APPLICATION_CREDENTIALS env var pointing to Firebase service account
    - Or run from a directory with the credentials file
"""

import sys
from pathlib import Path

# Add parent to path if running directly
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.persistence import FirestoreClient
from src.session.grading import PreflopGrader, Grade
from src.session.grading.test_cases import TEST_CASES, run_test_case, run_all_tests


def test_with_mock_data():
    """Run tests with mock hand data (doesn't require Firestore ranges)."""
    print("=" * 60)
    print("Testing with mock data (in-memory Firestore)")
    print("=" * 60)

    firestore = FirestoreClient(use_memory=True)
    grader = PreflopGrader(firestore)

    # This will mostly return empty results without Firestore
    # But it validates the code structure
    print("\nBuilding mock hand logs...")
    from src.session.grading.test_cases import _build_mock_hand_log

    for test in TEST_CASES[:3]:
        hand_log = _build_mock_hand_log(test)
        print(f"  {test['name']}: hand_log built OK")
        grades = grader.grade_hand(hand_log, "test_user")
        print(f"    Grades: {len(grades)} (expected 0 without Firestore)")


def test_with_real_firestore():
    """Run full test suite with real Firestore connection."""
    print("\n" + "=" * 60)
    print("Testing with real Firestore connection")
    print("=" * 60)

    try:
        firestore = FirestoreClient(use_memory=False)
        if not firestore.is_connected:
            print("\nFirestore not connected. Skipping real tests.")
            print("Set GOOGLE_APPLICATION_CREDENTIALS to enable.")
            return

        print(f"\nFirestore connected: {firestore.is_connected}")

        grader = PreflopGrader(firestore)

        # Run the full test suite
        print("\nRunning test suite...")
        results = run_all_tests(grader)

        print(f"\n{'=' * 60}")
        print(f"Test Results: {results['passed']}/{results['total']} passed")
        print("=" * 60)

        # Print failures
        failures = [r for r in results["results"] if not r["passed"]]
        if failures:
            print("\nFailed tests:")
            for f in failures:
                print(f"\n  {f['name']}:")
                print(f"    Expected: {f['expected_grade']}, Got: {f.get('actual_grade', 'None')}")
                print(f"    Confidence: expected {f['expected_confidence']}, got {f.get('actual_confidence', 'N/A')}")
                if f.get("reasoning"):
                    print(f"    Reasoning: {f['reasoning']}")
                if f.get("error"):
                    print(f"    Error: {f['error']}")
        else:
            print("\nAll tests passed!")

    except Exception as e:
        print(f"\nError running tests: {e}")
        import traceback
        traceback.print_exc()


def test_specific_hand():
    """Test grading a specific hand scenario."""
    print("\n" + "=" * 60)
    print("Testing specific hand scenarios")
    print("=" * 60)

    try:
        firestore = FirestoreClient(use_memory=False)
        if not firestore.is_connected:
            print("\nFirestore not connected. Skipping.")
            return

        grader = PreflopGrader(firestore)

        # Test: Fold AA from BTN (should be MISTAKE)
        print("\n1. Folding AA from BTN in RFI spot:")
        hand_log = {
            "hand_id": "test_fold_aa_btn",
            "button_seat": 0,
            "big_blind": 100,
            "seats": [
                {"seat_index": 0, "user_id": "hero"},
                {"seat_index": 1, "user_id": "sb"},
                {"seat_index": 2, "user_id": "bb"},
            ],
            "hole_cards": {"0": ["As", "Ah"]},
            "actions": [
                {"seat": 0, "action": "fold", "amount": None, "street": "preflop"},
            ],
        }

        grades = grader.grade_hand(hand_log, "hero")
        for g in grades:
            print(f"   Grade: {g.grade.value}")
            print(f"   Confidence: {g.confidence:.2f}")
            print(f"   Reasoning: {g.reasoning}")

        # Test: Open 72o from UTG (should be MISTAKE)
        print("\n2. Opening 72o from UTG:")
        hand_log = {
            "hand_id": "test_open_72o_utg",
            "button_seat": 0,
            "big_blind": 100,
            "seats": [
                {"seat_index": i, "user_id": f"p{i}"}
                for i in range(6)
            ],
            "hole_cards": {"3": ["7h", "2c"]},  # UTG is seat 3
            "actions": [
                {"seat": 3, "action": "raise_to", "amount": 250, "street": "preflop"},
            ],
        }
        # Fix: UTG is seat 3 when BTN is seat 0
        hand_log["seats"][3]["user_id"] = "hero"

        grades = grader.grade_hand(hand_log, "hero")
        for g in grades:
            print(f"   Grade: {g.grade.value}")
            print(f"   Confidence: {g.confidence:.2f}")
            print(f"   Reasoning: {g.reasoning}")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("Preflop Grading System - Integration Tests")
    print("=" * 60)

    test_with_mock_data()
    test_with_real_firestore()
    test_specific_hand()

    print("\n" + "=" * 60)
    print("Tests complete")
    print("=" * 60)
