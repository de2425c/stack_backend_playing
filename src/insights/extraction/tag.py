"""Step 4: Add metadata tags to chunks using LLM."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from anthropic import Anthropic

from .config import METADATA_SCHEMA


TAGGING_PROMPT = """You are tagging a poker textbook chunk with metadata for a RAG retrieval system.

Analyze the chunk and return a JSON object with these fields:
- streets: Array of streets discussed (from: {streets})
- board_textures: Array of board textures covered (from: {board_textures})
- pot_types: Array of pot types covered (from: {pot_types})
- positions: Array of positions mentioned (from: {positions})
- concepts: Array of strategic concepts (from: {concepts})
- stack_depths: Array of stack depth contexts (from: {stack_depths})
- has_range_data: Boolean - does it include range charts, percentages, or specific hand combos?
- has_ev_calculations: Boolean - does it include EV calculations or math?
- has_examples: Boolean - does it include specific hand examples?
- difficulty: "beginner", "intermediate", or "advanced"

IMPORTANT:
- Only include tags that are ACTUALLY discussed, not just mentioned
- Be selective - empty arrays are fine if a topic isn't covered
- For concepts, focus on the 3-5 MAIN concepts, not every term used

CHUNK TEXT:
{text}

Return ONLY valid JSON, no other text."""


def tag_chunk(
    chunk: dict,
    client: Anthropic,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 3
) -> dict:
    """
    Add metadata tags to a chunk using Claude.

    Args:
        chunk: Chunk dict with text.
        client: Anthropic client.
        model: Model to use.
        max_retries: Maximum retry attempts.

    Returns:
        Chunk dict with added metadata field.
    """
    prompt = TAGGING_PROMPT.format(
        text=chunk["text"][:8000],  # Limit input size
        streets=", ".join(METADATA_SCHEMA["streets"]),
        board_textures=", ".join(METADATA_SCHEMA["board_textures"]),
        pot_types=", ".join(METADATA_SCHEMA["pot_types"]),
        positions=", ".join(METADATA_SCHEMA["positions"]),
        concepts=", ".join(METADATA_SCHEMA["concepts"]),
        stack_depths=", ".join(METADATA_SCHEMA["stack_depths"]),
    )

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1000,
                timeout=60.0,  # 60 second timeout
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text

            # Parse JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            metadata = json.loads(content)

            # Validate and normalize metadata
            chunk["metadata"] = {
                "streets": [s for s in metadata.get("streets", [])
                           if s in METADATA_SCHEMA["streets"]],
                "board_textures": [b for b in metadata.get("board_textures", [])
                                  if b in METADATA_SCHEMA["board_textures"]],
                "pot_types": [p for p in metadata.get("pot_types", [])
                             if p in METADATA_SCHEMA["pot_types"]],
                "positions": [p for p in metadata.get("positions", [])
                             if p in METADATA_SCHEMA["positions"]],
                "concepts": [c for c in metadata.get("concepts", [])
                            if c in METADATA_SCHEMA["concepts"]],
                "stack_depths": [s for s in metadata.get("stack_depths", [])
                                if s in METADATA_SCHEMA["stack_depths"]],
                "has_range_data": bool(metadata.get("has_range_data", False)),
                "has_ev_calculations": bool(metadata.get("has_ev_calculations", False)),
                "has_examples": bool(metadata.get("has_examples", False)),
                "difficulty": metadata.get("difficulty", "intermediate"),
            }

            return chunk

        except json.JSONDecodeError as e:
            print(f"    JSON parse error: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue

        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                wait_time = 30 * (2 ** attempt)
                print(f"    Rate limited, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            print(f"    Error: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue

    # Fallback: empty metadata
    chunk["metadata"] = {
        "streets": [],
        "board_textures": [],
        "pot_types": [],
        "positions": [],
        "concepts": [],
        "stack_depths": [],
        "has_range_data": False,
        "has_ev_calculations": False,
        "has_examples": False,
        "difficulty": "intermediate",
    }
    return chunk


def tag_all_chunks(
    chunks: list[dict],
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
    batch_size: int = 10,
    checkpoint_path: str | None = None
) -> list[dict]:
    """
    Add metadata tags to all chunks.

    Args:
        chunks: List of chunk dicts.
        api_key: Anthropic API key.
        model: Model to use.
        batch_size: Number of chunks to process before pausing.
        checkpoint_path: Path to save progress checkpoints.

    Returns:
        List of chunks with metadata.
    """
    client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    for i, chunk in enumerate(chunks):
        # Skip if already tagged
        if chunk.get("metadata") and chunk["metadata"].get("concepts") is not None:
            continue

        print(f"  Tagging {i+1}/{len(chunks)}: {chunk['chunk_id']}")
        tag_chunk(chunk, client, model)

        # Save checkpoint periodically
        if checkpoint_path and (i + 1) % batch_size == 0:
            print(f"    Saving checkpoint at {i+1} chunks...")
            with open(checkpoint_path, "w") as f:
                json.dump(chunks, f, indent=2)

        # Rate limiting
        if (i + 1) % batch_size == 0:
            print(f"    Processed {i+1} chunks, brief pause...")
            time.sleep(2)

    return chunks


def save_corpus(chunks: list[dict], output_path: str | Path) -> None:
    """Save final corpus to JSON file."""
    with open(output_path, "w") as f:
        json.dump(chunks, f, indent=2)


def load_corpus(path: str | Path) -> list[dict]:
    """Load corpus from JSON file."""
    with open(path) as f:
        return json.load(f)


def print_metadata_summary(chunks: list[dict]) -> None:
    """Print summary of metadata coverage."""
    total = len(chunks)

    # Count non-empty metadata fields
    streets_coverage = sum(1 for c in chunks if c.get("metadata", {}).get("streets"))
    textures_coverage = sum(1 for c in chunks if c.get("metadata", {}).get("board_textures"))
    concepts_coverage = sum(1 for c in chunks if c.get("metadata", {}).get("concepts"))
    range_data = sum(1 for c in chunks if c.get("metadata", {}).get("has_range_data"))
    ev_calcs = sum(1 for c in chunks if c.get("metadata", {}).get("has_ev_calculations"))
    examples = sum(1 for c in chunks if c.get("metadata", {}).get("has_examples"))

    print(f"\n{'='*60}")
    print("METADATA COVERAGE")
    print(f"{'='*60}")
    print(f"Total chunks: {total}")
    print(f"Streets tagged: {streets_coverage} ({100*streets_coverage/total:.1f}%)")
    print(f"Board textures tagged: {textures_coverage} ({100*textures_coverage/total:.1f}%)")
    print(f"Concepts tagged: {concepts_coverage} ({100*concepts_coverage/total:.1f}%)")
    print(f"Has range data: {range_data} ({100*range_data/total:.1f}%)")
    print(f"Has EV calculations: {ev_calcs} ({100*ev_calcs/total:.1f}%)")
    print(f"Has examples: {examples} ({100*examples/total:.1f}%)")

    # Concept frequency
    concept_counts: dict[str, int] = {}
    for chunk in chunks:
        for concept in chunk.get("metadata", {}).get("concepts", []):
            concept_counts[concept] = concept_counts.get(concept, 0) + 1

    print(f"\nTop concepts:")
    for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  {concept}: {count}")
