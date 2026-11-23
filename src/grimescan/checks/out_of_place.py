"""Values that don't belong in their column, or rows that don't belong in the data."""

from __future__ import annotations

import re

from ..model import Check
from ..semantics import NULL_TOKENS, RE, describe, fits, to_float

SPECIFIC = {
    "email",
    "url",
    "uuid",
    "ipv4",
    "iban",
    "card_number",
    "ssn",
    "date",
    "boolean",
    "us_state",
    "us_zip",
    "phone",
    "integer",
    "decimal",
    "person_name",
}
NUMERIC_FORMATTED = ("currency_amount", "percent", "number_with_unit", "scientific")

PLACEHOLDERS = {
    "test",
    "testing",
    "test test",
    "test user",
    "testuser",
    "tester",
    "asdf",
    "asdfg",
    "asdfgh",
    "asdfasdf",
    "qwerty",
    "qwer",
    "zxcv",
    "xxx",
    "xxxx",
    "xxxxx",
    "yyy",
    "zzz",
    "aaa",
    "aaaa",
    "abc",
    "abcd",
    "abcdef",
    "abc123",
    "dummy",
    "sample",
    "example",
    "foo",
    "bar",
    "foobar",
    "baz",
    "blah",
    "blah blah",
    "lorem",
    "lorem ipsum",
    "john doe",
    "jane doe",
    "joe bloggs",
    "john smith test",
    "first last",
    "firstname lastname",
    "first name",
    "last name",
    "your name",
    "name",
    "fake",
    "placeholder",
    "changeme",
    "change me",
    "temp",
    "tmp",
    "todo",
    "fixme",
    "test@test.com",
    "test@example.com",
    "a@a.com",
    "abc@abc.com",
    "email@email.com",
    "user@example.com",
    "noreply@example.com",
    "123 main st",
    "123 main street",
    "1234 main st",
    "555-555-5555",
    "(555) 555-5555",
    "000-000-0000",
    "123-456-7890",
    "1234567890",
    "0000000000",
    "1111111111",
    "9999999999",
    "000-00-0000",
    "123-45-6789",
    "111-11-1111",
    "999-99-9999",
    "00000",
    "12345",
    "99999",
}
DIGIT_PLACEHOLDER_TYPES = {"phone", "ssn", "us_zip", "email", "person_name", "text", "word"}
KEYBOARD = re.compile(r"(asdf|sdfg|dfgh|fghj|ghjk|hjkl|qwer|wert|erty|rtyu|tyui|yuio|uiop|zxcv|xcvb|cvbn|vbnm)", re.I)
REPEATED = re.compile(r"^(.)\1{3,}$")
LOREM = re.compile(r"\blorem ipsum\b|\bdolor sit amet\b", re.I)

HTML = re.compile(
    r"<\s*(/?)(html|body|div|span|p|br|a|b|i|img|table|tr|td|script|style|font|ul|li|h[1-6])\b[^>]*>", re.I
)
SQL = re.compile(
    r"\b(select\s+.+\s+from|insert\s+into|update\s+\w+\s+set|delete\s+from|drop\s+table|union\s+select)\b", re.I
)
TRACE = re.compile(
    r"Traceback \(most recent call last\)|Exception in thread|\bat [a-z][\w$]*(\.[\w$]+)+\(|"
    r"NullPointerException|Stack ?trace|SyntaxError:|TypeError:|ORA-\d{5}|SQLSTATE",
    re.I,
)
JS = re.compile(r"^(undefined|\[object Object\]|NaN|Infinity|-Infinity|function\s*\(|=>)$|\{\{\s*\w+\s*\}\}|\$\{\w+\}")
JS_TOKENS = {"undefined", "NaN", "Infinity", "null"} - {"null"}
JSON_LIKE = re.compile(r"^\s*[\[{].*[\]}]\s*$", re.S)

SCRIPTS = [
    ("Cyrillic", re.compile(r"[Ѐ-ӿ]")),
    ("Greek", re.compile(r"[Ͱ-Ͽ]")),
    ("Arabic", re.compile(r"[؀-ۿ]")),
    ("Hebrew", re.compile(r"[֐-׿]")),
    ("Devanagari", re.compile(r"[ऀ-ॿ]")),
    ("Thai", re.compile(r"[฀-๿]")),
    ("Chinese/Japanese", re.compile(r"[぀-ヿ一-鿿]")),
    ("Korean", re.compile(r"[가-힯]")),
]
SUMMARY = re.compile(r"^(grand\s+)?(total|totals|sum|subtotal|sub-total|average|avg|count|summary)s?:?$", re.I)


def _is_header_or_total(values: list[str | None], header: list[str]) -> bool:
    names = [h.strip().lower() for h in header]
    return structural_row(values, names, len(names))


def structural_row(values: list[str | None], names: list[str], field_count: int) -> bool:
    """A repeated header, a row with the wrong number of fields, or a total/summary row.

    These are reported once by their own checks and skipped by the per-value checks, so a
    single broken row doesn't show up as an out-of-place value in every column."""
    if field_count >= 0 and field_count != len(names):
        return True
    head = [v.strip() for v in values[:2] if v]
    if not head:
        return False
    if any(h.lower() in names for h in head):  # cheap pre-test before comparing the whole row
        filled = [(j, v.strip().lower()) for j, v in enumerate(values) if v and v.strip()]
        if sum(1 for j, v in filled if v == names[j]) >= max(2, 0.6 * len(filled)):
            return True
    return any(len(h) <= 12 and SUMMARY.match(h) for h in head)


class TypeMismatch(Check):
    id = "type_mismatch"
    category = "out_of_place"
    title = "Values that don't match the column's type"
    description = (
        "grimescan works out what each column holds (emails, dates, ZIP codes, numbers, names...) and "
        "reports values that don't fit, such as an email in a name column or text in an amount column."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.ctype.label in SPECIFIC and p.ctype.share < 1.0]
        self.c = {j: self.collector() for j in self.columns}
        self.kinds: dict[int, dict[str, int]] = {j: {} for j in self.columns}

    def value(self, j, raw, v, i):
        if not v or v.lower() in NULL_TOKENS:
            return
        label = self.ctx.profiles[j].ctype.label
        if fits(label, v):
            return
        if label in ("integer", "decimal") and any(RE[k].match(v) for k in NUMERIC_FORMATTED):
            return  # reported by numbers_as_text / spreadsheet_damage
        if label == "integer" and RE["decimal"].match(v):
            return
        if v.lower() in PLACEHOLDERS:
            return  # reported as a placeholder
        if label == "us_zip" and v.isdigit() and 3 <= len(v) < 5:
            return  # reported as lost leading zeros
        self.ctx.row_mismatches += 1
        looks = describe(v)
        k = self.kinds[j]
        k[looks] = k.get(looks, 0) + 1
        self.c[j].add(i, self.ctx.header[j], raw, f"{looks} in a column of {_type_name(label)}")

    def finish(self):
        out = []
        for j, c in self.c.items():
            if not c.count:
                continue
            p = self.ctx.profiles[j]
            label = p.ctype.label
            kinds = ", ".join(f"{n:,} {k}" for k, n in sorted(self.kinds[j].items(), key=lambda kv: -kv[1])[:4])
            sev = (
                "high"
                if label
                in ("email", "date", "uuid", "us_zip", "ssn", "card_number", "iban", "integer", "decimal", "ipv4")
                else "medium"
            )
            share = c.count / max(1, p.non_null)
            out.append(
                self.issue(
                    sev,
                    f"Out-of-place values in `{p.name}` ({_type_name(label)})",
                    f"`{p.name}` holds {_type_name(label)}, but {c.count:,} value(s) ({share:.1%}) don't fit: {kinds}.",
                    [p.name],
                    c,
                    denominator=p.non_null,
                    column_type=label,
                    breakdown=self.kinds[j],
                )
            )
        return out


def _type_name(label: str) -> str:
    return {
        "us_zip": "ZIP codes",
        "person_name": "names",
        "card_number": "card numbers",
        "integer": "whole numbers",
        "decimal": "numbers",
        "email": "email addresses",
        "date": "dates",
        "phone": "phone numbers",
        "url": "URLs",
        "uuid": "UUIDs",
        "ipv4": "IP addresses",
        "iban": "IBANs",
        "ssn": "SSNs",
        "us_state": "state codes",
        "boolean": "yes/no values",
    }.get(label, label)


class SwappedColumns(Check):
    id = "swapped_columns"
    category = "out_of_place"
    title = "Swapped columns"
    description = (
        "Rows where two columns' values are swapped: latitude and longitude reversed, or an email in the "
        "phone column while the phone number sits in the email column."
    )
    needs_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        names = {p.name.lower(): p.index for p in ctx.profiles}
        self.lat = next(
            (j for n, j in names.items() if n in ("lat", "latitude") or n.endswith("_lat") or n.endswith("latitude")),
            None,
        )
        self.lon = next(
            (
                j
                for n, j in names.items()
                if n in ("lon", "lng", "long", "longitude")
                or n.endswith("_lon")
                or n.endswith("_lng")
                or n.endswith("longitude")
            ),
            None,
        )
        typed = [
            p
            for p in ctx.profiles
            if p.ctype.label in ("email", "phone", "date", "us_zip", "url", "person_name", "uuid", "us_state")
        ]
        self.pairs = [
            (a.index, b.index) for k, a in enumerate(typed) for b in typed[k + 1 :] if a.ctype.label != b.ctype.label
        ][:30]
        self.geo = self.collector()
        self.c = {pair: self.collector() for pair in self.pairs}
        self.ranges = {}
        for j in (self.lat, self.lon):
            if j is None:
                continue
            nums = sorted(x for x in (to_float(v) for v in ctx.profiles[j].sample.items) if x is not None)
            if len(nums) >= 50:
                k = max(1, len(nums) // 200)
                lo, hi = nums[k], nums[-k - 1]
                pad = 0.1 * (hi - lo)  # trimming the extremes must not exclude real edge values
                self.ranges[j] = (lo - pad, hi + pad)

    def row(self, i, values, field_count):
        if self.lat is not None and self.lon is not None:
            la, lo = values[self.lat], values[self.lon]
            fa, fo = to_float(la) if la else None, to_float(lo) if lo else None
            if fa is not None and fo is not None and self._swapped(fa, fo):
                self.geo.add(
                    i,
                    f"{self.ctx.header[self.lat]}/{self.ctx.header[self.lon]}",
                    f"{la}, {lo}",
                    "latitude and longitude look reversed",
                    f"{lo.strip()}, {la.strip()}",
                )
        if not self.pairs or self.ctx.row_mismatches < 2:
            return  # a swap makes both values misfit their columns
        for a, b in self.pairs:
            va, vb = values[a], values[b]
            if not va or not vb:
                continue
            va, vb = va.strip(), vb.strip()
            ta, tb = self.ctx.profiles[a].ctype.label, self.ctx.profiles[b].ctype.label
            if not fits(ta, va) and not fits(tb, vb) and fits(tb, va) and fits(ta, vb):
                self.c[(a, b)].add(
                    i,
                    f"{self.ctx.header[a]}/{self.ctx.header[b]}",
                    f"{va} | {vb}",
                    "values swapped between the two columns",
                    f"{vb} | {va}",
                )

    def _swapped(self, lat: float, lon: float) -> bool:
        if abs(lat) > 90 and abs(lon) <= 90:
            return True
        r_lat, r_lon = self.ranges.get(self.lat), self.ranges.get(self.lon)
        if not r_lat or not r_lon:
            return False
        inside = lambda x, r: r[0] <= x <= r[1]  # noqa: E731
        return not inside(lat, r_lat) and not inside(lon, r_lon) and inside(lat, r_lon) and inside(lon, r_lat)

    def finish(self):
        out = []
        if self.geo.count:
            names = [self.ctx.header[self.lat], self.ctx.header[self.lon]]
            out.append(
                self.issue(
                    "high",
                    "Latitude and longitude swapped",
                    f"{self.geo.count:,} row(s) have a latitude that fits the longitude column and a "
                    "longitude that fits the latitude column: the two were probably swapped.",
                    names,
                    self.geo,
                )
            )
        for (a, b), c in self.c.items():
            if c.count:
                names = [self.ctx.header[a], self.ctx.header[b]]
                out.append(
                    self.issue(
                        "high",
                        f"`{names[0]}` and `{names[1]}` swapped in some rows",
                        f"{c.count:,} row(s) have the `{names[0]}` value in `{names[1]}` and vice versa.",
                        names,
                        c,
                    )
                )
        return out


class BrokenRows(Check):
    id = "broken_rows"
    category = "out_of_place"
    title = "Broken and shifted rows"
    description = (
        "CSV rows with the wrong number of fields (often an unquoted comma inside a value), and rows "
        "whose values have shifted one column left or right."
    )
    needs_rows = True
    sees_structural_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        self.n = len(ctx.header)
        self.fields = self.collector()
        self.types = [p.ctype.label for p in ctx.profiles]
        self.typed = sum(1 for t in self.types if t in SPECIFIC)
        self.shifted = self.collector()

    def row(self, i, values, field_count):
        if field_count >= 0 and field_count != self.n:
            self.fields.add(i, None, None, f"{field_count} fields; the header has {self.n}", "fix quoting at source")
            return
        if self.ctx.row_mismatches < 3 or _is_header_or_total(values, self.ctx.header):
            return
        if self.typed < 3 or self.ctx.row_mismatches < 3:
            return  # a shifted row produces several out-of-type values, counted by type_mismatch
        right = left = 0
        for j in range(self.n):
            v = values[j]
            if not v:
                continue
            v = v.strip()
            t = self.types[j]
            if t not in SPECIFIC or fits(t, v):
                continue
            if j + 1 < self.n and self.types[j + 1] in SPECIFIC and fits(self.types[j + 1], v):
                left += 1
            if j > 0 and self.types[j - 1] in SPECIFIC and fits(self.types[j - 1], v):
                right += 1
        if max(left, right) >= 3:
            self.shifted.add(
                i,
                None,
                None,
                f"{max(left, right)} values sit one column {'left' if left >= right else 'right'} of where they belong",
            )

    def finish(self):
        out = []
        if self.fields.count:
            out.append(
                self.issue(
                    "high",
                    "Rows with the wrong number of fields",
                    f"{self.fields.count:,} row(s) don't have {self.n} fields. Their values are "
                    "misaligned with the header, usually because of an unquoted delimiter or line break.",
                    [],
                    self.fields,
                    _fetch=True,
                )
            )
        if self.shifted.count:
            out.append(
                self.issue(
                    "high",
                    "Shifted rows",
                    f"{self.shifted.count:,} row(s) have several values that fit the neighbouring "
                    "column instead of their own: the row shifted by one position.",
                    [],
                    self.shifted,
                    _fetch=True,
                )
            )
        return out


class HeaderAndSummaryRows(Check):
    id = "header_and_summary_rows"
    category = "out_of_place"
    title = "Header and total rows inside the data"
    description = (
        "A copy of the header row in the middle of the data (from concatenated files) and summary rows "
        'like "Total" or "Grand total" exported along with the records.'
    )
    needs_rows = True
    sees_structural_rows = True

    def prepare(self, ctx):
        super().prepare(ctx)
        self.names = [h.strip().lower() for h in ctx.header]
        self.headers = self.collector()
        self.summary = self.collector()

    def row(self, i, values, field_count):
        filled = [(j, v.strip().lower()) for j, v in enumerate(values) if v and v.strip()]
        if filled and sum(1 for j, v in filled if v == self.names[j]) >= max(2, 0.6 * len(filled)):
            self.headers.add(i, None, ", ".join(v for _, v in filled[:4]), "repeated header row", "delete the row")
            return
        for j in range(min(2, len(values))):
            v = values[j]
            if v and SUMMARY.match(v.strip()):
                self.summary.add(i, self.ctx.header[j], v, "summary row exported with the data", "delete the row")
                return

    def finish(self):
        out = []
        if self.headers.count:
            out.append(
                self.issue(
                    "high",
                    "Header row repeated inside the data",
                    f"{self.headers.count:,} row(s) repeat the column names, typically from files that "
                    "were concatenated with their headers.",
                    [],
                    self.headers,
                )
            )
        if self.summary.count:
            out.append(
                self.issue(
                    "medium",
                    "Total or summary rows inside the data",
                    f"{self.summary.count:,} row(s) look like totals or summaries, which double-count "
                    "when the data is aggregated.",
                    [],
                    self.summary,
                    _fetch=True,
                )
            )
        return out


class Placeholders(Check):
    id = "placeholder_values"
    category = "out_of_place"
    title = "Placeholder, test and junk values"
    description = (
        'Values like "test", "asdf", "lorem ipsum", "John Doe", test@test.com, 555-555-5555 or "xxxx" that '
        "were typed to get past a form, or left over from testing."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [
            p.index for p in ctx.profiles if p.non_null and p.ctype.label not in ("uuid", "decimal", "date")
        ]
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if len(v) < 3:
            return
        low = " ".join(v.lower().split())
        t = self.ctx.profiles[j].ctype.label
        reason = None
        if low in PLACEHOLDERS:
            if (
                low.replace("-", "").replace(" ", "").replace("(", "").replace(")", "").isdigit()
                and t not in DIGIT_PLACEHOLDER_TYPES
            ):
                return
            reason = "known placeholder value"
        elif LOREM.search(low):
            reason = "lorem ipsum filler text"
        elif t not in ("integer", "decimal", "us_zip", "phone") and REPEATED.match(low):
            reason = "one character repeated"
        elif (
            " " not in low
            and 4 <= len(low) <= 12
            and low.isalpha()
            and KEYBOARD.search(low)
            and sum(ch in "aeiou" for ch in low) <= 1
        ):
            reason = "keyboard mash"
        if reason:
            self.c[j].add(i, self.ctx.header[j], raw, reason, "replace with the real value or null")

    def finish(self):
        return [
            self.issue(
                "medium",
                f"Placeholder or junk values in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` look like test data or filler.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


class MarkupAndCode(Check):
    id = "markup_and_code"
    category = "out_of_place"
    title = "Markup, code and error messages inside values"
    description = (
        "HTML tags, SQL statements, stack traces, unrendered template variables ({{name}}), JavaScript "
        "artifacts like [object Object], and JSON blobs in columns that shouldn't hold them."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.non_null]
        self.json_cols = {
            p.index
            for p in ctx.profiles
            if sum(1 for v in p.sample.items if JSON_LIKE.match(v)) >= 0.5 * max(1, len(p.sample.items))
        }
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if len(v) < 3:
            return
        what = None
        if v.isalnum():
            if v in JS_TOKENS:
                self.c[j].add(i, self.ctx.header[j], raw, "a code or template artifact")
            return
        if "<" in v and HTML.search(v):
            what = "HTML markup"
        elif len(v) >= 12 and TRACE.search(v):
            what = "an error message or stack trace"
        elif len(v) >= 15 and " " in v and SQL.search(v):
            what = "a SQL statement"
        elif JS.search(v):
            what = "a code or template artifact"
        elif j not in self.json_cols and v[0] in "[{" and JSON_LIKE.match(v) and ":" in v:
            what = "a JSON object"
        if what:
            self.c[j].add(i, self.ctx.header[j], raw[:120], what)

    def finish(self):
        return [
            self.issue(
                "medium",
                f"Markup or code inside `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` contain markup, code or error output.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


class UnexpectedScript(Check):
    id = "unexpected_script"
    category = "out_of_place"
    title = "Text in an unexpected writing system"
    description = "Values in Cyrillic, Chinese, Arabic and other scripts in a column that is otherwise Latin-only."

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = []
        for p in ctx.profiles:
            items = p.sample.items
            if p.ctype.label in ("text", "word", "person_name") and len(items) >= 50:
                foreign = sum(1 for v in items if not v.isascii() and any(s.search(v) for _, s in SCRIPTS))
                if foreign <= 0.01 * len(items):
                    self.columns.append(p.index)
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if v.isascii():
            return
        for name, pat in SCRIPTS:
            if pat.search(v):
                self.c[j].add(i, self.ctx.header[j], raw, f"{name} text")
                return

    def finish(self):
        return [
            self.issue(
                "low",
                f"Unexpected writing system in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` use a different script from the rest of the column.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
            )
            for j, c in self.c.items()
            if c.count
        ]


CHECKS = [TypeMismatch, SwappedColumns, BrokenRows, HeaderAndSummaryRows, Placeholders, MarkupAndCode, UnexpectedScript]
