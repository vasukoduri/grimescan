"""Format and encoding problems."""

from __future__ import annotations

import re

from ..model import Check
from ..semantics import NULL_TOKENS, RE, digits_only, parse_date

MOJIBAKE = re.compile(r"Ã[\x80-\xbf¡-ÿ]|Â[\x80-\xbf ]|â€|ï»¿|�|Ã©|Ã¨|Ã¼|Ã¶|Ã¤")
HIDDEN = re.compile(r"[​‌‍⁠﻿   \x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LINE_BREAK = re.compile(r"[\r\n]")
_HIDDEN_NAMES = {
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "﻿": "byte-order mark",
    " ": "non-breaking space",
    " ": "narrow non-breaking space",
    " ": "figure space",
}
FIELD_LIMITS = {
    10,
    16,
    20,
    25,
    30,
    32,
    40,
    50,
    60,
    64,
    80,
    100,
    128,
    150,
    200,
    250,
    255,
    256,
    500,
    512,
    1000,
    1024,
    2000,
    4000,
    4096,
    8000,
}
BIRTH = re.compile(r"birth|dob|born", re.I)


def _base(fmt: str) -> str:
    return fmt.split(" (")[0]


class DateProblems(Check):
    id = "date_problems"
    category = "format"
    title = "Date problems"
    description = (
        "Mixed date formats in one column, conflicting day/month order, impossible dates such as "
        "February 30, birth dates in the future and dates before 1900."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.ctype.label == "date"]
        self.formats = {j: {} for j in self.columns}
        self.fmt_examples: dict[tuple[int, str], list[tuple[int, str]]] = {}
        self.impossible = {j: self.collector() for j in self.columns}
        self.day_first = {j: self.collector() for j in self.columns}
        self.month_first = {j: self.collector() for j in self.columns}
        self.ambiguous = {j: 0 for j in self.columns}
        self.future = {j: self.collector() for j in self.columns}
        self.old = {j: self.collector() for j in self.columns}
        self.ref = ctx.options.reference_date

    def value(self, j, raw, v, i):
        if not v or v.lower() in NULL_TOKENS:
            return
        pd = parse_date(v)
        if pd is None:
            return
        name = self.ctx.header[j]
        base = _base(pd.fmt)
        f = self.formats[j]
        f[base] = f.get(base, 0) + 1
        ex = self.fmt_examples.setdefault((j, base), [])
        if len(ex) < self.ctx.options.max_examples:
            ex.append((i, raw))
        if pd.value is None:
            self.impossible[j].add(i, name, raw, "this date does not exist", "check the source value")
            return
        if "(day first)" in pd.fmt:
            self.day_first[j].add(i, name, raw, "only valid as day/month")
        elif "(month first)" in pd.fmt:
            self.month_first[j].add(i, name, raw, "only valid as month/day")
        elif pd.ambiguous:
            self.ambiguous[j] += 1
        if BIRTH.search(name) and pd.value > self.ref:
            self.future[j].add(i, name, raw, "birth date in the future")
        if pd.value.year < 1900 and pd.value.year > 1:
            self.old[j].add(i, name, raw, "before 1900")

    def finish(self):
        out = []
        for j in self.columns:
            name = self.ctx.header[j]
            p = self.ctx.profiles[j]
            fmts = self.formats[j]
            total = sum(fmts.values())
            if len(fmts) >= 2 and total:
                main = max(fmts, key=fmts.get)
                minority = sum(c for f, c in fmts.items() if f != main)
                c = self.collector()
                c.count = minority
                for f in fmts:
                    if f != main:
                        for r, val in self.fmt_examples.get((j, f), [])[: self.ctx.options.max_examples]:
                            c.examples.append(_example(r, name, val, f"format {f}", f"convert to {main}"))
                breakdown = ", ".join(f"{f}: {n:,}" for f, n in sorted(fmts.items(), key=lambda kv: -kv[1]))
                out.append(
                    self.issue(
                        "medium",
                        f"Mixed date formats in `{name}`",
                        f"`{name}` uses {len(fmts)} date formats ({breakdown}).",
                        [name],
                        c,
                        denominator=p.non_null,
                        formats=fmts,
                    )
                )
            df, mf = self.day_first[j], self.month_first[j]
            if df.count and mf.count:
                c = self.collector()
                c.count = min(df.count, mf.count)
                c.examples = df.examples[:5] + mf.examples[:5]
                out.append(
                    self.issue(
                        "high",
                        f"Day and month order conflicts in `{name}`",
                        f"Some values in `{name}` only make sense as day/month ({df.count:,}) and others "
                        f"only as month/day ({mf.count:,}), so the {self.ambiguous[j]:,} ambiguous "
                        "values (like 03/04) can't be read safely.",
                        [name],
                        c,
                        denominator=p.non_null,
                    )
                )
            for coll, sev, title, msg in (
                (self.impossible[j], "high", "Impossible dates", "dates that don't exist on the calendar"),
                (self.future[j], "high", "Birth dates in the future", "birth dates after the reference date"),
                (self.old[j], "low", "Dates before 1900", "dates before 1900, often a typo or a placeholder"),
            ):
                if coll.count:
                    out.append(
                        self.issue(
                            sev,
                            f"{title} in `{name}`",
                            f"{coll.count:,} value(s) are {msg}.",
                            [name],
                            coll,
                            denominator=p.non_null,
                        )
                    )
        return out


class ExcelDamage(Check):
    id = "spreadsheet_damage"
    category = "format"
    title = "Spreadsheet damage"
    description = (
        "Numbers converted to scientific notation (digits lost, like 1.23457E+15) and codes that lost "
        "their leading zeros (ZIP 07030 saved as 7030): typical of opening and re-saving files in Excel."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.non_null]
        self.sci = {j: self.collector() for j in self.columns}
        self.zero_len: dict[int, int] = {}
        self.zeros = {}
        for p in ctx.profiles:
            digits = [v for v in p.sample.items if v.isdigit()]
            if len(digits) < 20 or len(digits) < 0.8 * len(p.sample.items):
                continue
            lens: dict[int, int] = {}
            for v in digits:
                lens[len(v)] = lens.get(len(v), 0) + 1
            main = max(lens, key=lens.get)
            leading = sum(1 for v in digits if v.startswith("0")) / len(digits)
            hint = re.search(r"zip|postal|code|id\b|_id|number|no\b|acct|account|sku|ssn", p.name, re.I)
            if main >= 3 and lens[main] >= 0.8 * len(digits) and (leading >= 0.05 or hint):
                self.zero_len[p.index] = main
                self.zeros[p.index] = self.collector()

    def value(self, j, raw, v, i):
        if not v:
            return
        name = self.ctx.header[j]
        if ("E" in v or "e" in v) and RE["scientific"].match(v):
            exp = int(v.lower().split("e+")[1])
            if exp >= 10:
                self.sci[j].add(
                    i,
                    name,
                    raw,
                    "scientific notation: trailing digits were lost",
                    "re-export the original value as text",
                )
        if j in self.zero_len and v.isdigit() and len(v) < self.zero_len[j]:
            self.zeros[j].add(
                i, name, raw, f"{len(v)} digits; the column uses {self.zero_len[j]}", v.zfill(self.zero_len[j])
            )

    def finish(self):
        out = []
        for j, c in self.sci.items():
            if c.count:
                n = self.ctx.header[j]
                out.append(
                    self.issue(
                        "high",
                        f"Numbers in scientific notation in `{n}`",
                        f"{c.count:,} value(s) in `{n}` look like 1.23457E+15. Long numbers like IDs or "
                        "card numbers lose their trailing digits this way and can't be recovered.",
                        [n],
                        c,
                        denominator=self.ctx.profiles[j].non_null,
                    )
                )
        for j, c in self.zeros.items():
            if c.count:
                n = self.ctx.header[j]
                out.append(
                    self.issue(
                        "high",
                        f"Lost leading zeros in `{n}`",
                        f"{c.count:,} value(s) in `{n}` are shorter than the usual {self.zero_len[j]} "
                        "digits, which usually means a leading zero was dropped.",
                        [n],
                        c,
                        denominator=self.ctx.profiles[j].non_null,
                    )
                )
        return out


class NumbersAsText(Check):
    id = "numbers_as_text"
    category = "format"
    title = "Numbers stored as formatted text"
    description = 'Numeric columns containing values like "$1,200.50", "(500)", "12%" or "5 kg".'

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = []
        for p in ctx.profiles:
            sh = p.ctype.label_shares
            numeric = sh.get("decimal", 0)
            formatted = max(sh.get("currency_amount", 0), sh.get("percent", 0), sh.get("number_with_unit", 0))
            if p.ctype.label in ("integer", "decimal") or (numeric + formatted >= 0.8 and formatted > 0):
                self.columns.append(p.index)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if not v or RE["decimal"].match(v):
            return
        for label in ("currency_amount", "percent", "number_with_unit"):
            if RE[label].match(v):
                num = re.sub(r"[^\d.-]", "", v.replace("(", "-"))
                self.c[j].add(i, self.ctx.header[j], raw, label.replace("_", " "), num or None)
                return

    def finish(self):
        return [
            self.issue(
                "medium",
                f"Formatted numbers in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` carry currency symbols, separators, "
                "percent signs or units, so they won't sum or sort as numbers.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


class EncodingAndCharacters(Check):
    id = "encoding_and_characters"
    category = "format"
    title = "Encoding errors and hidden characters"
    description = (
        "Garbled text from the wrong character encoding (JosÃ© instead of José), invisible characters "
        "(zero-width and non-breaking spaces, control characters), line breaks inside values, and "
        "leading or trailing spaces."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.non_null or p.whitespace_only]
        self.mojibake = {j: self.collector() for j in self.columns}
        self.hidden = {j: self.collector() for j in self.columns}
        self.breaks = {j: self.collector() for j in self.columns}
        self.untrimmed = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if not v:
            return
        name = self.ctx.header[j]
        if raw.isascii():
            if raw is not v and raw != v:
                self.untrimmed[j].add(i, name, repr(raw), "leading/trailing spaces", v)
            if "\n" in v or "\r" in v:
                self.breaks[j].add(i, name, repr(v)[:80], "line break inside the value", " ".join(v.split()))
            return
        if MOJIBAKE.search(raw):
            self.mojibake[j].add(i, name, raw, "garbled characters", _fix_mojibake(raw))
        m = HIDDEN.search(raw)
        if m:
            ch = m.group(0)
            what = _HIDDEN_NAMES.get(ch, f"control character U+{ord(ch):04X}")
            self.hidden[j].add(
                i, name, repr(raw), what, HIDDEN.sub(lambda x: " " if x.group(0) in "   " else "", raw).strip()
            )
        elif raw != v:
            self.untrimmed[j].add(i, name, repr(raw), "leading/trailing spaces", v)
        if LINE_BREAK.search(v):
            self.breaks[j].add(i, name, repr(v)[:80], "line break inside the value", " ".join(v.split()))

    def finish(self):
        out = []
        h = self.ctx.header
        for j in self.columns:
            nn = max(1, self.ctx.profiles[j].non_null)
            if self.mojibake[j].count:
                out.append(
                    self.issue(
                        "high",
                        f"Garbled text (encoding errors) in `{h[j]}`",
                        f"{self.mojibake[j].count:,} value(s) contain sequences like 'Ã©' or 'â€™', the "
                        "result of reading UTF-8 text with the wrong encoding.",
                        [h[j]],
                        self.mojibake[j],
                        denominator=nn,
                    )
                )
            if self.hidden[j].count:
                out.append(
                    self.issue(
                        "medium",
                        f"Invisible characters in `{h[j]}`",
                        f"{self.hidden[j].count:,} value(s) contain invisible characters, which make "
                        "equal-looking values fail to match in joins and filters.",
                        [h[j]],
                        self.hidden[j],
                        denominator=nn,
                    )
                )
            if self.breaks[j].count:
                out.append(
                    self.issue(
                        "low",
                        f"Line breaks inside values in `{h[j]}`",
                        f"{self.breaks[j].count:,} value(s) contain line breaks, which break many CSV "
                        "tools and exports.",
                        [h[j]],
                        self.breaks[j],
                        denominator=nn,
                    )
                )
            if self.untrimmed[j].count:
                out.append(
                    self.issue(
                        "low",
                        f"Untrimmed spaces in `{h[j]}`",
                        f"{self.untrimmed[j].count:,} value(s) have leading or trailing spaces.",
                        [h[j]],
                        self.untrimmed[j],
                        denominator=nn,
                    )
                )
        return out


def _fix_mojibake(s: str) -> str | None:
    try:
        fixed = s.encode("cp1252").decode("utf-8")
        return fixed if fixed != s else None
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            fixed = s.encode("latin-1").decode("utf-8")
            return fixed if fixed != s else None
        except (UnicodeEncodeError, UnicodeDecodeError):
            return None


class Truncation(Check):
    id = "truncated_values"
    category = "format"
    title = "Truncated values"
    description = "Many values cut off at exactly the same length (often a field limit like 50 or 255 characters)."

    def prepare(self, ctx):
        super().prepare(ctx)
        self.limits: dict[int, int] = {}
        for p in ctx.profiles:
            if (
                p.max_len < 10
                or p.non_null < 50
                or p.distinct < 50
                or p.ctype.label not in ("text", "word", "person_name")
            ):
                continue  # a handful of repeated categories (one long city name) is not truncation
            if False:
                continue
            at_max = p.len_counts.get(p.max_len, 0)
            below = p.len_counts.get(p.max_len - 1, 0)
            if (
                at_max >= 5
                and at_max >= 0.01 * p.non_null
                and (p.max_len in FIELD_LIMITS or at_max >= 5 * max(1, below))
            ):
                self.limits[p.index] = p.max_len
        self.columns = list(self.limits)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if len(v) == self.limits[j]:
            self.c[j].add(i, self.ctx.header[j], v, f"exactly {self.limits[j]} characters")

    def finish(self):
        return [
            self.issue(
                "medium",
                f"Values cut off at {self.limits[j]} characters in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` are exactly {self.limits[j]} characters "
                "long, far more than other lengths: they were probably truncated by a field limit.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


def _norm_category(v: str) -> str:
    return re.sub(r"[\s_.\-']+", " ", HIDDEN.sub("", v).lower()).strip()


def _edit_distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


class InconsistentCategories(Check):
    id = "inconsistent_categories"
    category = "format"
    title = "Inconsistent spellings and typos in categories"
    description = (
        'The same category written different ways ("Male", "male ", "M"), typos of a common value '
        '("Califronia"), and yes/no columns using several representations.'
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.fix: dict[int, dict[str, tuple[str, str]]] = {}
        self.groups: dict[int, list[dict]] = {}
        for p in ctx.profiles:
            counts = p.topk.counts
            if (
                not p.topk_exact
                or not 2 <= len(counts) <= 400
                or p.ctype.label in ("integer", "decimal", "date", "email", "url", "uuid")
            ):
                continue
            if p.unique_ratio > 0.5 or re.search(
                r"name|first|last|surname|street|address|email|note|comment|desc", p.name, re.I
            ):
                continue
            fixes: dict[str, tuple[str, str]] = {}
            by_norm: dict[str, list[str]] = {}
            for v in counts:
                by_norm.setdefault(_norm_category(v), []).append(v)
            groups = []
            for variants in by_norm.values():
                if len(variants) > 1:
                    canonical = max(variants, key=lambda x: counts[x])
                    groups.append({"variants": {v: counts[v] for v in variants}, "canonical": canonical})
                    for v in variants:
                        if v != canonical:
                            fixes[v] = (canonical, "same value, different spelling")
            if p.ctype.label == "boolean":
                kinds = {_bool_kind(v) for v in counts}
                if len(kinds) > 1:
                    canonical = max(counts, key=counts.get)
                    groups.append({"variants": dict(counts), "canonical": _bool_kind(canonical)})
                    for v in counts:
                        if _bool_kind(v) != _bool_kind(canonical):
                            fixes.setdefault(v, (_to_kind(v, _bool_kind(canonical)), "mixed yes/no representations"))
            top = max(counts.values())
            frequent = [v for v, c in counts.items() if c >= max(10, 0.02 * top) and len(v) >= 4]
            for v, c in counts.items():
                if c > max(5, 0.01 * top) or len(v) < 4 or v in fixes or p.ctype.label == "boolean" or len(counts) > 60:
                    continue
                limit = 1 if len(v) < 8 else 2
                for f in frequent:
                    if (
                        f != v
                        and counts[f] >= 10 * c
                        and _edit_distance(v.lower(), f.lower(), limit) <= limit
                        and not (v.isdigit() or f.isdigit())
                    ):
                        fixes[v] = (f, "probable typo")
                        groups.append({"variants": {v: c, f: counts[f]}, "canonical": f, "typo": True})
                        break
            if fixes:
                self.fix[p.index] = fixes
                self.groups[p.index] = groups
        self.columns = list(self.fix)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        hit = self.fix[j].get(v)
        if hit:
            self.c[j].add(i, self.ctx.header[j], raw, hit[1], hit[0])

    def finish(self):
        out = []
        for j, c in self.c.items():
            if not c.count:
                continue
            n = self.ctx.header[j]
            groups = self.groups[j]
            shown = "; ".join(" / ".join(f"{v!r}" for v in g["variants"]) for g in groups[:4])
            out.append(
                self.issue(
                    "medium",
                    f"Inconsistent values in `{n}`",
                    f"{c.count:,} value(s) in `{n}` are variants or typos of another value ({shown}"
                    f"{'; ...' if len(groups) > 4 else ''}).",
                    [n],
                    c,
                    denominator=self.ctx.profiles[j].non_null,
                    groups=groups[:20],
                )
            )
        return out


def _bool_kind(v: str) -> str:
    low = v.lower()
    if low in ("true", "false"):
        return "true/false"
    if low in ("yes", "no"):
        return "yes/no"
    if low in ("y", "n"):
        return "Y/N"
    if low in ("t", "f"):
        return "T/F"
    return "1/0"


def _to_kind(v: str, kind: str) -> str:
    truthy = v.lower() in ("true", "yes", "y", "t", "1")
    pairs = {
        "true/false": ("true", "false"),
        "yes/no": ("yes", "no"),
        "Y/N": ("Y", "N"),
        "T/F": ("T", "F"),
        "1/0": ("1", "0"),
    }
    return pairs[kind][0 if truthy else 1]


def _example(row, col, val, note, suggestion=None):
    from ..model import Example

    return Example(row, col, val, note, suggestion)


_ = digits_only  # re-exported helper used by other modules

CHECKS = [DateProblems, ExcelDamage, NumbersAsText, EncodingAndCharacters, Truncation, InconsistentCategories]
