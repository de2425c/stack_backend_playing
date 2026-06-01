#!/usr/bin/env python3
"""One-time script to embed Janda corpus into Pinecone."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.insights.vector_store import PokerVectorStore, JANDA_NAMESPACE


def main():
    """Embed Janda corpus into Pinecone."""
    # Check for API key
    if not os.getenv("PINECONE_API_KEY"):
        print("Error: PINECONE_API_KEY environment variable not set")
        sys.exit(1)

    # Locate corpus file
    script_dir = Path(__file__).parent
    corpus_path = script_dir.parent / "extraction" / "output" / "janda_corpus.json"

    if not corpus_path.exists():
        print(f"Error: Corpus file not found at {corpus_path}")
        sys.exit(1)

    # Load corpus to show stats
    with open(corpus_path) as f:
        corpus = json.load(f)

    print(f"Found {len(corpus)} chunks in Janda corpus")
    print(f"Corpus path: {corpus_path}")

    # Show part distribution
    from collections import Counter
    parts = Counter(c.get("part") for c in corpus)
    print("\nPart distribution:")
    for part_num, count in sorted(parts.items()):
        part_name = next((c.get("name", "") for c in corpus if c.get("part") == part_num), "")
        print(f"  Part {part_num}: {count} chunks - {part_name}")

    # Initialize vector store
    print("\nInitializing Pinecone vector store...")
    store = PokerVectorStore()

    # Check current namespace stats
    stats = store.index.describe_index_stats()
    janda_stats = stats.get("namespaces", {}).get(JANDA_NAMESPACE, {})
    existing_vectors = janda_stats.get("vector_count", 0)

    if existing_vectors > 0:
        print(f"\nWarning: {JANDA_NAMESPACE} namespace already has {existing_vectors} vectors")
        response = input("Delete existing vectors and re-index? [y/N]: ")
        if response.lower() != "y":
            print("Aborted.")
            sys.exit(0)

        # Delete existing vectors
        print(f"Deleting existing vectors from {JANDA_NAMESPACE}...")
        store.index.delete(delete_all=True, namespace=JANDA_NAMESPACE)
        print("Deleted.")

    # Index the corpus
    print("\nEmbedding and indexing chunks...")
    store.index_janda(str(corpus_path))

    # Verify
    print("\nVerifying index...")
    stats = store.index.describe_index_stats()
    janda_stats = stats.get("namespaces", {}).get(JANDA_NAMESPACE, {})
    final_count = janda_stats.get("vector_count", 0)

    print(f"\nFinal index stats:")
    print(f"  Namespace: {JANDA_NAMESPACE}")
    print(f"  Vector count: {final_count}")
    print(f"  Expected: {len(corpus)}")

    if final_count == len(corpus):
        print("\n✓ All chunks indexed successfully!")
    else:
        print(f"\n⚠ Vector count mismatch: {final_count} vs {len(corpus)}")

    # Test search
    print("\nTesting basic search...")
    results = store.search_janda("c-betting dry flop in position", top_k=3)
    print(f"Search 'c-betting dry flop in position' returned {len(results)} results:")
    for r in results:
        print(f"  {r['chunk_id']}: {r['title']} (score: {r['score']:.3f})")

    # Test filtered search
    print("\nTesting filtered search...")
    results = store.search_janda(
        "betting strategy",
        filters={"streets": ["flop"], "pot_types": ["single_raised"]},
        top_k=3
    )
    print(f"Search with filters returned {len(results)} results:")
    for r in results:
        print(f"  {r['chunk_id']}: {r['title']} (score: {r['score']:.3f})")


if __name__ == "__main__":
    main()
