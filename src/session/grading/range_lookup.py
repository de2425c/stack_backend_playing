"""GTO range lookup from Firestore."""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...persistence import FirestoreClient

logger = logging.getLogger(__name__)


# 8-max positions in order from UTG
POSITIONS_8MAX = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]

# 6-max positions in order from UTG
POSITIONS_6MAX = ["UTG", "HJ", "CO", "BTN", "SB", "BB"]

# Map 6-max positions to 8-max equivalents (skip UTG, UTG1)
# 6-max UTG = 8-max LJ (first to act in 6-max = third to act in 8-max)
POSITION_6MAX_TO_8MAX = {
    "UTG": "LJ",
    "HJ": "HJ",
    "CO": "CO",
    "BTN": "BTN",
    "SB": "SB",
    "BB": "BB",
}

# Available HU stack size collections (sorted)
HU_STACK_SIZES = [2, 3, 4, 5, 6, 7, 10, 15, 30, 40, 50, 100]


def get_closest_hu_stack(effective_bb: float) -> int:
    """
    Get the closest available HU stack size collection.

    When equidistant between two sizes, prefers the larger one (safer for ranges).

    Args:
        effective_bb: Effective stack size in big blinds

    Returns:
        The closest available stack size (2, 3, 4, 5, 6, 7, 10, 15, 30, 40, 50, or 100)
    """
    if effective_bb <= 2:
        return 2
    if effective_bb >= 100:
        return 100

    # Find closest stack size (prefer larger when equidistant)
    closest = HU_STACK_SIZES[0]
    min_diff = abs(effective_bb - closest)

    for size in HU_STACK_SIZES:
        diff = abs(effective_bb - size)
        # Use <= to prefer larger sizes when equidistant
        if diff <= min_diff:
            min_diff = diff
            closest = size

    return closest


def normalize_hand(hand: str) -> str:
    """
    Normalize a hand string to match range format.

    Input formats:
        - "AhAs" (4 chars, no space)
        - "Ah As" (with space)
        - ["Ah", "As"] would be joined first

    Output format matches the solver range format:
        - For pairs: higher suit first (e.g., "AdAc" not "AcAd")
        - For non-pairs: higher rank first (e.g., "KhQh" not "QhKh")

    Suit order: c < d < h < s (alphabetical)
    """
    # Remove any spaces
    hand = hand.replace(" ", "")

    if len(hand) != 4:
        return hand  # Can't normalize, return as-is

    card1 = hand[:2]
    card2 = hand[2:]

    # Card rank order (2 is lowest, A is highest)
    rank_order = "23456789TJQKA"
    # Suit order (c is lowest, s is highest - alphabetical)
    suit_order = "cdhs"

    rank1 = card1[0]
    rank2 = card2[0]
    suit1 = card1[1]
    suit2 = card2[1]

    # If same rank (pair), higher suit comes first
    if rank1 == rank2:
        if suit_order.index(suit1) > suit_order.index(suit2):
            return card1 + card2
        else:
            return card2 + card1

    # Different ranks: higher rank first
    if rank_order.index(rank1) > rank_order.index(rank2):
        return card1 + card2
    else:
        return card2 + card1


class RangeLookup:
    """Lookup GTO ranges from Firestore."""

    def __init__(self, firestore_client: "FirestoreClient"):
        self._db = firestore_client._db
        self._cache: dict[str, dict] = {}  # path -> node data

    def _get_collection_prefix(self, num_seats: int, effective_bb: float = 100) -> str:
        """
        Get the Firestore collection prefix based on table size and stack depth.

        Args:
            num_seats: Number of seats at the table
            effective_bb: Effective stack size in big blinds (only used for HU)

        Returns:
            Collection prefix like "8m100bb" or "hu15bb"
        """
        if num_seats == 2:
            stack_size = get_closest_hu_stack(effective_bb)
            return f"hu{stack_size}bb"
        return "8m100bb"

    def map_position(self, position: str, num_seats: int) -> str:
        """
        Map position for range lookup based on table size.

        For HU (2 seats): No mapping needed, uses BTN/BB directly
        For 6-max or less: Maps to 8-max equivalents (UTG -> LJ)
        For 8-max: No mapping needed
        """
        if num_seats == 2:
            # HU uses BTN and BB directly in hu100bb collection
            return position
        # 6-max to 8-max mapping
        return POSITION_6MAX_TO_8MAX.get(position, position)

    def map_position_6max_to_8max(self, position_6max: str) -> str:
        """
        Map a 6-max position to its 8-max equivalent.

        Args:
            position_6max: Position in 6-max format (UTG, HJ, CO, BTN, SB, BB)

        Returns:
            Position in 8-max format for range lookup
        """
        return POSITION_6MAX_TO_8MAX.get(position_6max, position_6max)

    def _get_node(self, path: str) -> Optional[dict]:
        """
        Get a node from Firestore, with caching.

        Args:
            path: Firestore document path like "8m100bb/BTN_RFI"

        Returns:
            Node data dict or None if not found
        """
        if path in self._cache:
            return self._cache[path]

        if not self._db:
            logger.warning("[RANGE_LOOKUP] No Firestore connection")
            return None

        try:
            doc = self._db.document(path).get()
            if doc.exists:
                data = doc.to_dict()
                self._cache[path] = data
                return data
            else:
                logger.debug(f"[RANGE_LOOKUP] Node not found: {path}")
                return None
        except Exception as e:
            logger.error(f"[RANGE_LOOKUP] Error fetching {path}: {e}")
            return None

    def _build_path(self, spot_path: list[str], num_seats: int = 8, effective_bb: float = 100) -> str:
        """
        Build Firestore path from spot path.

        Args:
            spot_path: List like ["BTN_RFI", "SB_3B", "BTN_Call"]
            num_seats: Number of seats at the table (2 for HU, 6 for 6-max, etc.)
            effective_bb: Effective stack size in big blinds (only used for HU)

        Returns:
            Firestore path like "8m100bb/BTN_RFI/children/SB_3B/children/BTN_Call"
            or "hu15bb/BTN_RFI/children/BB_3B" for HU with 15bb stacks
        """
        if not spot_path:
            return ""

        prefix = self._get_collection_prefix(num_seats, effective_bb)
        parts = [prefix, spot_path[0]]
        for node in spot_path[1:]:
            parts.extend(["children", node])

        return "/".join(parts)

    def get_node_at_spot(self, spot_path: list[str], num_seats: int = 8, effective_bb: float = 100) -> Optional[dict]:
        """
        Get the full node data at a spot.

        Args:
            spot_path: List like ["BTN_RFI", "SB_3B"]
            num_seats: Number of seats at the table
            effective_bb: Effective stack size in big blinds

        Returns:
            Node data with action, size, range, etc.
        """
        path = self._build_path(spot_path, num_seats, effective_bb)
        if not path:
            return None
        return self._get_node(path)

    def get_gto_frequencies(
        self,
        position: str,
        spot_path: list[str],
        hand: str,
        num_seats: int = 8,
        effective_bb: float = 100
    ) -> dict[str, float]:
        """
        Get GTO action frequencies for a hand at a spot.

        Args:
            position: Player's position (6-max format)
            spot_path: Current spot path like ["BTN_RFI"] for facing BTN open
            hand: Hand string like "AhAs"
            num_seats: Number of seats at the table (2 for HU)
            effective_bb: Effective stack size in big blinds

        Returns:
            Dict of {action: frequency} like {"Raise": 0.8, "Call": 0.15, "Fold": 0.05}
        """
        result: dict[str, float] = {}
        normalized_hand = normalize_hand(hand)
        mapped_position = self.map_position(position, num_seats)

        # Verify the spot exists
        current_node = self.get_node_at_spot(spot_path, num_seats, effective_bb)
        if not current_node:
            logger.warning(f"[RANGE_LOOKUP] Spot not found: {spot_path}")
            return result

        # Response ranges are stored in children subcollection
        # e.g., 8m100bb/BTN_RFI/children/BB_C for BB calling vs BTN open

        # Track if we found ANY data for this position's responses
        found_any_data = False
        total_action_freq = 0.0

        # Check for call responses (in children subcollection)
        call_key = f"{mapped_position}_C"
        call_path = spot_path + [call_key]
        call_node = self.get_node_at_spot(call_path, num_seats, effective_bb)
        if call_node and "range" in call_node:
            found_any_data = True
            call_range = call_node["range"]
            freq = call_range.get(normalized_hand, 0)
            if freq > 0:
                result["Call"] = freq
                total_action_freq += freq

        # Check for raise/3-bet responses
        raise_key = f"{mapped_position}_3B"
        raise_path = spot_path + [raise_key]
        raise_node = self.get_node_at_spot(raise_path, num_seats, effective_bb)
        if raise_node and "range" in raise_node:
            found_any_data = True
            raise_range = raise_node["range"]
            freq = raise_range.get(normalized_hand, 0)
            if freq > 0:
                result["Raise"] = freq
                total_action_freq += freq

        # Check for 4-bet responses
        fourbet_key = f"{mapped_position}_4B"
        fourbet_path = spot_path + [fourbet_key]
        fourbet_node = self.get_node_at_spot(fourbet_path, num_seats, effective_bb)
        if fourbet_node and "range" in fourbet_node:
            found_any_data = True
            fourbet_range = fourbet_node["range"]
            freq = fourbet_range.get(normalized_hand, 0)
            if freq > 0:
                result["Raise"] = result.get("Raise", 0) + freq
                total_action_freq += freq

        # If we didn't find ANY response data for this position, return empty
        # This prevents false positives from assuming 100% fold
        if not found_any_data:
            logger.debug(f"[RANGE_LOOKUP] No response data for {mapped_position} at {spot_path}")
            return {}

        # Fold frequency is whatever's left
        fold_freq = max(0, 1.0 - total_action_freq)  # Clamp to 0 in case of rounding
        if fold_freq > 0.001:  # Only include if meaningful
            result["Fold"] = fold_freq

        return result

    def get_rfi_frequencies(
        self,
        position: str,
        hand: str,
        num_seats: int = 8,
        effective_bb: float = 100
    ) -> dict[str, float]:
        """
        Get GTO frequencies for RFI (raise first in) spots.

        For short-stack HU, this may return push/fold (AI) or limp frequencies
        instead of standard RFI.

        Args:
            position: Player's position (6-max format)
            hand: Hand string like "AhAs"
            num_seats: Number of seats at the table (2 for HU)
            effective_bb: Effective stack size in big blinds

        Returns:
            Dict of {action: frequency} like {"Raise": 1.0, "Fold": 0.0}
            or {"Raise": 0.8, "Call": 0.2} for short stack limp/shove
        """
        mapped_position = self.map_position(position, num_seats)
        normalized_hand = normalize_hand(hand)

        prefix = self._get_collection_prefix(num_seats, effective_bb)

        # For HU, check for different opening strategies based on stack depth:
        # - BTN_RFI: Standard raise (15bb+)
        # - BTN_AI: All-in/push (short stacks)
        # - BTN_LIMP: Limp (some stack depths)
        result: dict[str, float] = {}

        if num_seats == 2 and mapped_position == "BTN":
            # Try RFI first (standard raise)
            rfi_path = f"{prefix}/BTN_RFI"
            rfi_node = self._get_node(rfi_path)
            if rfi_node and "range" in rfi_node:
                raise_freq = rfi_node["range"].get(normalized_hand, 0)
                if raise_freq > 0.001:
                    result["Raise"] = raise_freq

            # Try AI (all-in/push)
            ai_path = f"{prefix}/BTN_AI"
            ai_node = self._get_node(ai_path)
            if ai_node and "range" in ai_node:
                ai_freq = ai_node["range"].get(normalized_hand, 0)
                if ai_freq > 0.001:
                    # Treat all-in as a raise
                    result["Raise"] = result.get("Raise", 0) + ai_freq

            # Try LIMP
            limp_path = f"{prefix}/BTN_LIMP"
            limp_node = self._get_node(limp_path)
            if limp_node and "range" in limp_node:
                limp_freq = limp_node["range"].get(normalized_hand, 0)
                if limp_freq > 0.001:
                    result["Call"] = limp_freq  # Limp = call the blind

            # Calculate fold frequency
            total = sum(result.values())
            if total < 0.999:
                result["Fold"] = 1.0 - total

            if result:
                return result

        # Standard RFI lookup for non-HU or if HU lookup failed
        rfi_path = f"{prefix}/{mapped_position}_RFI"
        node = self._get_node(rfi_path)

        if not node:
            logger.warning(f"[RANGE_LOOKUP] RFI node not found: {rfi_path}")
            return {}

        range_data = node.get("range", {})
        raise_freq = range_data.get(normalized_hand, 0)

        if raise_freq > 0.001:
            result["Raise"] = raise_freq
        fold_freq = 1.0 - raise_freq
        if fold_freq > 0.001:
            result["Fold"] = fold_freq

        return result

    def get_spot_sizing(self, spot_path: list[str], num_seats: int = 8, effective_bb: float = 100) -> float:
        """
        Get the expected sizing for a spot.

        Args:
            spot_path: Spot path like ["BTN_RFI", "SB_3B"]
            num_seats: Number of seats at the table
            effective_bb: Effective stack size in big blinds

        Returns:
            Sizing in big blinds (e.g., 2.5 for standard open, 11.0 for 3-bet)
        """
        node = self.get_node_at_spot(spot_path, num_seats, effective_bb)
        if not node:
            return 0.0
        return float(node.get("size", 0))
