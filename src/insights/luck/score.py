"""Roll up per-category luck z-scores into a single 1-100 session score.

50 ≈ a typical session. Higher = ran hotter. Treats categories as
independent gaussian-ish samples and combines by the Stouffer formula
(sum(z) / sqrt(n)), which preserves unit variance under the null.
"""

from __future__ import annotations
import math
from typing import Optional

from .stats import z_to_percentile


def compute_session_luck_score(luck_results: list[dict]) -> Optional[dict]:
    """Return {score, percentile, combined_z, contributors} or None if no category had data."""
    contribs: list[dict] = []
    for r in luck_results:
        z = r.get("metrics", {}).get("z_score")
        if z is None:
            continue
        contribs.append({"category_id": r.get("category_id"), "z_score": z})

    if not contribs:
        return None

    combined_z = sum(c["z_score"] for c in contribs) / math.sqrt(len(contribs))
    pct = z_to_percentile(combined_z)
    score = max(1, min(99, round(pct * 100)))
    return {
        "score": score,
        "percentile": round(pct, 4),
        "combined_z": round(combined_z, 2),
        "contributors": contribs,
    }
