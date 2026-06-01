#!/usr/bin/env python3
"""Automated retrieval evaluation runner for Janda corpus."""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.insights.vector_store import PokerVectorStore
from src.insights.query_builder import build_query_for_decision, JandaQuery


@dataclass
class ScenarioResult:
    """Result for a single scenario evaluation."""
    scenario_id: str
    street: str
    expected_chunks: list[str]
    retrieved_chunks: list[str]
    retrieved_scores: list[float]
    correct_top1: bool
    correct_top3: bool
    query_used: str
    filters_used: dict[str, list[str]]


@dataclass
class EvalResults:
    """Aggregated evaluation results."""
    total: int = 0
    correct_top1: int = 0
    correct_top3: int = 0
    accuracy_top1: float = 0.0
    accuracy_top3: float = 0.0
    by_street: dict[str, dict[str, float]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    scenario_results: list[ScenarioResult] = field(default_factory=list)


def run_eval(scenarios_path: str | None = None, verbose: bool = True) -> EvalResults:
    """
    Run retrieval evaluation and compute metrics.

    Args:
        scenarios_path: Path to retrieval_eval.json. Defaults to same directory.
        verbose: Print progress during evaluation.

    Returns:
        EvalResults with accuracy metrics and failure details.
    """
    # Load scenarios
    if scenarios_path is None:
        scenarios_path = str(Path(__file__).parent / "retrieval_eval.json")

    with open(scenarios_path) as f:
        eval_data = json.load(f)

    scenarios = eval_data["scenarios"]

    if verbose:
        print(f"Loaded {len(scenarios)} evaluation scenarios")

    # Initialize vector store
    if not os.getenv("PINECONE_API_KEY"):
        raise ValueError("PINECONE_API_KEY environment variable not set")

    store = PokerVectorStore()

    # Track results
    results = EvalResults(total=len(scenarios))
    street_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "top1": 0, "top3": 0})

    for i, scenario in enumerate(scenarios):
        if verbose:
            print(f"\n[{i+1}/{len(scenarios)}] Evaluating: {scenario['id']} - {scenario['description']}")

        # Build query using query_builder
        janda_query = build_query_for_decision(
            street=scenario["street"],
            board=scenario.get("board", ""),
            pot_type=scenario.get("pot_type", "single_raised"),
            hero_position=scenario.get("hero_position", ""),
            villain_position=scenario.get("villain_position", ""),
            facing=scenario.get("facing", ""),
            hero_hand_category=""
        )

        # Also try with query hint if provided
        query_to_use = janda_query.query
        if scenario.get("query_hint"):
            # Use hint as primary query, it's more specific
            query_to_use = scenario["query_hint"]

        if verbose:
            print(f"  Query: {query_to_use}")
            print(f"  Filters: {janda_query.filters}")

        # Search Janda corpus
        try:
            search_results = store.search_janda(
                query=query_to_use,
                filters=janda_query.filters,
                top_k=5
            )
        except Exception as e:
            print(f"  ERROR: Search failed: {e}")
            continue

        retrieved_chunks = [r["chunk_id"] for r in search_results]
        retrieved_scores = [r["score"] for r in search_results]
        expected_chunks = scenario["expected_chunks"]

        if verbose:
            print(f"  Expected: {expected_chunks[:3]}")
            print(f"  Retrieved: {retrieved_chunks[:3]}")
            print(f"  Scores: {[f'{s:.3f}' for s in retrieved_scores[:3]]}")

        # Check correctness
        # Top-1: first retrieved is in expected
        correct_top1 = len(retrieved_chunks) > 0 and retrieved_chunks[0] in expected_chunks

        # Top-3: any of first 3 retrieved is in expected
        correct_top3 = any(c in expected_chunks for c in retrieved_chunks[:3])

        if correct_top1:
            results.correct_top1 += 1
        if correct_top3:
            results.correct_top3 += 1

        # Track by street
        street = scenario["street"]
        street_stats[street]["total"] += 1
        if correct_top1:
            street_stats[street]["top1"] += 1
        if correct_top3:
            street_stats[street]["top3"] += 1

        # Record failure details
        if not correct_top3:
            results.failures.append({
                "id": scenario["id"],
                "description": scenario["description"],
                "expected": expected_chunks,
                "got": retrieved_chunks[:5],
                "scores": retrieved_scores[:5],
                "query": query_to_use,
                "filters": janda_query.filters
            })

        # Store scenario result
        results.scenario_results.append(ScenarioResult(
            scenario_id=scenario["id"],
            street=street,
            expected_chunks=expected_chunks,
            retrieved_chunks=retrieved_chunks,
            retrieved_scores=retrieved_scores,
            correct_top1=correct_top1,
            correct_top3=correct_top3,
            query_used=query_to_use,
            filters_used=janda_query.filters
        ))

        if verbose:
            status = "✓" if correct_top3 else "✗"
            print(f"  Result: {status} (top1={correct_top1}, top3={correct_top3})")

    # Compute final metrics
    results.accuracy_top1 = results.correct_top1 / results.total if results.total > 0 else 0.0
    results.accuracy_top3 = results.correct_top3 / results.total if results.total > 0 else 0.0

    # Compute by-street metrics
    for street, stats in street_stats.items():
        total = stats["total"]
        results.by_street[street] = {
            "total": total,
            "accuracy_top1": stats["top1"] / total if total > 0 else 0.0,
            "accuracy_top3": stats["top3"] / total if total > 0 else 0.0
        }

    return results


def print_results(results: EvalResults) -> None:
    """Print formatted evaluation results."""
    print("\n" + "=" * 60)
    print("RETRIEVAL EVALUATION RESULTS")
    print("=" * 60)

    print(f"\nOverall Metrics:")
    print(f"  Total scenarios: {results.total}")
    print(f"  Correct top-1:   {results.correct_top1} ({results.accuracy_top1:.1%})")
    print(f"  Correct top-3:   {results.correct_top3} ({results.accuracy_top3:.1%})")

    print(f"\nBy Street:")
    for street in ["preflop", "flop", "turn", "river"]:
        if street in results.by_street:
            stats = results.by_street[street]
            print(f"  {street:10} - top1: {stats['accuracy_top1']:.1%}, top3: {stats['accuracy_top3']:.1%} (n={stats['total']})")

    if results.failures:
        print(f"\nFailures ({len(results.failures)}):")
        for f in results.failures:
            print(f"  - {f['id']}: {f['description']}")
            print(f"    Expected: {f['expected'][:2]}...")
            print(f"    Got:      {f['got'][:2]}... (scores: {[f'{s:.3f}' for s in f['scores'][:2]]})")

    # Success criteria check
    print(f"\n{'=' * 60}")
    target = 0.70
    if results.accuracy_top3 >= target:
        print(f"✓ SUCCESS: Top-3 accuracy {results.accuracy_top3:.1%} >= {target:.0%} target")
    else:
        print(f"✗ BELOW TARGET: Top-3 accuracy {results.accuracy_top3:.1%} < {target:.0%} target")
        print(f"  Consider: hybrid search, query expansion, or embedding adjustments")


def save_report(results: EvalResults, output_path: str | None = None) -> str:
    """Save detailed report to markdown file."""
    if output_path is None:
        output_path = str(Path(__file__).parent / "retrieval_report.md")

    lines = [
        "# Retrieval Evaluation Report",
        f"\nGenerated: {datetime.now().isoformat()}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total scenarios | {results.total} |",
        f"| Top-1 accuracy | {results.accuracy_top1:.1%} |",
        f"| Top-3 accuracy | {results.accuracy_top3:.1%} |",
        "",
        "## By Street",
        "",
        "| Street | Top-1 | Top-3 | N |",
        "|--------|-------|-------|---|",
    ]

    for street in ["preflop", "flop", "turn", "river"]:
        if street in results.by_street:
            stats = results.by_street[street]
            lines.append(f"| {street} | {stats['accuracy_top1']:.1%} | {stats['accuracy_top3']:.1%} | {stats['total']} |")

    lines.extend([
        "",
        "## Detailed Results",
        "",
    ])

    for sr in results.scenario_results:
        status = "✓" if sr.correct_top3 else "✗"
        lines.append(f"### {status} {sr.scenario_id}")
        lines.append(f"- **Street:** {sr.street}")
        lines.append(f"- **Query:** {sr.query_used}")
        lines.append(f"- **Filters:** {sr.filters_used}")
        lines.append(f"- **Expected:** {sr.expected_chunks[:3]}")
        lines.append(f"- **Retrieved:** {sr.retrieved_chunks[:3]}")
        lines.append(f"- **Scores:** {[f'{s:.3f}' for s in sr.retrieved_scores[:3]]}")
        lines.append(f"- **Top-1 correct:** {sr.correct_top1}")
        lines.append(f"- **Top-3 correct:** {sr.correct_top3}")
        lines.append("")

    if results.failures:
        lines.extend([
            "## Failures Analysis",
            "",
        ])
        for f in results.failures:
            lines.append(f"### {f['id']}")
            lines.append(f"- **Description:** {f['description']}")
            lines.append(f"- **Expected:** {f['expected']}")
            lines.append(f"- **Got:** {f['got']}")
            lines.append(f"- **Query:** {f['query']}")
            lines.append(f"- **Filters:** {f['filters']}")
            lines.append("")

    lines.extend([
        "## Tuning Notes",
        "",
        "If accuracy is below 70%, consider:",
        "",
        "1. **Hybrid search** - Combine BM25 keyword search with semantic search",
        "2. **Query expansion** - Generate 2-3 variant queries and merge results",
        "3. **Adjust embedding text** - Include more/less context in chunk embeddings",
        "4. **Metadata boost** - Weight filtered results higher in scoring",
        "5. **Rerank** - Use a cross-encoder reranker on top-N results",
        "",
    ])

    report_text = "\n".join(lines)

    with open(output_path, "w") as f:
        f.write(report_text)

    return output_path


def main():
    """Run evaluation and print results."""
    print("Starting retrieval evaluation...")
    print("-" * 60)

    try:
        results = run_eval(verbose=True)
        print_results(results)

        # Save report
        report_path = save_report(results)
        print(f"\nDetailed report saved to: {report_path}")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
