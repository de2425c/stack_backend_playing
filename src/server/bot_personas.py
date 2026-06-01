"""
Bot persona pool for realistic duel opponents.

Bot personas are stored in the `users` collection with a `bot_` prefix on the ID,
making them indistinguishable from real users to the client.
"""

import random
from datetime import datetime, timedelta
from typing import Optional

# Pool of 70 realistic bot personas
# Mix of: casual names, poker-themed handles, gamer-style names
BOT_PERSONAS = [
    # Casual real-looking names
    {"username": "mike_p92", "displayName": "Mike P.", "location": "Las Vegas, NV"},
    {"username": "sarah_k", "displayName": "Sarah K", "location": "Los Angeles, CA"},
    {"username": "jchen88", "displayName": "Jason Chen", "location": "San Francisco, CA"},
    {"username": "alexm_poker", "displayName": "Alex M", "location": "New York, NY"},
    {"username": "danielr", "displayName": "Daniel R", "location": "Miami, FL"},
    {"username": "emilyw23", "displayName": "Emily W", "location": "Austin, TX"},
    {"username": "chris_nyc", "displayName": "Chris", "location": "Brooklyn, NY"},
    {"username": "natalie_j", "displayName": "Natalie J", "location": "Chicago, IL"},
    {"username": "marcus_t", "displayName": "Marcus T", "location": "Atlanta, GA"},
    {"username": "jenny_liu", "displayName": "Jenny Liu", "location": "Houston, TX"},
    {"username": "bwilliams34", "displayName": "Brandon W", "location": "Philadelphia, PA"},
    {"username": "amanda_r", "displayName": "Amanda R", "location": "Phoenix, AZ"},
    {"username": "kevin_oh", "displayName": "Kevin Oh", "location": "Irvine, CA"},
    {"username": "rachelm88", "displayName": "Rachel M", "location": "San Diego, CA"},
    {"username": "tyler_b", "displayName": "Tyler B", "location": "Charlotte, NC"},
    {"username": "jessica_ng", "displayName": "Jessica Ng", "location": "San Jose, CA"},
    {"username": "dave_poker", "displayName": "Dave", "location": "Scottsdale, AZ"},
    {"username": "megan_c", "displayName": "Megan C", "location": "Raleigh, NC"},
    {"username": "andrewkim", "displayName": "Andrew Kim", "location": "Seattle, WA"},
    {"username": "lauren_h", "displayName": "Lauren H", "location": "Nashville, TN"},
    {"username": "jrod_99", "displayName": "J-Rod", "location": "Detroit, MI"},
    {"username": "samantha_p", "displayName": "Sam P", "location": "Orlando, FL"},
    {"username": "nick_vegas", "displayName": "Nick V", "location": "Las Vegas, NV"},
    {"username": "ashley_t", "displayName": "Ashley T", "location": "Salt Lake City, UT"},
    {"username": "matt_cards", "displayName": "Matt", "location": "Columbus, OH"},
    {"username": "steph_w", "displayName": "Steph W", "location": "Indianapolis, IN"},
    {"username": "ryan_ace", "displayName": "Ryan", "location": "Jacksonville, FL"},
    {"username": "kim_j", "displayName": "Kim J", "location": "Fort Worth, TX"},
    {"username": "tony_chips", "displayName": "Tony", "location": "Milwaukee, WI"},
    {"username": "lisa_m22", "displayName": "Lisa M", "location": "Memphis, TN"},

    # Poker-themed handles
    {"username": "river_king", "displayName": "River King", "location": "Atlantic City, NJ"},
    {"username": "nutflush", "displayName": "NutFlush", "location": "Henderson, NV"},
    {"username": "pocket_aces", "displayName": "Pocket Aces", "location": "Phoenix, AZ"},
    {"username": "bluffmaster", "displayName": "Bluff Master", "location": "Seattle, WA"},
    {"username": "the_grinder", "displayName": "The Grinder", "location": "Denver, CO"},
    {"username": "allin_andy", "displayName": "All-In Andy", "location": "Portland, OR"},
    {"username": "setminer", "displayName": "Set Miner", "location": "Reno, NV"},
    {"username": "valuebet_vic", "displayName": "Value Vic", "location": "Biloxi, MS"},
    {"username": "checkraise_carl", "displayName": "CheckRaise Carl", "location": "Tunica, MS"},
    {"username": "floatqueen", "displayName": "Float Queen", "location": "Lake Tahoe, CA"},
    {"username": "potodds_pete", "displayName": "PotOdds Pete", "location": "Laughlin, NV"},
    {"username": "broadway_bob", "displayName": "Broadway Bob", "location": "New York, NY"},
    {"username": "suited_sam", "displayName": "Suited Sam", "location": "San Antonio, TX"},
    {"username": "donkbet_dan", "displayName": "Donkbet Dan", "location": "Oklahoma City, OK"},
    {"username": "barrel_queen", "displayName": "Barrel Queen", "location": "Kansas City, MO"},
    {"username": "cbet_king", "displayName": "C-Bet King", "location": "St. Louis, MO"},
    {"username": "overbet_olivia", "displayName": "Overbet Olivia", "location": "Albuquerque, NM"},
    {"username": "triple_barrel", "displayName": "Triple Barrel", "location": "Tulsa, OK"},

    # Gamer-style names
    {"username": "xx_shark_xx", "displayName": "Shark", "location": "Dallas, TX"},
    {"username": "coldeck99", "displayName": "ColdDeck", "location": "Boston, MA"},
    {"username": "tiltproof", "displayName": "TiltProof", "location": "San Diego, CA"},
    {"username": "felt_ninja", "displayName": "Felt Ninja", "location": "Tampa, FL"},
    {"username": "stackattack", "displayName": "StackAttack", "location": "Nashville, TN"},
    {"username": "chipleader_", "displayName": "Chip Leader", "location": "Minneapolis, MN"},
    {"username": "iceman_poker", "displayName": "Iceman", "location": "Buffalo, NY"},
    {"username": "silentassassin", "displayName": "Silent Assassin", "location": "Cleveland, OH"},
    {"username": "cardshark_x", "displayName": "CardShark", "location": "Pittsburgh, PA"},
    {"username": "nightowl_poker", "displayName": "Night Owl", "location": "Tucson, AZ"},
    {"username": "steelnerves", "displayName": "Steel Nerves", "location": "Baltimore, MD"},
    {"username": "phantom_player", "displayName": "Phantom", "location": "Louisville, KY"},
    {"username": "lucky_7s", "displayName": "Lucky 7s", "location": "Richmond, VA"},
    {"username": "headsup_hero", "displayName": "HU Hero", "location": "Hartford, CT"},
    {"username": "cash_crusher", "displayName": "Cash Crusher", "location": "Providence, RI"},
    {"username": "table_captain", "displayName": "Table Captain", "location": "Birmingham, AL"},
    {"username": "royal_flush_rx", "displayName": "Royal Flush", "location": "New Orleans, LA"},
    {"username": "deadmans_hand", "displayName": "Dead Man's Hand", "location": "Deadwood, SD"},
    {"username": "ace_high_aj", "displayName": "Ace High", "location": "Omaha, NE"},
    {"username": "final_table_ft", "displayName": "Final Table", "location": "Des Moines, IA"},
]


def generate_persona_id(username: str) -> str:
    """Generate a user ID for a bot persona."""
    return f"bot_{username}"


def generate_join_date() -> datetime:
    """Generate a believable join date (1-18 months ago)."""
    days_ago = random.randint(30, 540)
    return datetime.utcnow() - timedelta(days=days_ago)


def get_persona_user_doc(persona: dict) -> dict:
    """
    Build a Firestore user document for a bot persona.

    Matches the schema used by real users in the `users` collection.
    """
    username = persona["username"]
    return {
        "id": generate_persona_id(username),
        "username": username,
        "usernameSearchable": username.lower(),  # For search indexing
        "displayName": persona["displayName"],
        "displayNameSearchable": persona["displayName"].lower(),
        "location": persona.get("location"),
        "createdAt": generate_join_date(),
        "favoriteGame": "No Limit Hold'em",
        "bio": None,
        "avatarURL": None,  # Use default avatar
        # Mark as bot for backend logic (not exposed to client)
        "_isBot": True,
    }


class BotPersonaPool:
    """
    Manages assignment of bot personas to duel opponents.

    Tracks which personas are currently in use to avoid duplicates.
    """

    def __init__(self, firestore_client=None):
        self._firestore = firestore_client
        self._in_use: set[str] = set()  # persona_ids currently assigned

    async def ensure_personas_exist(self) -> None:
        """
        Seed bot personas into Firestore if they don't exist.

        Called on server startup.
        """
        if not self._firestore or not self._firestore._db:
            print("[BOT_PERSONAS] Firestore not available, skipping seed")
            return

        db = self._firestore._db
        batch = db.batch()
        created = 0

        for persona in BOT_PERSONAS:
            persona_id = generate_persona_id(persona["username"])
            doc_ref = db.collection("users").document(persona_id)
            doc = doc_ref.get()

            if not doc.exists:
                user_doc = get_persona_user_doc(persona)
                # Convert datetime to Firestore timestamp
                user_doc["createdAt"] = user_doc["createdAt"]
                batch.set(doc_ref, user_doc)
                created += 1

        if created > 0:
            batch.commit()
            print(f"[BOT_PERSONAS] Created {created} bot personas in Firestore")
        else:
            print(f"[BOT_PERSONAS] All {len(BOT_PERSONAS)} personas already exist")

    async def get_available_persona(self, target_rating: float = 1500) -> Optional[dict]:
        """
        Get an available bot persona, preferring ones with similar rating.

        Args:
            target_rating: Target Glicko rating to match against

        Returns:
            Persona dict with id, username, displayName, or None if all in use
        """
        available = []

        for persona in BOT_PERSONAS:
            persona_id = generate_persona_id(persona["username"])
            if persona_id not in self._in_use:
                # Get rating from duel_ratings if available
                rating = 1500  # Default
                if self._firestore:
                    rating_doc = await self._firestore.get_duel_rating(persona_id)
                    if rating_doc:
                        rating = rating_doc.get("rating", 1500)

                available.append({
                    "persona_id": persona_id,
                    "username": persona["username"],
                    "displayName": persona["displayName"],
                    "rating": rating,
                    "rating_diff": abs(rating - target_rating),
                })

        if not available:
            return None

        # Sort by rating difference, pick from top 5 randomly for variety
        available.sort(key=lambda x: x["rating_diff"])
        top_matches = available[:min(5, len(available))]
        chosen = random.choice(top_matches)

        # Mark as in use
        self._in_use.add(chosen["persona_id"])

        return {
            "persona_id": chosen["persona_id"],
            "username": chosen["username"],
            "displayName": chosen["displayName"],
        }

    def release_persona(self, persona_id: str) -> None:
        """Release a persona back to the pool after duel ends."""
        self._in_use.discard(persona_id)

    def get_display_name(self, persona_id: str) -> str:
        """Get display name for a persona ID."""
        for persona in BOT_PERSONAS:
            if generate_persona_id(persona["username"]) == persona_id:
                return persona["displayName"]
        return "Bot"


# Global instance
_persona_pool: Optional[BotPersonaPool] = None


def get_persona_pool(firestore_client=None) -> BotPersonaPool:
    """Get or create the global bot persona pool."""
    global _persona_pool
    if _persona_pool is None:
        _persona_pool = BotPersonaPool(firestore_client)
    elif firestore_client and _persona_pool._firestore is None:
        _persona_pool._firestore = firestore_client
    return _persona_pool
