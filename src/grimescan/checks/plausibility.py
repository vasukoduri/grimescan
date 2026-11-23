"""Plausibility and signs of fabricated or mis-scaled data."""

from __future__ import annotations

import math
import re

from ..model import Check
from ..semantics import NULL_TOKENS, parse_date, to_float

NON_NEGATIVE = re.compile(
    r"(^|_)(age|qty|quantity|count|units?|price|weight|height|length|width|depth|size|"
    r"duration|distance|population|headcount|employees|pages|rooms|beds|items|visits|"
    r"clicks|views|tenure|years)(_|$)",
    re.I,
)
SIGNED_OK = re.compile(r"change|delta|diff|adjust|balance|net|profit|loss|variance|growth|return|pnl|temp", re.I)
PERCENT = re.compile(r"(^|_)(pct|percent|percentage)(_|$)", re.I)
AMOUNT = re.compile(
    r"amount|price|total|balance|payment|value|cost|revenue|sales|income|expense|salary|spend|fee|"
    r"invoice|charge",
    re.I,
)
BENFORD = [math.log10(1 + 1 / d) for d in range(1, 10)]


def _nums(items: list[str]) -> list[float]:
    out = []
    for v in items:
        if v.lower() in NULL_TOKENS:
            continue
        x = to_float(v)
        if x is not None and math.isfinite(x):
            out.append(x)
    return out


class ImpossibleValues(Check):
    id = "impossible_values"
    category = "plausibility"
    title = "Impossible values"
    description = (
        "Negative ages, quantities and prices; ages above 120; percentages above 100; latitudes beyond "
        "±90 and longitudes beyond ±180; and the (0, 0) 'Null Island' coordinate."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.rules: dict[int, tuple[float | None, float | None, str]] = {}
        for p in ctx.profiles:
            if p.ctype.label not in ("integer", "decimal"):
                continue
            n = p.name.lower()
            if re.fullmatch(r"(.*_)?age(_years)?", n):
                self.rules[p.index] = (0, 120, "an impossible age")
            elif n in ("lat", "latitude") or n.endswith("_lat") or n.endswith("latitude"):
                self.rules[p.index] = (-90, 90, "an impossible latitude")
            elif n in ("lon", "lng", "long", "longitude") or n.endswith(("_lon", "_lng", "longitude")):
                self.rules[p.index] = (-180, 180, "an impossible longitude")
            elif PERCENT.search(n):
                nums = _nums(p.sample.items)
                if nums and sum(1 for x in nums if 0 <= x <= 1) >= 0.95 * len(nums):
                    self.rules[p.index] = (0, 1, "a share outside 0–1")
                else:
                    self.rules[p.index] = (0, 100, "a percentage outside 0–100")
            elif NON_NEGATIVE.search(n) and not SIGNED_OK.search(n):
                self.rules[p.index] = (0, None, "a negative value where only positive values make sense")
        self.columns = list(self.rules)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        x = to_float(v)
        if x is None:
            return
        lo, hi, what = self.rules[j]
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            self.c[j].add(i, self.ctx.header[j], raw, what)

    def finish(self):
        return [
            self.issue(
                "high",
                f"Impossible values in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` are {self.rules[j][2]}.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


class Outliers(Check):
    id = "outliers"
    category = "plausibility"
    title = "Extreme outliers"
    description = (
        "Numbers extremely far from the rest of the column (robust z-score above 10, based on the median "
        "and median absolute deviation, so the outliers themselves don't hide each other)."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.stats: dict[int, tuple[float, float]] = {}
        for p in ctx.profiles:
            if p.ctype.label not in ("integer", "decimal") or p.unique_ratio < 0.02:
                continue
            nums = sorted(_nums(p.sample.items))
            if len(nums) < 50:
                continue
            med = nums[len(nums) // 2]
            mad = sorted(abs(x - med) for x in nums)[len(nums) // 2]
            if mad > 0:
                self.stats[p.index] = (med, mad)
        self.columns = list(self.stats)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        x = to_float(v)
        if x is None:
            return
        med, mad = self.stats[j]
        z = 0.6745 * (x - med) / mad
        if abs(z) > 10:
            self.c[j].add(i, self.ctx.header[j], raw, f"{abs(z):,.0f}× the usual spread from the median ({med:,.4g})")

    def finish(self):
        return [
            self.issue(
                "low",
                f"Extreme outliers in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` are extremely far from the rest; check for "
                "typos, extra digits or test records.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


class RoundNumbers(Check):
    id = "round_number_heaping"
    category = "plausibility"
    title = "Too many round numbers"
    description = (
        "A measured or transactional column where a large share of values are exact multiples of 1,000 or "
        "100: a sign of estimates, defaults or invented data."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.base: dict[int, tuple[int, float]] = {}
        for p in ctx.profiles:
            if p.ctype.label not in ("integer", "decimal") or p.distinct < 50:
                continue
            nums = [x for x in _nums(p.sample.items) if abs(x) >= 100]
            if len(nums) < 100:
                continue
            for base in (1000, 100):
                pool = [x for x in nums if abs(x) >= base * 2]
                if len(pool) < 100:
                    continue
                share = sum(1 for x in pool if x % base == 0) / len(pool)
                expected = 1 / base if any(x % 1 for x in pool) else 10 / base
                if share >= 0.2 and share >= 20 * expected:
                    self.base[p.index] = (base, share)
                    break
        self.columns = list(self.base)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        x = to_float(v)
        base = self.base[j][0]
        if x is not None and abs(x) >= base * 2 and x % base == 0:
            self.c[j].add(i, self.ctx.header[j], raw, f"exact multiple of {base:,}")

    def finish(self):
        out = []
        for j, c in self.c.items():
            base, share = self.base[j]
            out.append(
                self.issue(
                    "low",
                    f"Round-number heaping in `{self.ctx.header[j]}`",
                    f"About {share:.0%} of large values in `{self.ctx.header[j]}` are exact multiples of "
                    f"{base:,}, far more than chance. They may be estimates, defaults or made-up values.",
                    [self.ctx.header[j]],
                    c,
                    denominator=self.ctx.profiles[j].non_null,
                )
            )
        return out


class Benford(Check):
    id = "benford"
    category = "plausibility"
    title = "First digits don't follow Benford's law"
    description = (
        "For amounts spanning several orders of magnitude, leading digits naturally follow Benford's law "
        "(about 30% start with 1). Large deviations are a classic screen for invented or manipulated "
        "figures."
    )

    def finish(self):
        out = []
        for p in self.ctx.profiles:
            if p.ctype.label not in ("integer", "decimal") or not AMOUNT.search(p.name):
                continue
            nums = sorted(abs(x) for x in _nums(p.sample.items) if x != 0)
            if len(nums) < 500:
                continue
            p05, p95 = nums[len(nums) // 20], nums[-len(nums) // 20]
            if p95 / max(p05, 1e-9) < 100:
                continue  # Benford's law only holds for values spanning several orders of magnitude
            counts = [0] * 9
            for x in nums:
                d = int(f"{x:e}"[0])
                if 1 <= d <= 9:
                    counts[d - 1] += 1
            total = sum(counts)
            observed = [c / total for c in counts]
            mad = sum(abs(o - e) for o, e in zip(observed, BENFORD, strict=False)) / 9
            if mad > 0.015:
                dist = ", ".join(
                    f"{d}: {o:.0%} (expected {e:.0%})"
                    for d, (o, e) in enumerate(zip(observed, BENFORD, strict=False), 1)
                    if abs(o - e) > 0.03
                )
                out.append(
                    self.issue(
                        "low",
                        f"`{p.name}` doesn't follow Benford's law",
                        f"The leading digits of `{p.name}` deviate from the natural pattern "
                        f"(mean absolute deviation {mad:.3f}; above 0.015 is non-conforming). "
                        f"Largest gaps: {dist or 'spread across digits'}.",
                        [p.name],
                        0,
                        observed=[round(o, 4) for o in observed],
                        expected=[round(e, 4) for e in BENFORD],
                        mad=round(mad, 4),
                    )
                )
        return out


class TimeStripped(Check):
    id = "time_stripped"
    category = "plausibility"
    title = "Timestamps with the time stripped"
    description = "A timestamp column where nearly every time is exactly 00:00:00, so the time of day was lost."

    def finish(self):
        out = []
        for p in self.ctx.profiles:
            if p.ctype.label != "date":
                continue
            parsed = [parse_date(v) for v in p.sample.items]
            parsed = [x for x in parsed if x and x.value]
            with_time = [x for x in parsed if x.has_time]
            if (
                len(with_time) >= 50
                and len(with_time) >= 0.9 * len(parsed)
                and sum(x.midnight for x in with_time) >= 0.95 * len(with_time)
            ):
                out.append(
                    self.issue(
                        "low",
                        f"Times are all midnight in `{p.name}`",
                        f"`{p.name}` stores timestamps, but almost all are exactly 00:00:00, so the time "
                        "of day was lost somewhere upstream.",
                        [p.name],
                        0,
                    )
                )
        return out


class UnitMixup(Check):
    id = "unit_mixup"
    category = "plausibility"
    title = "Mixed units or scales"
    description = (
        "A numeric column with two separate clusters about 100× or 1,000× apart, such as some amounts in "
        "cents and others in dollars, or grams mixed with kilograms."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        import numpy as np

        self.split: dict[int, tuple[float, float, float, int]] = {}
        for p in ctx.profiles:
            if p.ctype.label not in ("integer", "decimal") or re.search(r"(^|_)(id|zip|year|code)($|_)", p.name, re.I):
                continue
            pos = [x for x in _nums(p.sample.items) if x > 0]
            if len(pos) < 200:
                continue
            logs = np.sort(np.log10(np.array(pos)))
            if logs[-1] - logs[0] < 1.5:
                continue
            c1, c2 = logs[len(logs) // 10], logs[-len(logs) // 10]
            for _ in range(25):
                in_a = np.abs(logs - c1) <= np.abs(logs - c2)
                if in_a.all() or not in_a.any():
                    break
                c1, c2 = logs[in_a].mean(), logs[~in_a].mean()
            in_a = np.abs(logs - c1) <= np.abs(logs - c2)
            if in_a.all() or not in_a.any():
                continue
            a, b = logs[in_a], logs[~in_a]
            gap = c2 - c1
            small = min(len(a), len(b)) / len(logs)
            spread = max(float(a.std()), float(b.std()))
            for target in (2.0, 3.0):
                if abs(gap - target) <= 0.25 and small >= 0.01 and spread <= 0.45:
                    minority = 0 if len(a) < len(b) else 1
                    self.split[p.index] = (float(c1), float(c2), target, minority)
                    break
        self.columns = list(self.split)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        x = to_float(v)
        if x is None or x <= 0:
            return
        c1, c2, target, minority = self.split[j]
        lx = math.log10(x)
        cluster = 0 if abs(lx - c1) <= abs(lx - c2) else 1
        if cluster == minority:
            factor = 10**target
            suggestion = x * factor if minority == 0 else x / factor
            self.c[j].add(
                i,
                self.ctx.header[j],
                raw,
                f"in the {'smaller' if minority == 0 else 'larger'}-scale cluster ({factor:,.0f}× apart)",
                f"{suggestion:g}",
            )

    def finish(self):
        out = []
        for j, c in self.c.items():
            factor = 10 ** self.split[j][2]
            out.append(
                self.issue(
                    "medium",
                    f"Possible unit mix-up in `{self.ctx.header[j]}`",
                    f"`{self.ctx.header[j]}` has two clusters of values about {factor:,.0f}× apart; "
                    f"{c.count:,} value(s) in the smaller cluster may use a different unit (for example "
                    "cents vs dollars).",
                    [self.ctx.header[j]],
                    c,
                    denominator=self.ctx.profiles[j].non_null,
                )
            )
        return out


def _sd(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


class ConstantColumn(Check):
    id = "constant_column"
    category = "plausibility"
    title = "Columns with a single value"
    description = (
        "Columns where every filled value is the same: often a broken extract or a column that can be dropped."
    )

    def finish(self):
        out = []
        for p in self.ctx.profiles:
            if self.ctx.n_rows >= 20 and p.non_null >= 20 and p.topk_exact and len(p.topk.counts) == 1:
                value = next(iter(p.topk.counts))
                out.append(
                    self.issue(
                        "low",
                        f"`{p.name}` has only one value",
                        f"Every filled value of `{p.name}` is {value!r}.",
                        [p.name],
                        0,
                    )
                )
        return out


CHECKS = [ImpossibleValues, Outliers, RoundNumbers, Benford, TimeStripped, UnitMixup, ConstantColumn]
