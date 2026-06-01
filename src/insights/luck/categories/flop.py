"""Flop run-good: for every flop hero saw, how did the deck treat them?

Headline metric is a z-score of summed flop equity deviations from each
starting hand's baseline preflop equity. Per-flop equity is computed at
runtime via MC vs. a random villain on the actual flop. Per-class flop-equity
variances are precomputed offline (data/flop_baselines.json).

Bucket counters (sets flopped, two-pair+, pair+, flush draws, OESD)
are computed by manual rank/suit pattern matching — for the user-facing
breakdown ("you flopped 3 sets, expected ~1").
"""

from __future__ import annotations
import json
import math
from collections import Counter
from pathlib import Path
from typing import Optional

from ..base import LuckCategoryResult
from ..equity import parse_cards, hero_equity_vs_random
from ..stats import run_quality_headline, z_to_percentile


_PREFLOP_PATH = Path(__file__).resolve().parents[1] / "data/preflop_equity_vs_random.json"
_FLOP_PATH = Path(__file__).resolve().parents[1] / "data/flop_baselines.json"

_PREFLOP_CACHE: dict | None = None
_FLOP_CACHE: dict | None = None

# A=12, K=11, ..., 2=0
_RANK_VAL = {r: i for i, r in enumerate("23456789TJQKA")}
_RANK_ORDER = "AKQJT98765432"  # for hand_class formatting
_RANK_INDEX = {r: i for i, r in enumerate(_RANK_ORDER)}


def _load_preflop() -> dict:
    global _PREFLOP_CACHE
    if _PREFLOP_CACHE is None:
        _PREFLOP_CACHE = json.loads(_PREFLOP_PATH.read_text())
    return _PREFLOP_CACHE


def _load_flop() -> dict:
    global _FLOP_CACHE
    if _FLOP_CACHE is None:
        _FLOP_CACHE = json.loads(_FLOP_PATH.read_text())
    return _FLOP_CACHE


def _hand_class(card1: str, card2: str) -> Optional[str]:
    if len(card1) != 2 or len(card2) != 2:
        return None
    r1, s1 = card1[0], card1[1]
    r2, s2 = card2[0], card2[1]
    if r1 not in _RANK_INDEX or r2 not in _RANK_INDEX:
        return None
    if r1 == r2:
        return r1 + r2
    if _RANK_INDEX[r1] < _RANK_INDEX[r2]:
        hi, lo, hi_s, lo_s = r1, r2, s1, s2
    else:
        hi, lo, hi_s, lo_s = r2, r1, s2, s1
    return f"{hi}{lo}{'s' if hi_s == lo_s else 'o'}"


def _hero_cards(hand: dict, hero_user_id: str) -> Optional[list[str]]:
    for s in hand.get("seats", []):
        if s.get("user_id") == hero_user_id:
            si = s.get("seat_index")
            hc = hand.get("hole_cards", {})
            cards = hc.get(str(si)) or hc.get(si)
            if isinstance(cards, list) and len(cards) == 2:
                return cards
            return None
    return None


def _hero_saw_flop(hand: dict, hero_user_id: str) -> bool:
    """Hero contributed an action at flop or later, or reached showdown."""
    hero_seat = None
    for s in hand.get("seats", []):
        if s.get("user_id") == hero_user_id:
            hero_seat = s.get("seat_index")
            break
    if hero_seat is None:
        return False
    for a in hand.get("actions", []):
        if a.get("seat") == hero_seat and a.get("street") in ("flop", "turn", "river"):
            return True
    for w in hand.get("winners", []):
        if w.get("seat") == hero_seat and (hand.get("board") or []):
            return True
    return False


def classify_flop(hero: list[str], board: list[str]) -> dict:
    """Indicator events for a (hero, flop) — set_plus/two_pair_plus/pair_plus/flush_draw/oesd.

    Counts are non-exclusive: a flopped set also sets two_pair_plus & pair_plus
    so each tier reads as 'X or better'.
    """
    if len(hero) != 2 or len(board) < 3:
        return {"set_plus": False, "two_pair_plus": False, "pair_plus": False,
                "flush_draw": False, "oesd": False, "strong_draw": False}
    flop = board[:3]
    h_ranks = [_RANK_VAL[c[0]] for c in hero]
    h_suits = [c[1] for c in hero]
    b_ranks = [_RANK_VAL[c[0]] for c in flop]
    b_suits = [c[1] for c in flop]

    pocket_pair = h_ranks[0] == h_ranks[1]
    suit_count = Counter(h_suits + b_suits)
    all_ranks = h_ranks + b_ranks

    pair_plus = False
    two_pair_plus = False
    trips_plus = False

    if pocket_pair:
        pair_plus = True
        if h_ranks[0] in b_ranks:
            trips_plus = True
            two_pair_plus = True
        if any(b_ranks.count(r) == 2 for r in set(b_ranks)):
            two_pair_plus = True
    else:
        on_board = [hr for hr in h_ranks if hr in b_ranks]
        if on_board:
            pair_plus = True
        if len(on_board) >= 2:
            two_pair_plus = True
        for hr in set(h_ranks):
            if b_ranks.count(hr) == 2:
                trips_plus = True
                two_pair_plus = True
        if pair_plus and any(b_ranks.count(r) == 2 for r in set(b_ranks)):
            two_pair_plus = True

    # Made flush on flop = all 3 board + 2 hero are the same suit. With only 5
    # cards available the flush takes all 5. Requires hero suited + flop monotone.
    hero_flush = (h_suits[0] == h_suits[1] and suit_count[h_suits[0]] >= 5)

    distinct = sorted(set(all_ranks))
    extended = ([-1] + distinct) if 12 in distinct else distinct

    hero_straight = False
    for i in range(len(extended) - 4):
        w = extended[i:i + 5]
        if w[4] - w[0] == 4:
            wset = {12 if r == -1 else r for r in w}
            if any(r in wset for r in h_ranks):
                hero_straight = True
                break

    set_plus = trips_plus or hero_flush or hero_straight
    two_pair_plus = two_pair_plus or set_plus
    pair_plus = pair_plus or two_pair_plus

    # Flush draw: 4 of one suit on the flop + hero contributes ≥1 of that suit.
    flush_draw = False
    if not hero_flush:
        for suit, cnt in suit_count.items():
            if cnt >= 4 and suit in h_suits:
                flush_draw = True
                break

    # OESD: 4 consecutive distinct ranks present with both ends openable
    # (i.e. neither boundary is the bottom A-low nor the top A-high) and hero
    # uses ≥1 rank in the window. Pure gutshots don't count.
    oesd = False
    if not hero_straight:
        for i in range(len(extended) - 3):
            w = extended[i:i + 4]
            if w[3] - w[0] != 3:
                continue
            wset = {12 if r == -1 else r for r in w}
            if not any(r in wset for r in h_ranks):
                continue
            # Need a card below and above for an OESD (excludes A-low and A-high terminals).
            if w[0] > 0 and w[3] < 12:
                oesd = True
                break

    return {
        "set_plus": set_plus,
        "two_pair_plus": two_pair_plus,
        "pair_plus": pair_plus,
        "flush_draw": flush_draw,
        "oesd": oesd,
        "strong_draw": flush_draw or oesd,
    }


class FlopCategory:
    category_id = "flop"
    name = "Flop run-good"

    def compute(self, hands: list[dict], hero_user_id: str) -> Optional[LuckCategoryResult]:
        try:
            pre_table = _load_preflop()
        except FileNotFoundError:
            print(f"[LUCK] flop: preflop equity table missing at {_PREFLOP_PATH}; skipping")
            return None
        try:
            flop_table = _load_flop()
        except FileNotFoundError:
            print(f"[LUCK] flop: flop baselines missing at {_FLOP_PATH}; skipping")
            return None

        pre_classes = pre_table["classes"]
        flop_classes = flop_table["classes"]

        dev_sum = 0.0
        var_sum = 0.0
        actual_eq_sum = 0.0
        baseline_eq_sum = 0.0
        n = 0

        actual_counts = {"set_plus": 0, "two_pair_plus": 0, "pair_plus": 0,
                         "flush_draw": 0, "oesd": 0, "strong_draw": 0}
        expected_counts = {k: 0.0 for k in actual_counts}

        per_flop_details: list[dict] = []

        for hand in hands:
            board = hand.get("board") or []
            if len(board) < 3:
                continue
            if not _hero_saw_flop(hand, hero_user_id):
                continue
            hero = _hero_cards(hand, hero_user_id)
            if hero is None:
                continue
            cls = _hand_class(hero[0], hero[1])
            if cls is None or cls not in pre_classes or cls not in flop_classes:
                continue

            flop = board[:3]
            try:
                hero_cards = parse_cards(hero)
                flop_cards = parse_cards(flop)
            except Exception:
                continue

            baseline_eq = pre_classes[cls]["equity"]
            try:
                eq = hero_equity_vs_random(hero_cards, flop_cards, mc_iters=300)
            except Exception:
                continue

            dev = eq - baseline_eq
            dev_sum += dev
            var_sum += flop_classes[cls]["flop_equity_var"]
            actual_eq_sum += eq
            baseline_eq_sum += baseline_eq
            n += 1

            cats = classify_flop(hero, flop)
            for k in actual_counts:
                if cats[k]:
                    actual_counts[k] += 1
                expected_counts[k] += flop_classes[cls].get(f"p_{k}", 0.0)

            per_flop_details.append({
                "hand_id": hand.get("hand_id"),
                "hero_hand": cls,
                "flop": flop,
                "equity": round(eq, 4),
                "baseline_equity": round(baseline_eq, 4),
                "delta": round(dev, 4),
                "set_plus": cats["set_plus"],
                "two_pair_plus": cats["two_pair_plus"],
                "pair_plus": cats["pair_plus"],
                "flush_draw": cats["flush_draw"],
                "oesd": cats["oesd"],
            })

        if n == 0:
            return None

        sd = math.sqrt(var_sum) if var_sum > 0 else 0.0
        z = dev_sum / sd if sd > 0 else 0.0
        avg_actual = actual_eq_sum / n
        avg_baseline = baseline_eq_sum / n
        avg_delta = avg_actual - avg_baseline

        headline = run_quality_headline(
            subject="Flops",
            z=z,
            sample_desc=f"{avg_delta*100:+.0f}% equity vs your starting hands, {n} flops",
            min_n=8,
            n=n,
        )

        return LuckCategoryResult(
            category_id=self.category_id,
            name=self.name,
            sample_size=n,
            headline=headline,
            metrics={
                "z_score": round(z, 2),
                "percentile": round(z_to_percentile(z), 4),
                "flops_seen": n,
                "avg_flop_equity": round(avg_actual, 4),
                "avg_baseline_equity": round(avg_baseline, 4),
                "avg_equity_delta": round(avg_delta, 4),
                "sets_or_better_flopped": actual_counts["set_plus"],
                "sets_or_better_expected": round(expected_counts["set_plus"], 2),
                "two_pair_plus_flopped": actual_counts["two_pair_plus"],
                "two_pair_plus_expected": round(expected_counts["two_pair_plus"], 2),
                "pair_plus_flopped": actual_counts["pair_plus"],
                "pair_plus_expected": round(expected_counts["pair_plus"], 2),
                "flush_draws_flopped": actual_counts["flush_draw"],
                "flush_draws_expected": round(expected_counts["flush_draw"], 2),
                "oesd_flopped": actual_counts["oesd"],
                "oesd_expected": round(expected_counts["oesd"], 2),
            },
            details={
                "flops": per_flop_details,
            },
        )
