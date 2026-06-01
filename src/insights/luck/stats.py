"""Shared stats helpers for luck categories."""

from __future__ import annotations
import math


def z_to_percentile(z: float) -> float:
    """Two-tailed normal CDF: P(Z <= z) using erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def run_quality_headline(subject: str, z: float, sample_desc: str, min_n: int, n: int) -> str:
    """User-facing one-liner from a z-score. No 'sigma'/'variance' jargon.

    Below `min_n` samples we don't trust the percentile — show a softer line.
    Hot/cold above 75th/below 25th percentile; otherwise 'around average'.
    """
    if n < min_n:
        return f"{subject}: only {n} — not enough to call"
    pct = z_to_percentile(z) * 100
    if pct >= 75:
        return f"{subject} ran hot — top {max(1, round(100 - pct))}% ({sample_desc})"
    if pct <= 25:
        return f"{subject} ran cold — bottom {max(1, round(pct))}% ({sample_desc})"
    return f"{subject} around average ({sample_desc})"
