"""Pot equity computation backed by pokerkit's hand evaluator."""

from __future__ import annotations
import itertools
import random
from math import comb
from typing import Iterable

from pokerkit import StandardHighHand, Card


_FULL_DECK: list[Card] | None = None


def _deck() -> list[Card]:
    global _FULL_DECK
    if _FULL_DECK is None:
        _FULL_DECK = [Card(r, s) for r in "AKQJT98765432" for s in "hdcs"]
    return _FULL_DECK


def parse_cards(card_strs: Iterable[str]) -> list[Card]:
    out: list[Card] = []
    for c in card_strs:
        parsed = list(Card.parse(c))
        if len(parsed) != 1:
            raise ValueError(f"expected single card, got {c!r} -> {parsed}")
        out.append(parsed[0])
    return out


def _eval_showdown(hero: list[Card], villains: list[list[Card]], board: list[Card]) -> float:
    """Hero's pot share at showdown: 1.0 outright, 1/N on N-way tie, 0.0 loss."""
    hero_h = StandardHighHand.from_game(hero, board)
    villain_hs = [StandardHighHand.from_game(v, board) for v in villains]
    for v in villain_hs:
        if v > hero_h:
            return 0.0
    n_ties = 1 + sum(1 for v in villain_hs if v == hero_h)
    return 1.0 / n_ties


def hero_equity_vs_random(
    hero: list[Card],
    board: list[Card],
    mc_iters: int = 200,
    seed: int = 0,
) -> float:
    """Hero's pot equity on a (possibly partial) board vs. a uniformly random villain hand.

    Single MC loop that samples villain's 2 cards + any remaining board cards together,
    then evaluates showdown. Equivalent to averaging `hero_equity(hero, [v], board)` over
    all villain combos consistent with the dead cards.
    """
    used = set(hero + board)
    remaining = [c for c in _deck() if c not in used]
    cards_to_deal = (5 - len(board)) + 2
    rng = random.Random(seed)
    wins = 0.0
    for _ in range(mc_iters):
        sample = rng.sample(remaining, cards_to_deal)
        villain = sample[:2]
        full_board = board + sample[2:]
        wins += _eval_showdown(hero, [villain], full_board)
    return wins / mc_iters


def hero_equity(
    hero: list[Card],
    villains: list[list[Card]],
    board: list[Card],
    mc_iters: int = 2000,
    exhaustive_cap: int = 5000,
    seed: int = 0,
) -> float:
    """Compute hero's pot equity vs. one or more villains given a partial board."""
    used = set(hero + board)
    for v in villains:
        used.update(v)
    remaining = [c for c in _deck() if c not in used]
    cards_to_deal = 5 - len(board)

    if cards_to_deal == 0:
        return _eval_showdown(hero, villains, board)

    n_combos = comb(len(remaining), cards_to_deal)
    if n_combos <= exhaustive_cap:
        wins = 0.0
        total = 0
        for ru in itertools.combinations(remaining, cards_to_deal):
            wins += _eval_showdown(hero, villains, board + list(ru))
            total += 1
        return wins / total if total else 0.0

    rng = random.Random(seed)
    wins = 0.0
    for _ in range(mc_iters):
        ru = rng.sample(remaining, cards_to_deal)
        wins += _eval_showdown(hero, villains, board + ru)
    return wins / mc_iters
