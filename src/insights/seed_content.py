"""Seed poker_content collection with Chapter 1: Opening the Pot."""

from google.cloud import firestore


def seed_chapter1():
    """Seed content from Opening the Pot chapter."""
    db = firestore.Client()
    collection = db.collection("poker_content")

    # Chapter 1: Opening the Pot - extracted terms and concepts
    content_entries = [
        {
            "id": "opening-range",
            "name": "Opening Range",
            "blurb": "The set of hands you choose to raise with when you are first to enter the pot. Your opening range should vary by position - tighter from early position, wider from late position.",
            "body": None,
            "category": "ranges",
        },
        {
            "id": "fold-equity",
            "name": "Fold Equity",
            "blurb": "The value you gain when opponents fold to your bet or raise. Fold equity is a central concept in poker - some hands are profitable to play primarily because opponents will fold often enough.",
            "body": None,
            "category": "fundamentals",
        },
        {
            "id": "steal",
            "name": "Steal",
            "blurb": "An open raise from late position (CO, BTN, SB) designed primarily to win the blinds uncontested. Steals are profitable when opponents fold frequently enough to offset the times you get called or raised.",
            "body": None,
            "category": "betting-actions",
        },
        {
            "id": "value-raise",
            "name": "Value Raise",
            "blurb": "Opening with a strong hand expecting to get called by worse hands. Value raises aim to build the pot with hands that perform well postflop against opponents' calling ranges.",
            "body": None,
            "category": "betting-actions",
        },
        {
            "id": "semi-steal",
            "name": "Semi-Steal",
            "blurb": "Hands opened partly for value and partly for fold equity. These hands have enough playability to continue profitably when called, but also benefit from opponents folding preflop.",
            "body": None,
            "category": "betting-actions",
        },
        {
            "id": "open-sizing",
            "name": "Open Sizing",
            "blurb": "How much you raise when opening the pot, typically expressed as multiples of the big blind (2.5x, 3x, 4x). Smaller sizing gives you a better price on steals; larger sizing punishes loose callers.",
            "body": None,
            "category": "bet-sizing",
        },
        {
            "id": "utg",
            "name": "UTG (Under the Gun)",
            "blurb": "The first position to act preflop, immediately left of the big blind. UTG requires the tightest opening range because you have the most players left to act behind you.",
            "body": None,
            "category": "position",
        },
        {
            "id": "hijack",
            "name": "Hijack (HJ)",
            "blurb": "The position two seats right of the button. The HJ is where opening ranges start to widen as you have fewer players behind, but it is not yet a true steal position.",
            "body": None,
            "category": "position",
        },
        {
            "id": "cutoff",
            "name": "Cutoff (CO)",
            "blurb": "The position one seat right of the button and the second most profitable seat. The CO is the first true steal position where you can profitably open a wide range due to high fold equity.",
            "body": None,
            "category": "position",
        },
        {
            "id": "button",
            "name": "Button (BTN)",
            "blurb": "The most profitable position in poker, acting last on every postflop street. From the button you can open the widest range because you have maximum fold equity and guaranteed position.",
            "body": None,
            "category": "position",
        },
        {
            "id": "small-blind",
            "name": "Small Blind (SB)",
            "blurb": "The position immediately left of the button that posts a forced half-bet. The SB is the least profitable position because you act first postflop and have already invested chips.",
            "body": None,
            "category": "position",
        },
        {
            "id": "big-blind",
            "name": "Big Blind (BB)",
            "blurb": "The position that posts the full forced bet preflop. The BB closes the action preflop and gets a discount to see flops, but plays out of position postflop.",
            "body": None,
            "category": "position",
        },
        {
            "id": "expected-value",
            "name": "Expected Value (EV)",
            "blurb": "The average amount you expect to win or lose from a decision over the long run. A play is +EV (profitable) if it makes money on average, -EV (unprofitable) if it loses money.",
            "body": None,
            "category": "fundamentals",
        },
        {
            "id": "fish",
            "name": "Fish",
            "blurb": "A weak recreational player who makes fundamental mistakes like playing too many hands, calling too much, and not folding when they should. Fish are your primary source of profit.",
            "body": None,
            "category": "player-types",
        },
        {
            "id": "regular",
            "name": "Regular (Reg)",
            "blurb": "A competent player who plays frequently and understands basic strategy. Regs are harder to profit from than fish and may exploit your weaknesses if you play predictably.",
            "body": None,
            "category": "player-types",
        },
        {
            "id": "effective-stack",
            "name": "Effective Stack",
            "blurb": "The smaller of the two stacks in a heads-up pot, which determines the maximum amount that can be won or lost. Effective stack size significantly impacts which hands are playable.",
            "body": None,
            "category": "fundamentals",
        },
    ]

    print(f"Writing {len(content_entries)} entries to poker_content collection...")
    for entry in content_entries:
        doc_ref = collection.document(entry["id"])
        doc_ref.set(entry)
        print(f"  + {entry['id']}: {entry['name']}")

    print("\nDone! Verifying...")

    # Verify
    docs = collection.stream()
    count = 0
    categories = {}
    for doc in docs:
        count += 1
        data = doc.to_dict()
        cat = data.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    print(f"Total documents in collection: {count}")
    print("By category:")
    for cat, num in sorted(categories.items()):
        print(f"  {cat}: {num}")


if __name__ == "__main__":
    seed_chapter1()
