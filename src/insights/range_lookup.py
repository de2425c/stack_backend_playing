"""
Preflop range lookup from Firebase 8m100bb collection.

Collection structure:
- Documents: "BTN_RFI", "CO_RFI", "HJ_RFI", "LJ_RFI", "SB_RFI", "UTG_RFI", "UTG1_RFI"
  - Fields: range (dict of combos with frequencies), size, action
  - Subcollection "children": "BB_3B", "BB_C", "SB_3B", "SB_C", etc.
    - Fields: range (dict), size, action

Range format: {'KdTs': 0.1541, 'JcTh': 0.869, ...} where value is frequency 0-1

Usage:
    lookup = RangeLookup()
    result = lookup.get_ranges_for_spot("BB vs BTN 3bet")
    # Returns SpotRanges with combo dicts for each player
"""

import logging
import re
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Position ordering (earliest to latest)
POSITIONS = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]

# Aliases for position names
POSITION_ALIASES = {
    "MP": "HJ",
    "BUTTON": "BTN",
    "CUTOFF": "CO",
    "HIJACK": "HJ",
    "LOJACK": "LJ",
    "EP": "UTG",
}

# Action abbreviations used in Firebase
ACTION_ABBREV = {
    "3bet": "3B",
    "call": "C",
    "flat": "C",
    "coldcall": "C",
    "4bet": "4B",
    "5bet": "5B",
}


@dataclass
class SpotRanges:
    """Ranges for a preflop spot."""
    opener_position: str
    opener_range: dict[str, float]  # {'AhKs': 1.0, '9d8c': 0.45, ...}
    responder_position: str
    responder_range: dict[str, float]
    responder_action: str  # "call", "3bet", "4bet", etc.
    spot_description: str

    def to_dict(self) -> dict:
        return {
            "opener": {
                "position": self.opener_position,
                "range": self.opener_range,
                "combos": len(self.opener_range),
                "weighted_combos": sum(self.opener_range.values()),
            },
            "responder": {
                "position": self.responder_position,
                "range": self.responder_range,
                "action": self.responder_action,
                "combos": len(self.responder_range) if self.responder_range else 0,
                "weighted_combos": sum(self.responder_range.values()) if self.responder_range else 0,
            },
            "spot": self.spot_description,
        }

    def get_opener_combos(self, min_freq: float = 0.0) -> list[str]:
        """Get opener combos above frequency threshold."""
        return [h for h, f in self.opener_range.items() if f > min_freq]

    def get_responder_combos(self, min_freq: float = 0.0) -> list[str]:
        """Get responder combos above frequency threshold."""
        if not self.responder_range:
            return []
        return [h for h, f in self.responder_range.items() if f > min_freq]


class RangeLookup:
    """
    Lookup preflop ranges from Firebase.

    Parses spot descriptions like:
    - "BB vs BTN 3bet" -> BTN opens, BB 3bets
    - "CO vs HJ call" -> HJ opens, CO calls
    - "BTN vs CO 4bet" -> CO opens, BTN 3bets, CO 4bets (future)
    """

    COLLECTION = "8m100bb"

    def __init__(self, use_memory: bool = False):
        """Initialize with Firestore connection."""
        self._db = None
        self._cache: dict[str, dict] = {}  # Cache fetched ranges

        if not use_memory:
            self._try_init_firestore()

    def _try_init_firestore(self) -> None:
        """Attempt to initialize Firebase Admin SDK."""
        try:
            import firebase_admin
            from firebase_admin import firestore

            if not firebase_admin._apps:
                logger.info("Initializing Firebase Admin SDK for range lookup...")
                firebase_admin.initialize_app()

            self._db = firestore.client()
            logger.info("Range lookup Firestore client initialized")
        except Exception as e:
            logger.warning(f"Firestore init failed for range lookup: {e}")

    @property
    def is_connected(self) -> bool:
        return self._db is not None

    def _normalize_position(self, pos: str) -> str:
        """Normalize position name to standard format."""
        pos_upper = pos.upper().strip()
        return POSITION_ALIASES.get(pos_upper, pos_upper)

    def _parse_spot(self, spot: str) -> Optional[tuple[str, str, str]]:
        """
        Parse spot description into (opener, responder, action).

        Examples:
            "BB vs BTN 3bet" -> ("BTN", "BB", "3bet")
            "CO vs HJ call" -> ("HJ", "CO", "call")
            "SB 3bet vs BTN" -> ("BTN", "SB", "3bet")

        Returns:
            (opener_position, responder_position, responder_action) or None
        """
        spot_clean = spot.strip().upper()

        # Pattern 1: "RESPONDER vs OPENER ACTION" (e.g., "BB vs BTN 3bet")
        match = re.match(
            r"(\w+)\s+VS\s+(\w+)\s+(3BET|CALL|4BET|FLAT|COLD.?CALL)",
            spot_clean
        )
        if match:
            responder = self._normalize_position(match.group(1))
            opener = self._normalize_position(match.group(2))
            action = match.group(3).lower().replace("cold", "").replace("-", "").replace("flat", "call")
            return (opener, responder, action)

        # Pattern 2: "RESPONDER ACTION vs OPENER" (e.g., "SB 3bet vs BTN")
        match = re.match(
            r"(\w+)\s+(3BET|CALL|4BET|FLAT|COLD.?CALL)\s+VS\s+(\w+)",
            spot_clean
        )
        if match:
            responder = self._normalize_position(match.group(1))
            action = match.group(2).lower().replace("cold", "").replace("-", "").replace("flat", "call")
            opener = self._normalize_position(match.group(3))
            return (opener, responder, action)

        # Pattern 3: "OPENER open/RFI" (e.g., "BTN open", "CO RFI")
        match = re.match(r"(\w+)\s+(OPEN|RFI)", spot_clean)
        if match:
            opener = self._normalize_position(match.group(1))
            return (opener, None, "open")

        logger.warning(f"Could not parse spot: {spot}")
        return None

    def _get_rfi_doc_name(self, position: str) -> str:
        """Get the RFI document name for a position (e.g., BTN_RFI)."""
        return f"{position}_RFI"

    def _get_response_doc_name(self, position: str, action: str) -> str:
        """Get the response document name (e.g., BB_3B, SB_C)."""
        action_abbrev = ACTION_ABBREV.get(action.lower(), action.upper())
        return f"{position}_{action_abbrev}"

    def get_ranges_for_spot(self, spot: str) -> Optional[SpotRanges]:
        """
        Get ranges for a preflop spot.

        Args:
            spot: Description like "BB vs BTN 3bet", "CO vs HJ call"

        Returns:
            SpotRanges with both players' ranges, or None if not found
        """
        parsed = self._parse_spot(spot)
        if not parsed:
            return None

        opener, responder, action = parsed

        # Handle RFI-only queries
        if responder is None:
            opener_range = self._fetch_rfi_range(opener)
            if opener_range:
                return SpotRanges(
                    opener_position=opener,
                    opener_range=opener_range,
                    responder_position="",
                    responder_range={},
                    responder_action="open",
                    spot_description=f"{opener} RFI",
                )
            return None

        # Fetch opener's RFI range
        opener_range = self._fetch_rfi_range(opener)
        if not opener_range:
            logger.warning(f"No RFI range found for {opener}")
            return None

        # Fetch responder's range from subcollection
        responder_range = self._fetch_response_range(opener, responder, action)
        if not responder_range:
            logger.warning(f"No {action} range found for {responder} vs {opener}")
            return None

        return SpotRanges(
            opener_position=opener,
            opener_range=opener_range,
            responder_position=responder,
            responder_range=responder_range,
            responder_action=action,
            spot_description=f"{responder} {action} vs {opener} RFI",
        )

    def _fetch_rfi_range(self, position: str) -> Optional[dict[str, float]]:
        """Fetch RFI range for a position."""
        cache_key = f"rfi:{position}"
        if cache_key in self._cache:
            return self._cache[cache_key].get("range")

        if not self._db:
            logger.error("Firestore not connected")
            return None

        doc_name = self._get_rfi_doc_name(position)
        try:
            doc = self._db.collection(self.COLLECTION).document(doc_name).get()
            if doc.exists:
                data = doc.to_dict()
                self._cache[cache_key] = data
                return data.get("range")
            else:
                logger.warning(f"Document not found: {self.COLLECTION}/{doc_name}")
                return None
        except Exception as e:
            logger.error(f"Error fetching RFI range: {e}")
            return None

    def _fetch_response_range(
        self, opener: str, responder: str, action: str
    ) -> Optional[dict[str, float]]:
        """Fetch response range from subcollection."""
        cache_key = f"response:{opener}:{responder}:{action}"
        if cache_key in self._cache:
            return self._cache[cache_key].get("range")

        if not self._db:
            logger.error("Firestore not connected")
            return None

        rfi_doc_name = self._get_rfi_doc_name(opener)
        response_doc_name = self._get_response_doc_name(responder, action)

        try:
            doc = (
                self._db.collection(self.COLLECTION)
                .document(rfi_doc_name)
                .collection("children")  # Subcollection is named "children"
                .document(response_doc_name)
                .get()
            )
            if doc.exists:
                data = doc.to_dict()
                self._cache[cache_key] = data
                return data.get("range")
            else:
                logger.warning(
                    f"Response not found: {self.COLLECTION}/{rfi_doc_name}/children/{response_doc_name}"
                )
                return None
        except Exception as e:
            logger.error(f"Error fetching response range: {e}")
            return None

    def list_available_spots(self) -> list[str]:
        """List all available preflop spots in the database."""
        if not self._db:
            return []

        spots = []
        try:
            # Get all RFI documents
            rfi_docs = self._db.collection(self.COLLECTION).stream()
            for rfi_doc in rfi_docs:
                rfi_name = rfi_doc.id  # e.g., "BTN_RFI"
                spots.append(rfi_name)

                # Get children subcollection
                children = (
                    self._db.collection(self.COLLECTION)
                    .document(rfi_doc.id)
                    .collection("children")
                    .stream()
                )
                for child_doc in children:
                    spots.append(f"{child_doc.id} vs {rfi_name}")
        except Exception as e:
            logger.error(f"Error listing spots: {e}")

        return spots


# Convenience function for quick lookups
def get_preflop_ranges(spot: str) -> Optional[dict]:
    """
    Quick lookup for preflop ranges.

    Args:
        spot: e.g., "BB vs BTN 3bet", "CO vs HJ call"

    Returns:
        Dict with opener and responder ranges, or None
    """
    lookup = RangeLookup()
    result = lookup.get_ranges_for_spot(spot)
    return result.to_dict() if result else None
