"""
Table configuration for poker games.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableConfig:
    """
    Configuration for a poker table.

    All monetary values are in cents.
    """
    stake_id: str = "nlh_1_2"      # Stakes identifier for logging
    max_players: int = 6
    min_players_to_start: int = 3  # 2 for HU, 3 for 6-max
    small_blind_cents: int = 100   # $1
    big_blind_cents: int = 200     # $2
    min_buy_in_cents: int = 4000   # 20bb
    max_buy_in_cents: int = 40000  # 200bb
    action_timeout_seconds: int = 60
    bot_early_street_timeout_seconds: int = 5  # Bot timeout on preflop/flop
    # Grace window applied SERVER-SIDE when validating an incoming action
    # (handler.handle_action) and when the timer service auto-fires a
    # timeout (timer.is_expired). Clients still see the original
    # `expires_at_ms` so the ring drains to zero on screen, but a user
    # who tapped fold at deadline-30ms and was delayed by network won't
    # be auto-folded if they land within the grace window. Real poker
    # rooms call this the "time bank cushion."
    action_timeout_grace_ms: int = 1500
