"""Relationships between columns (and between tables)."""

from __future__ import annotations

import re
from datetime import date
from itertools import combinations

from ..model import Check, Collector, Example, Issue
from ..semantics import DATE_SENTINELS, parse_date, to_float

DATE_PAIRS = [
    ("start", "end"),
    ("begin", "end"),
    ("from", "to"),
    ("created", "updated"),
    ("created", "modified"),
    ("open", "close"),
    ("opened", "closed"),
    ("check_in", "check_out"),
    ("checkin", "checkout"),
    ("order", "ship"),
    ("ordered", "shipped"),
    ("ship", "deliver"),
    ("shipped", "delivered"),
    ("birth", "death"),
    ("hire", "termination"),
    ("hired", "terminated"),
    ("issue", "expiry"),
    ("issue", "expiration"),
    ("issued", "expires"),
    ("effective", "expiration"),
    ("admit", "discharge"),
    ("admission", "discharge"),
    ("pickup", "dropoff"),
    ("departure", "arrival"),
    ("signup", "cancel"),
]
NEGATIVE_PARTS = re.compile(r"discount|refund|rebate|credit|deduct|adjust|coupon|promo|writeoff|write_off", re.I)
TOTAL_NAME = re.compile(r"(?<!sub)total|grand|amount_due|net_amount|^net$|^amount$|^sum$|balance_due", re.I)
MONEY_CHARS = re.compile(r"[$€£,\s]")


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")")
    x = to_float(MONEY_CHARS.sub("", v.strip("()")))
    return -x if (x is not None and neg) else x


def _date(v: str | None) -> date | None:
    if not v or not v.strip():
        return None
    pd = parse_date(v.strip())
    return pd.value if pd else None


class DateOrder(Check):
    id = "date_order"
    category = "relationships"
    title = "Dates in the wrong order"
    description = (
        "Pairs of date columns that must be in order (start/end, created/updated, order/ship, "
        "birth/death...) where the second date comes first."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        dates = [p for p in ctx.profiles if p.ctype.label == "date"]
        self.pairs: list[tuple[int, int]] = []
        for a in dates:
            for b in dates:
                if a.index == b.index:
                    continue
                an, bn = a.name.lower(), b.name.lower()
                for first, second in DATE_PAIRS:
                    if first in an and second in bn and an.replace(first, "#") == bn.replace(second, "#"):
                        self.pairs.append((a.index, b.index))
                        break
        self.c = {pair: self.collector() for pair in self.pairs}

    def row(self, i, values, field_count):
        for a, b in self.pairs:
            da, db = _date(values[a]), _date(values[b])
            if da and db and db < da:
                h = self.ctx.header
                self.c[(a, b)].add(
                    i,
                    f"{h[a]} → {h[b]}",
                    f"{values[a].strip()} → {values[b].strip()}",
                    f"`{h[b]}` is {(da - db).days:,} day(s) before `{h[a]}`",
                )

    def finish(self):
        h = self.ctx.header
        return [
            self.issue(
                "high",
                f"`{h[b]}` before `{h[a]}`",
                f"{c.count:,} row(s) have `{h[b]}` earlier than `{h[a]}`.",
                [h[a], h[b]],
                c,
            )
            for (a, b), c in self.c.items()
            if c.count
        ]


class TotalMismatch(Check):
    id = "total_mismatch"
    category = "relationships"
    title = "Totals that don't add up"
    description = (
        "Finds a total column and the columns that add up to it (for example total = subtotal + tax - "
        "discount) from a sample of rows, then reports every row where the arithmetic is wrong."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        import numpy as np

        self.rules: list[tuple[int, list[tuple[int, int]]]] = []
        self.c = {}
        numeric = [
            p
            for p in ctx.profiles
            if p.ctype.label in ("integer", "decimal")
            or p.ctype.label_shares.get("currency_amount", 0) + p.ctype.label_shares.get("decimal", 0) >= 0.8
        ]
        if len(numeric) < 3 or not ctx.row_sample:
            return
        sample = ctx.row_sample.items
        matrix = {
            p.index: np.array([_num(r[p.index]) if r[p.index] is not None else np.nan for r in sample], dtype=float)
            for p in numeric
        }
        for t in numeric:
            if not TOTAL_NAME.search(t.name):
                continue
            parts = [
                p
                for p in numeric
                if p.index != t.index
                and not TOTAL_NAME.search(p.name)
                and not re.search(
                    r"(^|_)(id|qty|quantity|count|year|zip|rate|pct|percent|age|lat|latitude|lon|lng|"
                    r"longitude|weight)($|_)",
                    p.name,
                    re.I,
                )
            ][:10]
            tv = matrix[t.index]
            best = None
            for size in (2, 3, 4):
                for combo in combinations(parts, size):
                    signed = [(p.index, -1 if NEGATIVE_PARTS.search(p.name) else 1) for p in combo]
                    total = sum(sign * matrix[j] for j, sign in signed)
                    ok_rows = ~np.isnan(total) & ~np.isnan(tv)
                    n = int(ok_rows.sum())
                    if n < 30:
                        continue
                    diff = np.abs(total[ok_rows] - tv[ok_rows])
                    share = float(np.mean(diff <= np.maximum(0.011, 0.001 * np.abs(tv[ok_rows]))))
                    if share >= 0.9 and (best is None or share > best[0]):
                        best = (share, signed)
                if best:
                    break
            if best:
                self.rules.append((t.index, best[1]))
        self.c = {t: self.collector() for t, _ in self.rules}

    def formula(self, t: int, signed: list[tuple[int, int]]) -> str:
        h = self.ctx.header
        expr = " ".join(f"{'+' if s > 0 else '-'} {h[j]}" for j, s in signed).lstrip("+ ")
        return f"{h[t]} = {expr}"

    def row(self, i, values, field_count):
        for t, signed in self.rules:
            tv = _num(values[t])
            vals = [_num(values[j]) for j, _ in signed]
            if tv is None or any(v is None for v in vals):
                continue
            s = sum(sign * v for (_, sign), v in zip(signed, vals, strict=False))
            if abs(s - tv) > max(0.011, 0.001 * abs(tv)):
                self.c[t].add(i, self.ctx.header[t], values[t].strip(), f"parts add up to {s:,.2f}", f"{s:.2f}")

    def finish(self):
        out = []
        for t, signed in self.rules:
            c = self.c[t]
            if c.count:
                f = self.formula(t, signed)
                out.append(
                    self.issue(
                        "high",
                        f"`{self.ctx.header[t]}` doesn't add up",
                        f"In most rows {f}, but {c.count:,} row(s) break that rule.",
                        [self.ctx.header[t]] + [self.ctx.header[j] for j, _ in signed],
                        c,
                        formula=f,
                    )
                )
        return out


class AgeVsBirthDate(Check):
    id = "age_vs_birth_date"
    category = "relationships"
    title = "Age doesn't match date of birth"
    description = (
        "Rows where the age column disagrees with the date of birth. The dataset's usual offset is "
        "learned first, so data snapshotted some time ago isn't flagged."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        self.age = next(
            (
                p.index
                for p in ctx.profiles
                if re.fullmatch(r"(customer_|member_|patient_)?age(_years)?", p.name, re.I)
                and p.ctype.label in ("integer", "decimal")
            ),
            None,
        )
        self.dob = next(
            (p.index for p in ctx.profiles if re.search(r"birth|dob", p.name, re.I) and p.ctype.label == "date"), None
        )
        self.offset = 0
        self.c = self.collector()
        if self.age is None or self.dob is None or not ctx.row_sample:
            self.age = None
            return
        ref = ctx.options.reference_date
        diffs = []
        for row in ctx.row_sample.items:
            a, d = to_float(row[self.age] or ""), _date(row[self.dob])
            if a is not None and d:
                diffs.append(round(a) - _age(d, ref))
        if len(diffs) >= 20:
            diffs.sort()
            self.offset = diffs[len(diffs) // 2]

    def row(self, i, values, field_count):
        if self.age is None:
            return
        a, d = to_float((values[self.age] or "").strip()), _date(values[self.dob])
        if a is None or not d or d in DATE_SENTINELS or d > self.ctx.options.reference_date or d.year < 1900:
            return  # placeholder or impossible birth dates are reported by other checks
        expected = _age(d, self.ctx.options.reference_date) + self.offset
        if abs(a - expected) > 1:
            self.c.add(
                i,
                self.ctx.header[self.age],
                f"{values[self.age].strip()} (born {values[self.dob].strip()})",
                f"expected about {expected}",
                str(expected),
            )

    def finish(self):
        if not self.c.count:
            return []
        names = [self.ctx.header[self.age], self.ctx.header[self.dob]]
        return [
            self.issue(
                "medium",
                "Age doesn't match date of birth",
                f"{self.c.count:,} row(s) have an age more than a year away from what the date of birth implies.",
                names,
                self.c,
            )
        ]


def _age(born: date, ref: date) -> int:
    return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


def orphan_keys(
    child_name: str,
    child_column: str,
    child_values: list[tuple[int, str]],
    parent_name: str,
    parent_column: str,
    parent_keys: set[str],
    max_examples: int = 10,
    n_rows: int = 1,
) -> Issue | None:
    """Rows whose foreign key has no match in the parent table."""
    c = Collector(max_examples)
    for row, v in child_values:
        if v not in parent_keys:
            c.add(row, child_column, v, f"no `{parent_column}` = {v!r} in {parent_name}")
    if not c.count:
        return None
    return Issue(
        "orphan_keys",
        "relationships",
        "high",
        f"`{child_column}` values missing from {parent_name}",
        f"{c.count:,} row(s) in {child_name} reference a `{parent_column}` that doesn't exist in {parent_name}.",
        [child_column],
        c.count,
        round(c.count / max(1, n_rows), 6),
        c.examples,
        {"parent": parent_name, "parent_column": parent_column},
    )


_ = Example
CHECKS = [DateOrder, TotalMismatch, AgeVsBirthDate]
