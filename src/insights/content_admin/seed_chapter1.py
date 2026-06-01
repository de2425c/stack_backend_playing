"""Seed poker_content collection with Chapter 1: Introduction."""

from google.cloud import firestore


def clear_and_seed_chapter1():
    """Clear existing content and seed Chapter 1."""
    db = firestore.Client()
    collection = db.collection("poker_content")

    # Clear existing documents
    print("Clearing existing content...")
    docs = collection.stream()
    for doc in docs:
        doc.reference.delete()
    print("Cleared.")

    # Chapter 1: Introduction - Terms and Concepts
    content_entries = [
        # TERMS - Fundamentals
        {
            "id": "expected-value",
            "name": "Expected Value (EV)",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
        {
            "id": "positive-ev",
            "name": "+EV (Profitable)",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
        {
            "id": "negative-ev",
            "name": "-EV (Unprofitable)",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
        {
            "id": "variance",
            "name": "Variance",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
        {
            "id": "all-in-ev",
            "name": "All-in EV",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
        # TERMS - Terminology
        {
            "id": "hero",
            "name": "Hero",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "terminology",
            "chapter": 1,
        },
        {
            "id": "villain",
            "name": "Villain",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "terminology",
            "chapter": 1,
        },
        # TERMS - Mental Game
        {
            "id": "tilt",
            "name": "Tilt",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "mental-game",
            "chapter": 1,
        },
        # TERMS - Practical Game
        {
            "id": "bankroll",
            "name": "Bankroll",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "practical-game",
            "chapter": 1,
        },
        {
            "id": "winrate",
            "name": "Winrate",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "practical-game",
            "chapter": 1,
        },
        {
            "id": "table-selection",
            "name": "Table Selection",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "practical-game",
            "chapter": 1,
        },
        {
            "id": "hand-history",
            "name": "Hand History",
            "type": "term",
            "blurb": None,
            "body": None,
            "category": "practical-game",
            "chapter": 1,
        },
        # CONCEPTS
        {
            "id": "ev-maximization",
            "name": "EV Maximization",
            "type": "concept",
            "blurb": None,
            "body": None,
            "category": "fundamentals",
            "chapter": 1,
        },
    ]

    print(f"Seeding {len(content_entries)} entries from Chapter 1...")
    for entry in content_entries:
        doc_ref = collection.document(entry["id"])
        doc_ref.set(entry)
        print(f"  + [{entry['type']}] {entry['id']}: {entry['name']}")

    print("\nDone! Summary:")

    # Summary
    docs = list(collection.stream())
    terms = [d for d in docs if d.to_dict().get("type") == "term"]
    concepts = [d for d in docs if d.to_dict().get("type") == "concept"]

    print(f"  Total: {len(docs)}")
    print(f"  Terms: {len(terms)}")
    print(f"  Concepts: {len(concepts)}")

    categories = {}
    for doc in docs:
        cat = doc.to_dict().get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    print("  By category:")
    for cat, num in sorted(categories.items()):
        print(f"    {cat}: {num}")


if __name__ == "__main__":
    clear_and_seed_chapter1()
