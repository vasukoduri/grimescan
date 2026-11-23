"""Issues, examples and the base class every check implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profile import ScanContext

SEVERITIES = ("high", "medium", "low", "info")
SEVERITY_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.2, "info": 0.0}

CATEGORIES = {
    "missing": "Missing and empty data",
    "format": "Format and encoding",
    "out_of_place": "Values that don't belong",
    "duplicates": "Duplicates and conflicts",
    "relationships": "Relationships between columns and tables",
    "plausibility": "Plausibility and possible fabrication",
    "sensitive": "Sensitive data in the wrong place",
}


@dataclass
class Example:
    row: int | None
    column: str | None
    value: str | None
    note: str = ""
    suggestion: str | None = None


@dataclass
class Issue:
    check: str
    category: str
    severity: str
    title: str
    message: str
    columns: list[str] = field(default_factory=list)
    count: int = 0  # affected rows or values
    rate: float = 0.0  # share of rows (or of the column's non-null values)
    examples: list[Example] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False  # examples are masked in reports


class Collector:
    """Counts and samples examples for one issue key, with a fixed memory cap."""

    def __init__(self, max_examples: int = 10):
        self.count = 0
        self.examples: list[Example] = []
        self.max = max_examples

    def add(
        self, row: int | None, column: str | None, value: str | None, note: str = "", suggestion: str | None = None
    ) -> None:
        self.count += 1
        if len(self.examples) < self.max:
            self.examples.append(Example(row, column, value, note, suggestion))


class Check:
    """Base class. ``prepare`` runs after profiling, ``row`` once per row in the second pass,
    ``finish`` returns the issues. Checks that only need the profile can skip ``row``."""

    id: str = "check"
    category: str = "missing"
    title: str = ""
    description: str = ""
    needs_rows: bool = False

    def prepare(self, ctx: ScanContext) -> None:
        self.ctx = ctx

    #: column indices whose values this check wants to see (set in ``prepare``); see :meth:`value`
    columns: list[int] = []  # noqa: RUF012 - overwritten per instance

    def row(self, i: int, values: list[str | None], field_count: int) -> None:  # noqa: B027 - optional hook
        """Called for every row in pass two (only if ``needs_rows``)."""

    def value(self, j: int, raw: str, v: str, i: int) -> None:  # noqa: B027 - optional hook
        """Called for every non-null value of the columns in ``self.columns``: ``raw`` as read,
        ``v`` stripped (may be empty for whitespace-only values), ``i`` the row number."""

    def finish(self) -> list[Issue]:
        return []

    # helpers
    def collector(self) -> Collector:
        return Collector(self.ctx.options.max_examples)

    def issue(
        self,
        severity: str,
        title: str,
        message: str,
        columns: list[str],
        c: Collector | int,
        denominator: int | None = None,
        **details: Any,
    ) -> Issue:
        count = c if isinstance(c, int) else c.count
        examples = [] if isinstance(c, int) else c.examples
        denom = denominator if denominator else max(1, self.ctx.n_rows)
        sensitive = bool(details.pop("sensitive", False))
        return Issue(
            self.id,
            self.category,
            severity,
            title,
            message,
            columns,
            count,
            round(count / denom, 6),
            examples,
            details,
            sensitive,
        )
