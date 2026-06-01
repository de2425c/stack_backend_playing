"""Hand strength: were the hero's dealt cards above or below baseline?

For each hand dealt to the hero (regardless of whether they played it),
look up the hand class's equity vs. a uniformly random villain hand
(combo-weighted, card-removal-adjusted -- i.e. equity against the full
range grid). Sum across the session and z-score against the baseline
(E[e]=0.5, Var[e] from the lookup table).
"""

from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Optional

from ..base import LuckCategoryResult
from ..stats import run_quality_headline, z_to_percentile


_DATA_PATH = Path(__file__).resolve().parents[1] / "data/preflop_equity_vs_random.json"
_TABLE_CACHE: dict | None = None

_RANK_ORDER = "AKQJT98765432"
_RANK_INDEX = {r: i for i, r in enumerate(_RANK_ORDER)}

# Premium = AA, KK, QQ, AK (any). 6+6+6+16 = 34 combos / 1326 ~= 2.564%
_PREMIUM_CLASSES = {"AA", "KK", "QQ", "AKs", "AKo"}
_POCKET_PAIR_CLASSES = {r + r for r in _RANK_ORDER}


def _load_table() -> dict:
    global _TABLE_CACHE
    if _TABLE_CACHE is None:
        _TABLE_CACHE = json.loads(_DATA_PATH.read_text())
    return _TABLE_CACHE


def _hand_class(card1: str, card2: str) -> Optional[str]:
    """e.g. ('Ah','Kh') -> 'AKs'; ('Ah','Kd') -> 'AKo'; ('Ah','Ad') -> 'AA'."""
    if len(card1) != 2 or len(card2) != 2:
        return None
    r1, s1 = card1[0], card1[1]
    r2, s2 = card2[0], card2[1]
    if r1 not in _RANK_INDEX or r2 not in _RANK_INDEX:
        return None
    if r1 == r2:
        return r1 + r2
    # Order: higher rank first
    if _RANK_INDEX[r1] < _RANK_INDEX[r2]:
        hi, lo, hi_s, lo_s = r1, r2, s1, s2
    else:
        hi, lo, hi_s, lo_s = r2, r1, s2, s1
    return f"{hi}{lo}{'s' if hi_s == lo_s else 'o'}"


class HandStrengthCategory:
    category_id = "hand_strength"
    name = "Hand strength"

    def compute(self, hands: list[dict], hero_user_id: str) -> Optional[LuckCategoryResult]:
        try:
            table = _load_table()
        except FileNotFoundError:
            print(f"[LUCK] hand_strength: equity table missing at {_DATA_PATH}; skipping")
            return None

        classes = table["classes"]
        var_per_hand = table["variance"]
        baseline_mean = table["mean"]   # ~0.5 by symmetry

        per_class_counts: dict[str, int] = {}
        equity_sum = 0.0
        n = 0
        best_hands: list[tuple[float, str, str]] = []  # (equity, class, hand_id)

        for hand in hands:
            hero_cards = _hero_cards(hand, hero_user_id)
            if hero_cards is None:
                continue
            cls = _hand_class(hero_cards[0], hero_cards[1])
            if cls is None or cls not in classes:
                continue
            eq = classes[cls]["equity"]
            equity_sum += eq
            n += 1
            per_class_counts[cls] = per_class_counts.get(cls, 0) + 1
            best_hands.append((eq, cls, hand.get("hand_id", "")))

        if n == 0:
            return None

        expected_sum = n * baseline_mean
        var_sum = n * var_per_hand
        sd_sum = math.sqrt(var_sum) if var_sum > 0 else 0.0
        z = (equity_sum - expected_sum) / sd_sum if sd_sum > 0 else 0.0
        avg_equity = equity_sum / n

        premium_dealt = sum(c for cls, c in per_class_counts.items() if cls in _PREMIUM_CLASSES)
        premium_expected = n * sum(classes[c]["combos"] for c in _PREMIUM_CLASSES) / 1326

        pp_dealt = sum(c for cls, c in per_class_counts.items() if cls in _POCKET_PAIR_CLASSES)
        pp_expected = n * sum(classes[c]["combos"] for c in _POCKET_PAIR_CLASSES) / 1326

        best_hands.sort(reverse=True)
        top_dealt = [{"class": cls, "hand_id": hid, "equity": eq} for eq, cls, hid in best_hands[:5]]

        headline = run_quality_headline(
            subject="Hands dealt",
            z=z,
            sample_desc=f"avg equity {avg_equity*100:.0f}% over {n} hands",
            min_n=10,
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
                "avg_equity": round(avg_equity, 4),
                "baseline_equity": baseline_mean,
                "hands_dealt": n,
                "premium_dealt": premium_dealt,
                "premium_expected": round(premium_expected, 2),
                "pocket_pair_dealt": pp_dealt,
                "pocket_pair_expected": round(pp_expected, 2),
            },
            details={
                "top_dealt": top_dealt,
                "class_counts": per_class_counts,
            },
        )


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
