"""Pass one: profile every column in a single streaming pass, then infer column types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .semantics import ColumnType, infer_column_type, is_null_token
from .sketches import HyperLogLog, Reservoir, TopK
from .sources import Source

_MASK64 = (1 << 64) - 1


@dataclass
class ScanOptions:
    sample_size: int = 5000  # values per column kept for type inference and robust statistics
    max_examples: int = 10  # example rows kept per issue
    near_duplicate_limit: int = 20_000  # rows considered for fuzzy duplicate detection
    max_rows: int | None = None  # stop after this many rows (quick look at a huge file)
    type_threshold: float = 0.8  # share of values that must fit a type for the column to get it
    include: list[str] | None = None  # check ids or categories to run (default: all)
    exclude: list[str] | None = None
    key_columns: list[str] | None = None  # override automatic primary-key detection
    reference_date: date = field(default_factory=date.today)
    seed: int = 0


class ColumnProfile:
    __slots__ = (
        "name",
        "index",
        "nulls",
        "whitespace_only",
        "non_null",
        "hll",
        "topk",
        "sample",
        "min_len",
        "max_len",
        "len_counts",
        "ctype",
        "_topk_frozen",
        "_topk_misses",
    )

    def __init__(self, name: str, index: int, sample_size: int, seed: int):
        self.name = name
        self.index = index
        self.nulls = 0
        self.whitespace_only = 0
        self.non_null = 0
        self.hll = HyperLogLog(12)
        self.topk = TopK(400)
        self.sample = Reservoir(sample_size, seed=seed + index)
        self.min_len = 1 << 30
        self.max_len = 0
        self.len_counts: dict[int, int] = {}
        self.ctype: ColumnType = ColumnType("empty", 0.0)
        self._topk_frozen = False
        self._topk_misses = 0

    def add(self, raw: str | None, row: int) -> None:
        if raw is None:
            self.nulls += 1
            return
        v = raw.strip()
        if not v:
            self.whitespace_only += 1
            return
        self.non_null += 1
        h = hash(v) & _MASK64
        if self.hll._exact is not None:
            self.hll._exact.add(h)
            if len(self.hll._exact) > 2048:
                for x in self.hll._exact:
                    self.hll._add_hash(x)
                self.hll._exact = None
        else:
            self.hll._add_hash(h)
        if not self._topk_frozen:
            counts = self.topk.counts
            if v in counts or len(counts) < self.topk.capacity:
                self.topk.add(v)
            else:
                # far more distinct values than we track: an ID-like column, where top values are meaningless
                self._topk_misses += 1
                if self._topk_misses > 2 * self.topk.capacity:
                    self._topk_frozen = True
                else:
                    self.topk.add(v)
        self.sample.add(v, row)
        n = len(v)
        if n < self.min_len:
            self.min_len = n
        if n > self.max_len:
            self.max_len = n
        if n in self.len_counts or len(self.len_counts) < 300:
            self.len_counts[n] = self.len_counts.get(n, 0) + 1

    @property
    def missing(self) -> int:
        return self.nulls + self.whitespace_only

    @property
    def distinct(self) -> int:
        return self.hll.count()

    @property
    def topk_exact(self) -> bool:
        return not self._topk_frozen and self.topk.exact

    @property
    def unique_ratio(self) -> float:
        return min(1.0, self.distinct / self.non_null) if self.non_null else 0.0

    def to_dict(self, n_rows: int) -> dict:
        return {
            "name": self.name,
            "type": self.ctype.label,
            "type_share": round(self.ctype.share, 4),
            "missing": self.missing,
            "missing_rate": round(self.missing / n_rows, 6) if n_rows else 0.0,
            "distinct_estimate": self.distinct,
            "min_length": self.min_len if self.non_null else None,
            "max_length": self.max_len if self.non_null else None,
            "top_values": [[v, c] for v, c in self.topk.most_common(5)] if self.topk_exact else [],
        }


class NullTimeline:
    """Missing-value counts per column over row chunks, for the heatmap (bounded to ~120 chunks)."""

    def __init__(self, n_cols: int, max_chunks: int = 120):
        self.n_cols = n_cols
        self.max_chunks = max_chunks
        self.chunk_size = 500
        self.chunks: list[list[int]] = []
        self.rows: list[int] = []

    def add(self, row_index: int, missing_cols: list[int]) -> None:
        c = row_index // self.chunk_size
        while c >= self.max_chunks:
            self._merge()
            c = row_index // self.chunk_size
        while len(self.chunks) <= c:
            self.chunks.append([0] * self.n_cols)
            self.rows.append(0)
        self.rows[c] += 1
        chunk = self.chunks[c]
        for j in missing_cols:
            chunk[j] += 1

    def _merge(self) -> None:
        merged, rows = [], []
        for k in range(0, len(self.chunks), 2):
            pair = self.chunks[k : k + 2]
            merged.append([sum(x) for x in zip(*pair, strict=False)])
            rows.append(sum(self.rows[k : k + 2]))
        self.chunks, self.rows = merged, rows
        self.chunk_size *= 2

    def rates(self) -> list[list[float]]:
        return [
            [round(c / r, 4) if r else 0.0 for c in chunk] for chunk, r in zip(self.chunks, self.rows, strict=False)
        ]


@dataclass
class ScanContext:
    source_name: str
    header: list[str]
    options: ScanOptions
    profiles: list[ColumnProfile]
    n_rows: int = 0
    field_count_mismatches: int = 0
    timeline: NullTimeline | None = None
    profile_seconds: float = 0.0
    row_sample: Reservoir | None = None  # whole rows, for checks that relate columns to each other
    row_mismatches: int = 0  # out-of-type values seen in the current row (shared between checks)

    def col(self, name: str) -> ColumnProfile:
        return self.profiles[self.header.index(name)]

    def type_of(self, j: int) -> str:
        return self.profiles[j].ctype.label


def profile_pass(source: Source, options: ScanOptions) -> ScanContext:
    t0 = time.perf_counter()
    header = source.header
    n = len(header)
    profiles = [ColumnProfile(name, j, options.sample_size, options.seed) for j, name in enumerate(header)]
    timeline = NullTimeline(n)
    row_sample = Reservoir(2000, seed=options.seed + 7)
    ctx = ScanContext(source.name, header, options, profiles, timeline=timeline, row_sample=row_sample)
    rows = 0
    mismatches = 0
    for i, values, field_count in source.rows():
        if options.max_rows is not None and rows >= options.max_rows:
            break
        rows += 1
        if field_count >= 0 and field_count != n:
            mismatches += 1
        missing = []
        for j in range(n):
            v = values[j]
            profiles[j].add(v, i)
            if v is None or not v.strip():
                missing.append(j)
        timeline.add(rows - 1, missing)
        row_sample.add(values, i)
    ctx.n_rows = rows
    ctx.field_count_mismatches = mismatches
    for p in profiles:
        p.ctype = infer_column_type(
            p.name, [v for v in p.sample.items[:2000] if not is_null_token(v)], options.type_threshold
        )
    ctx.profile_seconds = round(time.perf_counter() - t0, 3)
    return ctx
