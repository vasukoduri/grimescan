"""Value classification and column semantic-type inference.

Every non-empty value is classified into the labels it *looks like* (an email, a date, a
5-digit number...). A column's semantic type is the most specific label that at least
``threshold`` of a sample of its values share, helped by hints in the column name. Values
that don't fit the column's type are what grimescan reports as "doesn't belong".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# ------------------------------------------------------------------ checksum validators
_DIGITS = re.compile(r"\D")


def digits_only(s: str) -> str:
    return _DIGITS.sub("", s)


def luhn_valid(number: str) -> bool:
    d = digits_only(number)
    if not 12 <= len(d) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        total += x
    return total % 10 == 0


def card_number_valid(number: str) -> bool:
    d = digits_only(number)
    if not luhn_valid(d):
        return False
    return bool(
        re.match(r"4|5[1-5]|2(2[2-9]|[3-6]\d|7[01]|720)|3[47]|6(011|5|4[4-9])|35(2[89]|[3-8])|3(0[0-5]|[68])", d)
    )


def iban_valid(value: str) -> bool:
    s = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def ssn_plausible(value: str) -> bool:
    d = digits_only(value)
    return len(d) == 9 and d[:3] not in ("000", "666") and d[3:5] != "00" and d[5:] != "0000"


# ------------------------------------------------------------------ dates
_MONTHS = {
    m: i
    for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)
}
DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "YYYY-MM-DD",
        re.compile(
            r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
        ),
    ),
    ("YYYY/MM/DD", re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")),
    ("NN/NN/YYYY", re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")),
    ("NN-NN-YYYY", re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")),
    ("NN.NN.YYYY", re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")),
    ("DD-Mon-YYYY", re.compile(r"^(\d{1,2})[- ]([A-Za-z]{3,9})[- ,]+(\d{4})$")),
    ("Mon DD, YYYY", re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$")),
]


@dataclass(frozen=True)
class ParsedDate:
    fmt: str
    value: date | None  # None = looks like a date but is impossible (Feb 30)
    ambiguous: bool = False  # 03/04/2025 could be March or April
    has_time: bool = False
    midnight: bool = False


def parse_date(s: str) -> ParsedDate | None:
    s = s.strip()
    if len(s) < 6 or len(s) > 35:
        return None
    for fmt, pat in DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        g = m.groups()
        try:
            if fmt in ("YYYY-MM-DD", "YYYY/MM/DD"):
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                has_time = fmt == "YYYY-MM-DD" and g[3] is not None
                midnight = has_time and int(g[3]) == 0 and int(g[4]) == 0 and int(g[5] or 0) == 0
                return ParsedDate(fmt, _mk(y, mo, d), has_time=has_time, midnight=midnight)
            if fmt in ("NN/NN/YYYY", "NN-NN-YYYY", "NN.NN.YYYY"):
                a, b, y = int(g[0]), int(g[1]), int(g[2])
                # prefer month/day for "/", day/month for "." and "-" when unambiguous
                if a > 12 and b <= 12:
                    return ParsedDate(fmt + " (day first)", _mk(y, b, a))
                if b > 12 and a <= 12:
                    return ParsedDate(fmt + " (month first)", _mk(y, a, b))
                if a <= 12 and b <= 12:
                    first = _mk(y, a, b) if "/" in fmt else _mk(y, b, a)
                    return ParsedDate(fmt, first, ambiguous=a != b)
                return ParsedDate(fmt, None)
            if fmt == "DD-Mon-YYYY":
                mo = _MONTHS.get(g[1][:3].lower())
                return ParsedDate(fmt, _mk(int(g[2]), mo, int(g[0])) if mo else None) if mo else None
            if fmt == "Mon DD, YYYY":
                mo = _MONTHS.get(g[0][:3].lower())
                return ParsedDate(fmt, _mk(int(g[2]), mo, int(g[1]))) if mo else None
        except (TypeError, ValueError):
            return ParsedDate(fmt, None)
    return None


def _mk(y: int, m: int | None, d: int) -> date | None:
    try:
        return date(y, m or 0, d)
    except ValueError:
        return None


# ------------------------------------------------------------------ value classifiers
RE = {
    "integer": re.compile(r"^[+-]?\d+$"),
    "decimal": re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$"),
    "email": re.compile(r"^[\w.+%-]+@[\w-]+(\.[\w-]+)*\.[A-Za-z]{2,}$"),
    "url": re.compile(r"^(https?://|www\.)\S+$", re.I),
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "ipv4": re.compile(r"^((25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(25[0-5]|2[0-4]\d|1?\d?\d)$"),
    "phone": re.compile(r"^(\+?\d{1,3}[ .-]?)?(\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}$"),
    "us_zip": re.compile(r"^\d{5}(-\d{4})?$"),
    "ssn": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
    "currency_amount": re.compile(
        r"^[($-]*\s*[$€£¥₹]\s*-?[\d,]+(\.\d+)?\)?$|^-?\d{1,3}(,\d{3})+(\.\d+)?$|^\(\d[\d,]*(\.\d+)?\)$"
    ),
    "percent": re.compile(r"^[+-]?\d+(\.\d+)?\s?%$"),
    "number_with_unit": re.compile(
        r"^[+-]?\d+(\.\d+)?\s?(kg|g|lb|lbs|oz|km|m|cm|mm|mi|ft|in|days?|hrs?|hours?|mins?|minutes?|s|sec|secs|yrs?|years?|months?|units?|pcs|usd|eur|gbp)$",
        re.I,
    ),
    "scientific": re.compile(r"^\d\.\d+E\+\d+$", re.I),
    "person_name": re.compile(r"^[A-Z][a-zA-Z'’.-]+( [A-Z][a-zA-Z'’.-]*){1,3}$"),
    "word": re.compile(r"^[A-Za-z][A-Za-z '&./-]{0,40}$"),
}
BOOLEANS = {"true", "false", "yes", "no", "y", "n", "t", "f"}
US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
    "OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR GU VI AS MP".split()
)

#: label priority: more specific labels win when several fit the column
PRIORITY = [
    "uuid",
    "email",
    "url",
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
    "currency_amount",
    "percent",
    "person_name",
    "word",
    "text",
]

#: column-name hints that make an ambiguous label win (a 5-digit number in "zip" is a ZIP code)
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "us_zip": ("zip", "postal", "postcode"),
    "phone": ("phone", "mobile", "cell", "tel", "fax"),
    "date": ("date", "dob", "birth", "_at", "time", "day", "created", "updated", "since", "until"),
    "us_state": ("state",),
    "person_name": ("name", "first", "last", "surname", "contact", "owner"),
    "ssn": ("ssn", "social"),
    "email": ("email", "e_mail"),
    "card_number": ("card", "pan", "cc_"),
}


def classify(value: str) -> set[str]:
    """All labels a non-empty, stripped value fits."""
    v = value.strip()
    out: set[str] = set()
    low = v.lower()
    if low in BOOLEANS or v in ("0", "1"):
        out.add("boolean")
    if RE["integer"].match(v):
        out.update(("integer", "decimal"))
        d = digits_only(v)
        if len(d) == 5:
            out.add("us_zip")
        if 7 <= len(d) <= 15:
            out.add("phone")
        if 13 <= len(d) <= 19 and card_number_valid(d):
            out.add("card_number")
    elif RE["decimal"].match(v):
        out.add("decimal")
        if RE["scientific"].match(v):
            out.add("scientific")
    if RE["currency_amount"].match(v):
        out.add("currency_amount")
    if RE["percent"].match(v):
        out.add("percent")
    if RE["number_with_unit"].match(v):
        out.add("number_with_unit")
    if "@" in v and RE["email"].match(v):
        out.add("email")
    if RE["url"].match(v):
        out.add("url")
    if RE["uuid"].match(v):
        out.add("uuid")
    if RE["ipv4"].match(v):
        out.add("ipv4")
    if not out & {"integer", "decimal"} and RE["phone"].match(v) and 7 <= len(digits_only(v)) <= 15:
        out.add("phone")
    if RE["us_zip"].match(v):
        out.add("us_zip")
    if RE["ssn"].match(v) and ssn_plausible(v):
        out.add("ssn")
    if 13 <= len(digits_only(v)) <= 19 and len(digits_only(v)) >= len(v) - 4 and card_number_valid(v):
        out.add("card_number")
    if len(v) >= 15 and v[:2].isalpha() and iban_valid(v):
        out.add("iban")
    if v.upper() in US_STATES and len(v) == 2:
        out.add("us_state")
    pd_ = parse_date(v) if any(c in v for c in "-/., ") and any(c.isdigit() for c in v) else None
    if pd_ is not None:
        out.add("date")
    if RE["person_name"].match(v):
        out.add("person_name")
    if RE["word"].match(v):
        out.add("word")
    out.add("text")
    return out


def best_label(labels: set[str]) -> str:
    for p in PRIORITY:
        if p in labels:
            return p
    return "text"


def describe(value: str) -> str:
    """Human description of what a value looks like (for 'an email in a name column')."""
    labels = classify(value) if value.strip() else set()
    nice = {
        "us_zip": "a ZIP code",
        "person_name": "a person's name",
        "card_number": "a card number",
        "currency_amount": "a money amount",
        "number_with_unit": "a number with a unit",
        "ssn": "an SSN",
        "ipv4": "an IP address",
        "uuid": "a UUID",
        "iban": "an IBAN",
        "us_state": "a state code",
        "integer": "a whole number",
        "decimal": "a number",
        "word": "a word",
        "text": "free text",
        "boolean": "a yes/no value",
        "date": "a date",
        "email": "an email address",
        "url": "a URL",
        "phone": "a phone number",
        "percent": "a percentage",
        "scientific": "scientific notation",
    }
    for p in [
        "scientific",
        "email",
        "url",
        "uuid",
        "iban",
        "card_number",
        "ssn",
        "date",
        "currency_amount",
        "number_with_unit",
        "percent",
        "phone",
        "us_zip",
        "integer",
        "decimal",
        "boolean",
        "person_name",
        "word",
    ]:
        if p in labels:
            return nice[p]
    return "free text"


# ------------------------------------------------------------------ column inference
@dataclass
class ColumnType:
    label: str
    share: float  # share of sampled values fitting the label
    label_shares: dict[str, float] = field(default_factory=dict)


def infer_column_type(name: str, values: list[str], threshold: float = 0.8) -> ColumnType:
    """Pick the most specific label that at least ``threshold`` of the values fit."""
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return ColumnType("empty", 0.0)
    counts: dict[str, int] = {}
    for v in vals:
        for label in classify(v):
            counts[label] = counts.get(label, 0) + 1
    shares = {k: c / len(vals) for k, c in counts.items()}
    lname = name.lower()
    # name hints first: a column called "zip" with 5-digit numbers is a ZIP code
    for label, hints in NAME_HINTS.items():
        if any(h in lname for h in hints) and shares.get(label, 0) >= threshold * 0.75:
            return ColumnType(label, shares[label], shares)
    for label in PRIORITY:
        share = shares.get(label, 0)
        if share < threshold:
            continue
        if label == "us_zip" or label == "phone" or label == "boolean" and shares.get("integer", 0) >= threshold:
            continue  # without a name hint these are just numbers
        if label == "us_state" and len(set(vals)) < 3:
            continue
        if label == "person_name" and not any(h in lname for h in NAME_HINTS["person_name"]):
            continue
        return ColumnType(label, share, shares)
    return ColumnType("text", 1.0, shares)


def is_numeric_label(label: str) -> bool:
    return label in ("integer", "decimal")


def to_float(value: str) -> float | None:
    v = value.strip()
    if RE["decimal"].match(v):
        try:
            return float(v)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ fast membership tests
NULL_TOKENS = {
    "n/a",
    "na",
    "n.a.",
    "n/a.",
    "null",
    "none",
    "nil",
    "nan",
    "-",
    "--",
    "---",
    "?",
    "??",
    "unknown",
    "unk",
    "tbd",
    "tba",
    "not available",
    "not applicable",
    "no data",
    "missing",
    "#n/a",
    "#null!",
    "#value!",
    "#ref!",
    "#div/0!",
    "undefined",
    "blank",
    "empty",
    "(blank)",
    "(null)",
    "[null]",
    "<null>",
    "(none)",
    "nothing",
    "void",
    "not provided",
    "not specified",
    "unspecified",
    "no value",
    "default",
    ".",
}
DATE_SENTINELS = {
    date(1900, 1, 1),
    date(1970, 1, 1),
    date(1, 1, 1),
    date(9999, 12, 31),
    date(2099, 12, 31),
    date(1899, 12, 30),
    date(1899, 12, 31),
    date(2999, 12, 31),
    date(3000, 1, 1),
}


def fits(label: str, v: str) -> bool:
    """Does stripped value ``v`` fit ``label``? Cheaper than :func:`classify`."""
    if label in ("text", "word", "empty"):
        return True
    if label == "integer":
        return bool(RE["integer"].match(v))
    if label == "decimal":
        return bool(RE["decimal"].match(v))
    if label == "date":
        return parse_date(v) is not None
    if label == "boolean":
        return v.lower() in BOOLEANS or v in ("0", "1")
    if label == "us_zip":
        return bool(RE["us_zip"].match(v))
    if label == "phone":
        return (bool(RE["phone"].match(v)) or bool(RE["integer"].match(v))) and 7 <= len(digits_only(v)) <= 15
    if label == "us_state":
        return v.upper() in US_STATES and len(v) == 2
    if label == "person_name":
        return bool(RE["person_name"].match(v)) or bool(RE["word"].match(v))
    if label == "card_number":
        return card_number_valid(v)
    if label == "iban":
        return iban_valid(v)
    if label == "ssn":
        return bool(RE["ssn"].match(v)) and ssn_plausible(v)
    pat = RE.get(label)
    return bool(pat.match(v)) if pat else True


def is_null_token(v: str) -> bool:
    return v.lower() in NULL_TOKENS
