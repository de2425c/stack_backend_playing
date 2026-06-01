"""Base types for the luck factor categories."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol, runtime_checkable


@dataclass
class LuckCategoryResult:
    category_id: str
    name: str
    sample_size: int
    headline: str                                # one-line UI summary
    metrics: dict = field(default_factory=dict)  # native session-level metrics
    details: dict = field(default_factory=dict)  # per-hand / per-spot breakdown

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class LuckCategory(Protocol):
    category_id: str
    name: str

    def compute(self, hands: list[dict], hero_user_id: str) -> Optional[LuckCategoryResult]:
        ...
