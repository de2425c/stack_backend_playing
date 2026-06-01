#!/usr/bin/env python3
"""
Run A/B evaluation comparing old single-pass vs new two-pass insight generation.

Usage:
    python -m src.insights.eval.run_reasoning_eval [--num-hands N]
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.insights.generator import InsightGenerator, build_hand_prompt, HAND_ANALYSIS_SYSTEM_PROMPT
from src.insights.schema import HandInsightRequest, StreetAction, HeroDecision


def load_eval_scenarios(eval_path: str | None = None) -> list[dict]:
    """Load evaluation scenarios from JSON file."""
    if eval_path is None:
        eval_path = Path(__file__).parent / "reasoning_eval.json"

    with open(eval_path) as f:
        data = json.load(f)

    return data["scenarios"]


def scenario_to_request(scenario: dict) -> HandInsightRequest:
    """Convert scenario dict to HandInsightRequest."""
    hand = scenario["hand"]

    street_actions = [
        StreetAction(
            street=sa["street"],
            cards=sa["cards"],
            actions=sa["actions"]
        )
        for sa in hand["street_actions"]
    ]

    hero_decisions = [
        HeroDecision(
            street=hd["street"],
            action_taken=hd["action_taken"],
            pot_before_bb=hd["pot_before_bb"],
            facing=hd["facing"],
            position_vs_villain=hd["position_vs_villain"]
        )
        for hd in hand["hero_decisions"]
    ]

    return HandInsightRequest(
        hero_position=hand["hero_position"],
        hero_hand=hand["hero_hand"],
        num_players=hand["num_players"],
        pot_type=hand["pot_type"],
        board=hand["board"],
        street_actions=street_actions,
        hero_decisions=hero_decisions,
        hero_won=hand["hero_won"],
        profit_bb=hand["profit_bb"],
    )


def generate_single_pass(generator: InsightGenerator, request: HandInsightRequest) -> tuple[str, float]:
    """Generate insight using old single-pass approach."""
    from anthropic import Anthropic

    user_prompt = build_hand_prompt(request)

    start = time.time()

    # Use direct API call like the old approach
    response = generator.client.messages.create(
        model=generator.model,
        max_tokens=150,
        system=HAND_ANALYSIS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    elapsed = time.time() - start
    insight = response.content[0].text.strip() if response.content else ""

    return insight, elapsed


def generate_two_pass(generator: InsightGenerator, request: HandInsightRequest) -> tuple[str, float]:
    """Generate insight using new two-pass approach."""
    start = time.time()
    response = generator.generate_hand_insight(request)
    elapsed = time.time() - start

    return response.insight, elapsed


def run_ab_comparison(scenarios: list[dict], num_hands: int = 10) -> dict:
    """
    Run A/B comparison between single-pass and two-pass.

    Returns results dict with comparisons and metrics.
    """
    generator = InsightGenerator(use_vector_search=True)

    # Sample scenarios
    if len(scenarios) > num_hands:
        scenarios = random.sample(scenarios, num_hands)

    results = {
        "comparisons": [],
        "metrics": {
            "single_pass_avg_time": 0,
            "two_pass_avg_time": 0,
            "single_pass_avg_length": 0,
            "two_pass_avg_length": 0,
        }
    }

    single_times = []
    two_times = []
    single_lengths = []
    two_lengths = []

    print(f"\nRunning A/B eval on {len(scenarios)} hands...\n")
    print("=" * 60)

    for i, scenario in enumerate(scenarios):
        print(f"\n[{i+1}/{len(scenarios)}] {scenario['id']}: {scenario['description']}")

        request = scenario_to_request(scenario)

        # Generate both versions
        print("  Generating single-pass...", end=" ", flush=True)
        single_insight, single_time = generate_single_pass(generator, request)
        print(f"({single_time:.1f}s)")

        print("  Generating two-pass...", end=" ", flush=True)
        two_insight, two_time = generate_two_pass(generator, request)
        print(f"({two_time:.1f}s)")

        # Randomize order for unbiased presentation
        order = random.choice(["AB", "BA"])
        if order == "AB":
            option_a = ("single_pass", single_insight)
            option_b = ("two_pass", two_insight)
        else:
            option_a = ("two_pass", two_insight)
            option_b = ("single_pass", single_insight)

        comparison = {
            "scenario_id": scenario["id"],
            "description": scenario["description"],
            "expected_concepts": scenario.get("expected_concepts", []),
            "option_a": {
                "source": option_a[0],
                "insight": option_a[1]
            },
            "option_b": {
                "source": option_b[0],
                "insight": option_b[1]
            },
            "single_pass_time": single_time,
            "two_pass_time": two_time,
            "winner": None  # To be filled by evaluator
        }

        results["comparisons"].append(comparison)

        single_times.append(single_time)
        two_times.append(two_time)
        single_lengths.append(len(single_insight))
        two_lengths.append(len(two_insight))

        # Print insights for review
        print(f"\n  OPTION A:\n    {option_a[1][:200]}...")
        print(f"\n  OPTION B:\n    {option_b[1][:200]}...")
        print("-" * 60)

    # Calculate metrics
    results["metrics"]["single_pass_avg_time"] = sum(single_times) / len(single_times)
    results["metrics"]["two_pass_avg_time"] = sum(two_times) / len(two_times)
    results["metrics"]["single_pass_avg_length"] = sum(single_lengths) / len(single_lengths)
    results["metrics"]["two_pass_avg_length"] = sum(two_lengths) / len(two_lengths)

    return results


def print_summary(results: dict):
    """Print evaluation summary."""
    metrics = results["metrics"]

    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY")
    print("=" * 60)
    print(f"\nLatency:")
    print(f"  Single-pass avg: {metrics['single_pass_avg_time']:.2f}s")
    print(f"  Two-pass avg: {metrics['two_pass_avg_time']:.2f}s")
    print(f"  Overhead: {metrics['two_pass_avg_time'] - metrics['single_pass_avg_time']:.2f}s")

    print(f"\nInsight Length:")
    print(f"  Single-pass avg: {metrics['single_pass_avg_length']:.0f} chars")
    print(f"  Two-pass avg: {metrics['two_pass_avg_length']:.0f} chars")

    # Count wins if recorded
    single_wins = sum(1 for c in results["comparisons"] if c.get("winner") == "single_pass")
    two_wins = sum(1 for c in results["comparisons"] if c.get("winner") == "two_pass")
    total_judged = single_wins + two_wins

    if total_judged > 0:
        print(f"\nWin Rate (n={total_judged}):")
        print(f"  Single-pass: {single_wins}/{total_judged} ({100*single_wins/total_judged:.0f}%)")
        print(f"  Two-pass: {two_wins}/{total_judged} ({100*two_wins/total_judged:.0f}%)")
    else:
        print("\n[No winner judgments recorded yet]")
        print("To judge: For each comparison, set 'winner' to 'single_pass' or 'two_pass'")


def save_results(results: dict, output_path: str | None = None):
    """Save results to JSON file."""
    if output_path is None:
        output_path = Path(__file__).parent / f"eval_results_{int(time.time())}.json"

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Run A/B eval for reasoning system")
    parser.add_argument("--num-hands", type=int, default=10, help="Number of hands to evaluate")
    parser.add_argument("--output", type=str, help="Output path for results JSON")
    args = parser.parse_args()

    # Check for required env vars
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)
    if not os.getenv("PINECONE_API_KEY"):
        print("Error: PINECONE_API_KEY not set")
        sys.exit(1)

    scenarios = load_eval_scenarios()
    results = run_ab_comparison(scenarios, num_hands=args.num_hands)

    print_summary(results)
    save_results(results, args.output)


if __name__ == "__main__":
    main()
