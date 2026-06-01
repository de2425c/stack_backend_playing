"""Run all registered luck categories. No cross-category aggregation."""

from __future__ import annotations

from .base import LuckCategoryResult
from .categories import all_categories


def compute_luck_categories(hands: list[dict], hero_user_id: str) -> list[LuckCategoryResult]:
    """Returns one LuckCategoryResult per category that had data."""
    results: list[LuckCategoryResult] = []
    for category in all_categories():
        try:
            res = category.compute(hands, hero_user_id)
        except Exception as e:
            print(f"[LUCK] category {category.category_id} raised {e!r}; skipping")
            continue
        if res is not None:
            results.append(res)
    return results
