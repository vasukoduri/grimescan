"""Check registry."""

from __future__ import annotations

from ..model import Check
from . import (
    duplicates,
    format,
    missing,
    out_of_place,
    plausibility,
    relationships,
    sensitive,
)

ALL_CHECKS: list[type[Check]] = [
    *missing.CHECKS,
    *format.CHECKS,
    *out_of_place.CHECKS,
    *duplicates.CHECKS,
    *relationships.CHECKS,
    *plausibility.CHECKS,
    *sensitive.CHECKS,
]
CHECK_IDS = [c.id for c in ALL_CHECKS]


def select_checks(include: list[str] | None = None, exclude: list[str] | None = None) -> list[Check]:
    """Instantiate checks, filtered by check id or category name."""
    chosen = []
    for cls in ALL_CHECKS:
        tags = {cls.id, cls.category}
        if include and not tags & set(include):
            continue
        if exclude and tags & set(exclude):
            continue
        chosen.append(cls())
    return chosen


__all__ = ["ALL_CHECKS", "CHECK_IDS", "select_checks"]
