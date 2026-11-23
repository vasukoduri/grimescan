"""Duplicates and conflicting records.

Row fingerprints are kept as packed 64-bit integers (about 24 bytes per row in total), and
grouped with numpy at the end, so exact duplicate detection scales to tens of millions of
rows. Near-duplicate detection uses MinHash with locality-sensitive hashing on the first
``near_duplicate_limit`` rows, which avoids comparing every pair.
"""

from __future__ import annotations

import re
from array import array

import numpy as np

from ..model import Check, Collector, Example
from ..sketches import MinHasher

_MASK = (1 << 64) - 1
KEY_NAME = re.compile(
    r"(^id$|_id$|^id_|\bid\b|key$|uuid|guid|^(customer|order|account|transaction|txn|record|"
    r"invoice|user|member|employee|product|sku)_?(no|num|number|code)?$|number$|_no$)",
    re.I,
)
VOLATILE = re.compile(
    r"(^|_)(id|uuid|guid|key|created|updated|modified|timestamp|ts|loaded|ingested|etl|row|"
    r"line|seq|index)(_|$|at$)",
    re.I,
)


def _norm(v: str | None) -> str:
    return " ".join(v.lower().split()) if v else ""


class Duplicates(Check):
    id = "duplicates"
    category = "duplicates"
    title = "Duplicate rows, keys and conflicting records"
    description = (
        "Exact duplicate rows, rows that are duplicates once case and spacing are ignored, whole batches "
        "loaded twice, duplicate primary keys, and records with the same key but different values."
    )
    needs_rows = True
    sees_structural_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        self.rows = array("Q")
        self.exact = array("Q")
        self.norm = array("Q")
        self.key = self._find_key(ctx)
        self.keys = array("Q")

    def _find_key(self, ctx) -> int | None:
        if ctx.options.key_columns:
            names = [k for k in ctx.options.key_columns if k in ctx.header]
            return ctx.header.index(names[0]) if names else None
        n = max(1, ctx.n_rows)
        for p in ctx.profiles:
            if KEY_NAME.search(p.name) and p.missing / n < 0.05 and p.unique_ratio >= 0.85 and p.non_null >= 10:
                # 0.85, not 1.0: duplicated batches lower the ratio of a real key
                return p.index
        return None

    def row(self, i, values, field_count):
        self.rows.append(i)
        self.exact.append(hash(tuple(values)) & _MASK)
        self.norm.append(hash(tuple(_norm(v) for v in values)) & _MASK)
        if self.key is not None:
            k = values[self.key]
            self.keys.append((hash(k.strip()) & _MASK) | 1 if k and k.strip() else 0)

    def finish(self):
        out = []
        n = len(self.rows)
        if n < 2:
            return out
        rows = np.frombuffer(self.rows, dtype=np.uint64)
        exact = np.frombuffer(self.exact, dtype=np.uint64)
        norm = np.frombuffer(self.norm, dtype=np.uint64)
        maxex = self.ctx.options.max_examples

        first_pos = _first_positions(exact)
        dup = first_pos != np.arange(n)
        n_dup = int(dup.sum())
        if n_dup:
            groups = len(np.unique(exact[dup]))
            c = Collector(maxex)
            c.count = n_dup
            for p in np.flatnonzero(dup)[:maxex]:
                c.examples.append(
                    Example(
                        int(rows[p]), None, None, f"identical to row {int(rows[first_pos[p]])}", "delete the duplicate"
                    )
                )
            out.append(
                self.issue(
                    "high",
                    "Exact duplicate rows",
                    f"{n_dup:,} row(s) are exact copies of an earlier row ({groups:,} distinct rows repeated).",
                    [],
                    c,
                    _fetch=True,
                )
            )
            out += self._blocks(rows, dup, first_pos)

        nfirst = _first_positions(norm)
        ndup = (nfirst != np.arange(n)) & ~dup
        n_ndup = int(ndup.sum())
        if n_ndup:
            c = Collector(maxex)
            c.count = n_ndup
            for p in np.flatnonzero(ndup)[:maxex]:
                c.examples.append(
                    Example(
                        int(rows[p]),
                        None,
                        None,
                        f"same as row {int(rows[nfirst[p]])} except for case or spacing",
                        "normalize and de-duplicate",
                    )
                )
            out.append(
                self.issue(
                    "medium",
                    "Duplicate rows hidden by case or spacing",
                    f"{n_ndup:,} row(s) duplicate an earlier row once upper/lower case and extra spaces are ignored.",
                    [],
                    c,
                    _fetch=True,
                )
            )

        if self.key is not None and len(self.keys):
            out += self._keys(rows, exact, dup)
        return out

    def _blocks(self, rows, dup, first_pos):
        positions = np.flatnonzero(dup)
        if len(positions) < 20:
            return []
        blocks = []
        start = prev = positions[0]
        for p in positions[1:]:
            if p == prev + 1 and first_pos[p] == first_pos[prev] + 1:
                prev = p
                continue
            if prev - start + 1 >= 20:
                blocks.append((start, prev))
            start = prev = p
        if prev - start + 1 >= 20:
            blocks.append((start, prev))
        if not blocks:
            return []
        c = Collector(self.ctx.options.max_examples)
        c.count = int(sum(b - a + 1 for a, b in blocks))
        for a, b in blocks[: self.ctx.options.max_examples]:
            c.examples.append(
                Example(
                    int(rows[a]),
                    None,
                    None,
                    f"rows {int(rows[a]):,}–{int(rows[b]):,} repeat rows "
                    f"{int(rows[first_pos[a]]):,}–{int(rows[first_pos[b]]):,}",
                    "remove the second load",
                )
            )
        return [
            self.issue(
                "high",
                "A batch of rows was loaded twice",
                f"{len(blocks)} block(s) of consecutive rows ({c.count:,} rows) repeat an earlier block in "
                "the same order, which usually means a file or batch was ingested twice.",
                [],
                c,
            )
        ]

    def _keys(self, rows, exact, exact_dup):
        keys = np.frombuffer(self.keys, dtype=np.uint64)
        name = self.ctx.header[self.key]
        present = keys != 0
        idx = np.flatnonzero(present)
        if len(idx) < 2:
            return []
        k = keys[idx]
        first = _first_positions(k)
        # rows that are exact copies are already reported as duplicate rows
        dup_local = (first != np.arange(len(k))) & ~exact_dup[idx]
        n_dup = int(dup_local.sum())
        if not n_dup:
            return []
        out = []
        maxex = self.ctx.options.max_examples
        order = np.lexsort((exact[idx], k))
        ks, es = k[order], exact[idx][order]
        boundaries = np.flatnonzero(np.diff(ks) != 0) + 1
        conflict_rows: list[tuple[int, int]] = []
        conflict_groups = 0
        conflict_count = 0
        for g in np.split(np.arange(len(ks)), boundaries):
            if len(g) < 2:
                continue
            if len(np.unique(es[g])) > 1:
                conflict_groups += 1
                conflict_count += len(g)
                if len(conflict_rows) < maxex:
                    members = sorted(int(rows[idx[order[x]]]) for x in g)
                    conflict_rows.append((members[0], members[1]))
        c = Collector(maxex)
        c.count = n_dup
        for p in np.flatnonzero(dup_local)[:maxex]:
            c.examples.append(
                Example(int(rows[idx[p]]), name, None, f"same `{name}` as row {int(rows[idx[first[p]]])}")
            )
        out.append(
            self.issue(
                "high",
                f"Duplicate keys in `{name}`",
                f"`{name}` looks like a unique identifier, but {n_dup:,} row(s) reuse an earlier value.",
                [name],
                c,
                _fetch=True,
            )
        )
        if conflict_groups:
            cc = Collector(maxex)
            cc.count = conflict_count
            for a, b in conflict_rows:
                cc.examples.append(
                    Example(
                        b,
                        name,
                        None,
                        f"same `{name}` as row {a} but different values",
                        "decide which record is correct",
                    )
                )
            out.append(
                self.issue(
                    "high",
                    f"Conflicting records for the same `{name}`",
                    f"{conflict_groups:,} `{name}` value(s) appear in rows whose other fields disagree "
                    f"({conflict_count:,} rows), so it's unclear which record is right.",
                    [name],
                    cc,
                    _fetch=True,
                )
            )
        return out


def _first_positions(h: np.ndarray) -> np.ndarray:
    """For each position, the position of the first occurrence of the same value."""
    _, first_idx, inverse = np.unique(h, return_index=True, return_inverse=True)
    return first_idx[inverse]


class NearDuplicates(Check):
    id = "near_duplicates"
    category = "duplicates"
    title = "Near-duplicate records"
    description = (
        'Records that are almost the same ("Jon Smith, 12 Oak St" and "John Smith, 12 Oak Street"), '
        "including the same entity stored under different IDs. Uses MinHash, so it doesn't compare every "
        "pair."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        self.limit = ctx.options.near_duplicate_limit
        self.hasher = MinHasher(64, 16, seed=ctx.options.seed + 3)
        n = max(1, ctx.n_rows)
        self.cols = [
            p.index
            for p in ctx.profiles
            if not VOLATILE.search(p.name)
            and p.missing / n < 0.9
            and not (p.unique_ratio > 0.98 and p.ctype.label in ("integer", "uuid", "decimal"))
            and p.ctype.label not in ("date",)
        ]
        self.key = next((p.index for p in ctx.profiles if KEY_NAME.search(p.name) and p.unique_ratio >= 0.95), None)
        self.active = len(self.cols) >= 2 and ctx.n_rows >= 2
        self.sigs: list[np.ndarray] = []
        self.rows: list[int] = []
        self.norm: list[int] = []
        self.keys: list[str | None] = []
        self.fields: list[list[int]] = []
        self.buckets: dict[tuple[int, bytes], list[int]] = {}

    def row(self, i, values, field_count):
        if not self.active or len(self.rows) >= self.limit:
            return
        parts = [values[j].strip() for j in self.cols if values[j] and values[j].strip()]
        if len(parts) < 2:
            return
        text = " | ".join(parts)
        pos = len(self.rows)
        sig = self.hasher.signature(self.hasher.shingles(text))
        self.sigs.append(sig)
        self.rows.append(i)
        self.norm.append(hash(_norm(text)) >> 1)
        self.keys.append(values[self.key].strip() if self.key is not None and values[self.key] else None)
        self.fields.append([hash(_norm(values[j] or "")) >> 1 for j in self.cols])
        for bk in self.hasher.band_keys(sig):
            bucket = self.buckets.setdefault(bk, [])
            if len(bucket) <= 25:
                bucket.append(pos)  # a bucket that overflows only reflects shared structure, not duplicates

    def finish(self):
        if not self.active or len(self.rows) < 2:
            return []
        pairs = set()
        for bucket in self.buckets.values():
            if 1 < len(bucket) <= 25:
                for a_i, a in enumerate(bucket):
                    for b in bucket[a_i + 1 :]:
                        pairs.add((a, b))
        if not pairs:
            return []
        pa = np.fromiter((a for a, _ in pairs), dtype=np.int64, count=len(pairs))
        pb = np.fromiter((b for _, b in pairs), dtype=np.int64, count=len(pairs))
        sigs = np.vstack(self.sigs)
        sims = (sigs[pa] == sigs[pb]).mean(axis=1)
        norm = np.array(self.norm, dtype=np.int64)
        fields = np.array(self.fields, dtype=np.int64)
        differing = (fields[pa] != fields[pb]).sum(axis=1)
        # similar text is not enough: the same entity differs in only a field or two
        max_diff = max(1, len(self.cols) // 4)
        keep = (sims >= 0.8) & (norm[pa] != norm[pb]) & (differing <= max_diff)
        parent = list(range(len(self.rows)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        matched = []
        for a, b, sim in zip(pa[keep].tolist(), pb[keep].tolist(), sims[keep].tolist(), strict=False):
            matched.append((a, b, sim))  # exact or case/spacing duplicates were excluded above
            parent[find(a)] = find(b)
        if not matched:
            return []
        clusters: dict[int, list[int]] = {}
        for a, b, _ in matched:
            for x in (a, b):
                members = clusters.setdefault(find(x), [])
                if x not in members:
                    members.append(x)
        c = Collector(self.ctx.options.max_examples)
        c.count = sum(len(m) - 1 for m in clusters.values())
        diff_ids = 0
        for a, b, sim in sorted(matched, key=lambda t: -t[2])[: self.ctx.options.max_examples]:
            note = f"{sim:.0%} similar to row {self.rows[a]}"
            if self.keys[a] and self.keys[b] and self.keys[a] != self.keys[b]:
                note += " with a different ID"
            c.examples.append(Example(self.rows[b], None, None, note, "review and merge"))
        for a, b, _ in matched:
            if self.keys[a] and self.keys[b] and self.keys[a] != self.keys[b]:
                diff_ids += 1
        scope = f" (checked the first {self.limit:,} rows)" if self.ctx.n_rows > self.limit else ""
        extra = f" {diff_ids:,} pair(s) carry different IDs: possibly the same entity stored twice." if diff_ids else ""
        return [
            self.issue(
                "medium",
                "Near-duplicate records",
                f"{len(clusters):,} group(s) of rows are almost identical apart from small differences{scope}.{extra}",
                [],
                c,
                _fetch=True,
                clusters=len(clusters),
                different_ids=diff_ids,
            )
        ]


CHECKS = [Duplicates, NearDuplicates]
