"""
Glicko rating system for heads-up duel mode.

Implements the Glicko-1 rating system for calculating player ratings
based on match outcomes. Similar to chess.com's rating system.

Constants:
- Initial rating: 1500
- Initial RD: 350 (high uncertainty for new players)
- Min RD: 50 (floor for active players - ratings become "sticky")
- RD increases by 10 per 30 days of inactivity
"""

import math
from datetime import datetime
from typing import Optional

# Glicko constants
Q = math.log(10) / 400  # ≈ 0.00575646
INITIAL_RATING = 1500.0
INITIAL_RD = 350.0
MIN_RD = 50.0
MAX_RD = 350.0  # Cap RD at initial value
RD_INCREASE_PER_PERIOD = 10.0  # RD increase per 30 days inactive
PERIOD_DAYS = 30


def g(rd: float) -> float:
    """
    Glicko g function - reduces impact of uncertain opponents.

    The g function decreases as RD increases, meaning that
    games against opponents with high RD (uncertain rating)
    have less impact on your rating.
    """
    return 1.0 / math.sqrt(1 + 3 * Q**2 * rd**2 / math.pi**2)


def expected_score(rating: float, opp_rating: float, opp_rd: float) -> float:
    """
    Expected probability of winning against opponent.

    Args:
        rating: Your rating
        opp_rating: Opponent's rating
        opp_rd: Opponent's rating deviation

    Returns:
        Probability between 0 and 1
    """
    return 1.0 / (1 + 10**(-g(opp_rd) * (rating - opp_rating) / 400))


def update_rd_for_inactivity(
    rd: float,
    last_played: Optional[datetime],
) -> float:
    """
    Increase RD if player has been inactive.

    Rating deviation increases over time when a player doesn't play,
    reflecting increased uncertainty about their true skill level.

    Args:
        rd: Current rating deviation
        last_played: When the player last played (None = never played)

    Returns:
        Updated RD (capped at INITIAL_RD)
    """
    if last_played is None:
        return INITIAL_RD

    days_inactive = (datetime.utcnow() - last_played).days
    periods = days_inactive // PERIOD_DAYS
    new_rd = rd + RD_INCREASE_PER_PERIOD * periods
    return min(MAX_RD, new_rd)


def calculate_new_rating(
    rating: float,
    rd: float,
    opp_rating: float,
    opp_rd: float,
    score: float,  # 1.0 for win, 0.0 for loss
) -> tuple[float, float]:
    """
    Calculate new rating and RD after a single game.

    The Glicko system updates both rating and rating deviation:
    - Rating moves toward expected based on outcome
    - RD decreases (more certainty) after each game

    Args:
        rating: Your current rating
        rd: Your current rating deviation
        opp_rating: Opponent's rating
        opp_rd: Opponent's rating deviation
        score: 1.0 for win, 0.0 for loss

    Returns:
        Tuple of (new_rating, new_rd)
    """
    g_opp = g(opp_rd)
    e = expected_score(rating, opp_rating, opp_rd)

    # d² term - measures information gained from this game
    d_squared = 1.0 / (Q**2 * g_opp**2 * e * (1 - e))

    # New rating - moves based on (actual - expected) outcome
    new_rating = rating + (Q / (1/rd**2 + 1/d_squared)) * g_opp * (score - e)

    # New RD - decreases with each game played (more certainty)
    new_rd = math.sqrt(1.0 / (1/rd**2 + 1/d_squared))
    new_rd = max(MIN_RD, new_rd)  # Floor at MIN_RD

    return new_rating, new_rd


def get_default_rating() -> tuple[float, float]:
    """Get default rating and RD for new players."""
    return INITIAL_RATING, INITIAL_RD
