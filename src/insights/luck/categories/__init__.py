"""Registry of luck-rating categories."""

from .allin_ev import AllInEVCategory
from .flop import FlopCategory
from .hand_strength import HandStrengthCategory

_CATEGORIES = [AllInEVCategory(), HandStrengthCategory(), FlopCategory()]


def all_categories() -> list:
    return list(_CATEGORIES)
