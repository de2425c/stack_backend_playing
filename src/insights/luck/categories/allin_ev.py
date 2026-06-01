"""All-in EV: did the hero run above or below expectation when all-in?

For each hand where hero went all-in, computes hero's equity at the all-in
street and compares the expected pot share to what hero actually won.

Two cases are handled:
  - Contested all-in (≥1 villain didn't fold): full equity calc vs revealed
    villain cards. Contributes to both sample and variance.
  - Uncalled all-in (all villains folded): hero won deterministically, so
    equity=1.0, EV diff=0, variance contribution=0. Still counts as +1
    sample so the category surfaces even on sessions where every shove
    got through (otherwise the UI shows nothing for "I went all-in once").
"""

from __future__ import annotations
import logging
import math
from typing import Optional

from ..base import LuckCategoryResult
from ..equity import hero_equity, parse_cards
from ..stats import z_to_percentile

logger = logging.getLogger(__name__)


_STREET_BOARD_CARDS = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


class AllInEVCategory:
    category_id = "allin_ev"
    name = "All-in EV"

    def compute(self, hands: list[dict], hero_user_id: str) -> Optional[LuckCategoryResult]:
        per_hand: list[dict] = []
        total_actual = 0
        total_expected = 0.0
        total_invested = 0
        big_blind = 0

        for hand in hands:
            entry = _analyze_hand(hand, hero_user_id)
            if entry is None:
                continue
            per_hand.append(entry)
            total_actual += entry["actual_won_cents"]
            total_expected += entry["_expected_won_raw"]
            total_invested += entry["hero_invested_cents"]
            big_blind = big_blind or hand.get("big_blind") or 0

        if not per_hand:
            return None

        delta_cents = total_actual - total_expected
        delta_bb = (delta_cents / big_blind) if big_blind else None

        public_hands = [
            {k: v for k, v in h.items() if not k.startswith("_")}
            for h in per_hand
        ]

        # Bernoulli-style variance per all-in: outcome bounded by pot, modelled as
        # win/lose the whole pot at probability `equity`. Sum across all-ins (independent).
        var_cents2 = 0.0
        for h in per_hand:
            eq = h["equity"]
            pot = h["pot_cents"]
            var_cents2 += eq * (1.0 - eq) * (pot ** 2)
        sd_cents = math.sqrt(var_cents2) if var_cents2 > 0 else 0.0
        z = (delta_cents / sd_cents) if sd_cents > 0 else 0.0

        if delta_bb is None:
            headline = f"All-in {len(per_hand)}x, {int(round(delta_cents)):+d}¢ vs EV"
        else:
            headline = f"All-in {len(per_hand)}x, {delta_bb:+.1f} BB vs EV"

        return LuckCategoryResult(
            category_id=self.category_id,
            name=self.name,
            sample_size=len(per_hand),
            headline=headline,
            metrics={
                "z_score": round(z, 2),
                "percentile": round(z_to_percentile(z), 4),
                "ev_diff_cents": int(round(delta_cents)),
                "ev_diff_bb": round(delta_bb, 2) if delta_bb is not None else None,
                "total_invested_cents": int(total_invested),
                "total_actual_won_cents": int(total_actual),
                "total_expected_won_cents": int(round(total_expected)),
            },
            details={"hands": public_hands},
        )


def _analyze_hand(hand: dict, hero_user_id: str) -> Optional[dict]:
    hero_seat = _hero_seat(hand, hero_user_id)
    if hero_seat is None:
        return None

    actions = hand.get("actions", [])
    folded_seats = {a.get("seat") for a in actions if a.get("action") == "fold"}

    # Hero must reach showdown (i.e., not fold) for an EV calc to be meaningful.
    if hero_seat in folded_seats:
        return None

    seats = hand.get("seats", [])
    villain_seats = [
        s.get("seat_index") for s in seats
        if s.get("seat_index") != hero_seat and s.get("seat_index") not in folded_seats
    ]
    if not villain_seats:
        # Uncalled shove — no variance to measure, correct to skip.
        return None

    # Trigger on the latest all-in action by anyone still in the hand. The
    # engine sets `is_all_in=True` only when the ACTING player's stack hits 0,
    # so a hero who CALLS villain's shove with a covering stack is not flagged
    # — but it's still a contested all-in from hero's perspective with no
    # future streets to fold. Using "any non-folded seat's all-in action"
    # catches that case (was the original bug: hand_85b02193075c, 2026-05-17).
    allin_actions = [
        a for a in actions
        if a.get("is_all_in") and a.get("seat") not in folded_seats
    ]
    if not allin_actions:
        return None

    # The all-in street is the street of the last all-in action — that's when
    # hero's decision became irreversible. (Pre-fix this came from hero's
    # first all-in action only, which broke when villain shoved later streets.)
    allin_street = allin_actions[-1].get("street", "preflop")
    board_cards_revealed = _STREET_BOARD_CARDS.get(allin_street, 0)

    hole_cards = hand.get("hole_cards", {})
    hero_raw = hole_cards.get(str(hero_seat)) or hole_cards.get(hero_seat)
    if not hero_raw or len(hero_raw) != 2:
        return None

    villain_raw = []
    for vs in villain_seats:
        vc = hole_cards.get(str(vs)) or hole_cards.get(vs)
        if vc and len(vc) == 2:
            villain_raw.append(vc)
    if not villain_raw:
        return None

    board_raw = (hand.get("board") or [])[:board_cards_revealed]

    try:
        hero_cards = parse_cards(hero_raw)
        villain_hands = [parse_cards(v) for v in villain_raw]
        board = parse_cards(board_raw)
    except Exception:
        return None

    equity = hero_equity(hero_cards, villain_hands, board)

    hero_invested = _seat_total_contribution(actions, hero_seat)
    pot_contested = hero_invested + sum(_seat_total_contribution(actions, vs) for vs in villain_seats)
    # winners[*].amount_won is the NET payoff from hand_logger.py (profit, not gross
    # share of pot). expected_won below is GROSS (equity * pot). Convert actual to
    # gross by adding back the hero's investment when the hero is in the winners
    # list — otherwise a winning favorite is reported as running below EV.
    hero_net_won = sum(
        w.get("amount_won", 0) for w in hand.get("winners", []) if w.get("seat") == hero_seat
    )
    hero_in_winners = any(w.get("seat") == hero_seat for w in hand.get("winners", []))
    actual_won = hero_net_won + (hero_invested if hero_in_winners else 0)
    expected_won = equity * pot_contested

    return {
        "hand_id": hand.get("hand_id"),
        "street": allin_street,
        "equity": round(equity, 4),
        "pot_cents": pot_contested,
        "hero_invested_cents": hero_invested,
        "actual_won_cents": actual_won,
        "expected_won_cents": int(round(expected_won)),
        "ev_diff_cents": int(round(actual_won - expected_won)),
        "_expected_won_raw": expected_won,
    }


def _hero_seat(hand: dict, hero_user_id: str) -> Optional[int]:
    for s in hand.get("seats", []):
        if s.get("user_id") == hero_user_id:
            return s.get("seat_index")
    return None


def _seat_total_contribution(actions: list[dict], seat: int) -> int:
    """Sum each street's max committed amount for `seat`.

    Action records store cumulative-per-street amounts (matching the
    pot-bookkeeping convention in src/persistence/hand_logger.py), so the
    per-street commitment is the max amount logged on that street.
    """
    per_street: dict[str, int] = {}
    for a in actions:
        if a.get("seat") != seat:
            continue
        amt = a.get("amount")
        if amt is None:
            continue
        s = a.get("street", "preflop")
        per_street[s] = max(per_street.get(s, 0), amt)
    return sum(per_street.values())
