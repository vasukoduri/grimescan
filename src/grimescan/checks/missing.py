"""Missing and empty data."""

from __future__ import annotations

import re
from collections import Counter
from itertools import combinations

from ..model import Check, Collector, Issue
from ..semantics import DATE_SENTINELS, NULL_TOKENS, parse_date, to_float

NUMERIC_SENTINELS = {-1.0, -9.0, -99.0, -999.0, -9999.0, 99.0, 999.0, 9999.0, 99999.0, 999999.0, 9999999.0}


class MissingValues(Check):
    id = "high_missing_rate"
    category = "missing"
    title = "Columns with many missing values"
    description = "Columns that are almost empty (90%+ missing) or have a high share (20%+) of missing values."

    def finish(self) -> list[Issue]:
        out = []
        n = self.ctx.n_rows
        if n < 10:
            return out
        for p in self.ctx.profiles:
            rate = p.missing / n
            if rate >= 0.9:
                out.append(
                    self.issue(
                        "high",
                        f"`{p.name}` is almost empty",
                        f"{rate:.1%} of rows have no value in `{p.name}`.",
                        [p.name],
                        p.missing,
                    )
                )
            elif rate >= 0.2:
                out.append(
                    self.issue(
                        "medium",
                        f"`{p.name}` is often missing",
                        f"{rate:.1%} of rows have no value in `{p.name}`.",
                        [p.name],
                        p.missing,
                    )
                )
        return out


class WhitespaceOnly(Check):
    id = "whitespace_only"
    category = "missing"
    title = "Whitespace-only values"
    description = "Values that look filled in but contain only spaces, tabs or other blank characters."

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.whitespace_only]
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if not v:
            self.c[j].add(i, self.ctx.header[j], raw, "only whitespace", "treat as missing")

    def finish(self):
        return [
            self.issue(
                "medium",
                f"Whitespace-only values in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) contain only blank characters, so they look present but are empty.",
                [self.ctx.header[j]],
                c,
            )
            for j, c in self.c.items()
            if c.count
        ]


class DisguisedNulls(Check):
    id = "disguised_nulls"
    category = "missing"
    title = "Disguised missing values"
    description = (
        'Placeholders standing in for "no value": text like N/A, null, unknown, -, numeric sentinels '
        "like -1 or 9999, and dates like 1900-01-01."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.non_null]
        self.c = {j: self.collector() for j in self.columns}
        self.sentinels: dict[int, set[float]] = {}
        for p in ctx.profiles:
            if p.ctype.label not in ("integer", "decimal") or not p.non_null:
                continue
            nums = [x for x in (to_float(v) for v in p.sample.items) if x is not None]
            if len(nums) < 30:
                continue
            found = set()
            for s in NUMERIC_SENTINELS:
                share = sum(1 for x in nums if x == s) / len(nums)
                rest = sorted(x for x in nums if x != s)
                if share < 0.01 or len(rest) < 20:
                    continue
                lo, hi = rest[len(rest) // 100], rest[-(len(rest) // 100) - 1]
                if s < lo or s > hi:  # a frequent value outside the rest of the distribution
                    found.add(s)
            if found:
                self.sentinels[p.index] = found
        self.dates = {p.index for p in ctx.profiles if p.ctype.label == "date"}

    def value(self, j, raw, v, i):
        if not v:
            return
        low = v.lower()
        if low in NULL_TOKENS:
            self.c[j].add(i, self.ctx.header[j], raw, f'"{v}" stands in for a missing value', "replace with null")
        elif j in self.sentinels:
            x = to_float(v)
            if x is not None and x in self.sentinels[j]:
                self.c[j].add(i, self.ctx.header[j], raw, f"{v} looks like a 'no value' code", "replace with null")
        elif j in self.dates:
            pd = parse_date(v)
            if pd and pd.value in DATE_SENTINELS:
                self.c[j].add(
                    i, self.ctx.header[j], raw, f"{pd.value} is a common placeholder date", "replace with null"
                )

    def finish(self):
        out = []
        for j, c in self.c.items():
            if not c.count:
                continue
            p = self.ctx.profiles[j]
            share = c.count / max(1, p.non_null)
            sev = "high" if share >= 0.1 else "medium"
            out.append(
                self.issue(
                    sev,
                    f"Disguised missing values in `{p.name}`",
                    f"{c.count:,} value(s) ({share:.1%} of filled values) are placeholders for "
                    "'no value', so they inflate counts and break averages.",
                    [p.name],
                    c,
                )
            )
        return out


class MostlyEmptyRows(Check):
    id = "mostly_empty_rows"
    category = "missing"
    title = "Mostly empty rows"
    description = "Rows where at least half of the fields are missing, in a dataset that is otherwise well filled."
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        n = len(ctx.header)
        avg = sum(p.missing for p in ctx.profiles) / max(1, ctx.n_rows * n)
        self.active = n >= 4 and avg < 0.3
        self.threshold = (n + 1) // 2
        self.c = self.collector()

    def row(self, i, values, field_count):
        if not self.active:
            return
        missing = sum(1 for v in values if v is None or not v.strip())
        if missing >= self.threshold:
            self.c.add(i, None, None, f"{missing} of {len(values)} fields empty", "check the source record")

    def finish(self):
        if not self.c.count:
            return []
        return [
            self.issue(
                "medium",
                "Mostly empty rows",
                f"{self.c.count:,} row(s) have at least half their fields empty.",
                [],
                self.c,
                _fetch=True,
            )
        ]


class MissingTogether(Check):
    id = "missing_together"
    category = "missing"
    title = "Columns that are missing together"
    description = (
        "Groups of columns whose values go missing in the same rows, which usually points to one "
        "broken source, join or form section."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        n = max(1, ctx.n_rows)
        cand = [p for p in ctx.profiles if 0.01 <= p.missing / n <= 0.9 and p.missing >= 20]
        self.cols = [p.index for p in sorted(cand, key=lambda p: -p.missing)[:40]]
        self.pairs: dict[tuple[int, int], int] = {}
        self.first_rows: dict[tuple[int, int], list[int]] = {}

    def row(self, i, values, field_count):
        miss = [j for j in self.cols if values[j] is None or not values[j].strip()]
        if len(miss) < 2:
            return
        for a, b in combinations(miss, 2):
            key = (a, b) if a < b else (b, a)
            self.pairs[key] = self.pairs.get(key, 0) + 1
            rows = self.first_rows.setdefault(key, [])
            if len(rows) < 5:
                rows.append(i)

    def finish(self):
        prof = self.ctx.profiles
        strong = [(a, b) for (a, b), co in self.pairs.items() if co / (prof[a].missing + prof[b].missing - co) >= 0.9]
        if not strong:
            return []
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                x = parent[x]
            return x

        for a, b in strong:
            parent[find(a)] = find(b)
        groups: dict[int, list[int]] = {}
        for a, b in strong:
            for x in (a, b):
                groups.setdefault(find(x), [])
                if x not in groups[find(x)]:
                    groups[find(x)].append(x)
        out = []
        for members in groups.values():
            names = [self.ctx.header[j] for j in sorted(members)]
            count = max(self.pairs.get((min(a, b), max(a, b)), 0) for a, b in combinations(members, 2))
            c = Collector(self.ctx.options.max_examples)
            c.count = count
            key = tuple(sorted(members)[:2])
            for r in self.first_rows.get(key, []):
                c.examples.append(_ex(r, ", ".join(names)))
            out.append(
                self.issue(
                    "low",
                    f"{len(names)} columns go missing together",
                    f"{', '.join(f'`{n}`' for n in names)} are empty in the same {count:,} rows. "
                    "This usually means one upstream source, join or form section failed.",
                    names,
                    c,
                )
            )
        return out


class MissingByGroup(Check):
    id = "missing_by_group"
    category = "missing"
    title = "Missing values concentrated in one group"
    description = (
        "A column that is missing far more often for one category (one region, one source system, one "
        "upload date) than overall: a sign of a pipeline problem, not random gaps."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        n = max(1, ctx.n_rows)
        cands = [
            p
            for p in ctx.profiles
            if p.topk_exact
            and 2 <= len(p.topk.counts) <= 30
            and p.missing / n < 0.1
            and p.ctype.label not in ("decimal", "integer", "date", "person_name")
            and not re.search(r"name|first|last|email|phone|note|comment|address", p.name, re.I)
        ]
        self.groups = [p.index for p in sorted(cands, key=lambda p: len(p.topk.counts))[:5]]
        self.targets = [p.index for p in ctx.profiles if 0.005 <= p.missing / n <= 0.9 and p.index not in self.groups]
        self.totals: dict[tuple[int, str], int] = {}
        self.miss: dict[tuple[int, str, int], int] = {}
        self.examples: dict[tuple[int, str, int], list[int]] = {}

    def row(self, i, values, field_count):
        for g in self.groups:
            gv = values[g]
            if gv is None:
                continue
            gv = gv.strip()
            self.totals[(g, gv)] = self.totals.get((g, gv), 0) + 1
            for t in self.targets:
                tv = values[t]
                if tv is None or not tv.strip():
                    k = (g, gv, t)
                    self.miss[k] = self.miss.get(k, 0) + 1
                    ex = self.examples.setdefault(k, [])
                    if len(ex) < 5:
                        ex.append(i)

    def finish(self):
        out = []
        n = max(1, self.ctx.n_rows)
        for (g, gv, t), m in self.miss.items():
            total = self.totals.get((g, gv), 0)
            overall = self.ctx.profiles[t].missing / n
            rate = m / total if total else 0
            others = (self.ctx.profiles[t].missing - m) / max(1, n - total)
            if total >= 30 and rate >= 0.5 and rate >= 5 * max(others, 0.01) and m >= 20:
                c = Collector(self.ctx.options.max_examples)
                c.count = m
                c.examples = [_ex(r, f"{self.ctx.header[g]} = {gv}") for r in self.examples[(g, gv, t)]]
                tn, gn = self.ctx.header[t], self.ctx.header[g]
                out.append(
                    self.issue(
                        "medium",
                        f"`{tn}` is missing mostly when `{gn}` = {gv!r}",
                        f"`{tn}` is empty in {rate:.0%} of rows where `{gn}` = {gv!r}, versus "
                        f"{others:.0%} elsewhere ({overall:.1%} overall).",
                        [tn, gn],
                        c,
                        denominator=total,
                    )
                )
        return sorted(out, key=lambda x: -x.count)[:20]


class DominantValue(Check):
    id = "default_value_inflation"
    category = "missing"
    title = "Default values filling a column"
    description = (
        "One value repeated in a large share of a column that otherwise has many different values, such as "
        "the load date or 0: usually a default written where the real value was missing."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.targets: dict[int, str] = {}
        for p in ctx.profiles:
            if (
                p.ctype.label not in ("date", "integer", "decimal", "email", "phone", "person_name", "url")
                or p.non_null < 50
                or p.distinct < 20
            ):
                continue
            items = p.sample.items
            if not items:
                continue
            top_value, top_n = Counter(items).most_common(1)[0]
            if top_value.lower() in NULL_TOKENS:
                continue
            share = top_n / len(items)
            if share >= 0.3:
                self.targets[p.index] = top_value
        self.columns = list(self.targets)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if v == self.targets[j]:
            self.c[j].add(i, self.ctx.header[j], raw, "repeated default value")

    def finish(self):
        out = []
        for j, c in self.c.items():
            p = self.ctx.profiles[j]
            share = c.count / max(1, p.non_null)
            out.append(
                self.issue(
                    "medium",
                    f"`{p.name}` is dominated by {self.targets[j]!r}",
                    f"{share:.0%} of `{p.name}` is {self.targets[j]!r}, although the column has about "
                    f"{p.distinct:,} different values. It may be a default written in place of the real "
                    "value.",
                    [p.name],
                    c,
                    denominator=p.non_null,
                )
            )
        return out


def _ex(row, note):
    from ..model import Example

    return Example(row, None, None, note)


CHECKS = [
    MissingValues,
    WhitespaceOnly,
    DisguisedNulls,
    MostlyEmptyRows,
    MissingTogether,
    MissingByGroup,
    DominantValue,
]
