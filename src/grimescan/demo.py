"""A synthetic "dirty" customer-orders dataset with known, planted problems.

Used by ``grimescan demo`` and the test suite: every planted problem is recorded, so you
can see what grimescan catches. All people, cards and identifiers are fictional (card
numbers are standard test numbers; SSNs use the never-issued 9xx range).
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

FIRST = [
    "Avery",
    "Jordan",
    "Priya",
    "Mateo",
    "Lena",
    "Omar",
    "Grace",
    "Ravi",
    "Sofia",
    "Noah",
    "Mei",
    "Elijah",
    "Hana",
    "Lucas",
    "Amara",
    "Diego",
    "Ingrid",
    "Kofi",
    "Yara",
    "Theo",
    "Anika",
    "Felix",
    "Zara",
    "Malik",
]
LAST = [
    "Whitfield",
    "Okafor",
    "Ramanathan",
    "Castellano",
    "Lindqvist",
    "Haddad",
    "Thornbury",
    "Venkatesan",
    "Moreau",
    "Brennan",
    "Takahashi",
    "Abernathy",
    "Kowalczyk",
    "Ferreira",
    "Osei",
    "Halvorsen",
]
STATES = [
    ("DE", "19801"),
    ("NJ", "07030"),
    ("NY", "10001"),
    ("PA", "19103"),
    ("CA", "94105"),
    ("TX", "73301"),
    ("MA", "02108"),
    ("IL", "60601"),
]
SEGMENTS = ["Retail", "Retail", "Retail", "Business", "Business", "Premium"]
CHANNELS = ["web", "web", "mobile", "store", "phone"]
NOTES = [
    "Prefers email contact",
    "Called about delivery window",
    "Gift order",
    "Repeat customer",
    "Asked for invoice",
    "Left at front desk",
    "",
]

HEADER = [
    "customer_id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "state",
    "zip",
    "segment",
    "channel",
    "birth_date",
    "age",
    "signup_date",
    "order_date",
    "ship_date",
    "subtotal",
    "tax",
    "discount",
    "total",
    "quantity",
    "unit_weight_kg",
    "latitude",
    "longitude",
    "notes",
]

PLANTED = {
    "disguised_nulls": "N/A, unknown, -1 and 1900-01-01 placeholders",
    "whitespace_only": "blank strings in email",
    "spreadsheet_damage": "customer IDs in scientific notation and ZIP codes without leading zeros",
    "date_problems": "mixed date formats, Feb 30, future birth dates",
    "numbers_as_text": "amounts like $1,204.50",
    "encoding_and_characters": "JosÃ© mojibake, zero-width spaces, untrimmed values",
    "inconsistent_categories": "Retail / retail / RETAIL and a typo 'Retial'",
    "type_mismatch": "emails in the phone column, text in amounts",
    "swapped_columns": "latitude and longitude swapped",
    "broken_rows": "rows with an extra field",
    "header_and_summary_rows": "a repeated header row and a TOTAL row",
    "placeholder_values": "test, asdf, John Doe, test@test.com",
    "markup_and_code": "HTML and a stack trace in notes",
    "duplicates": "exact duplicates, a batch loaded twice, duplicate IDs with conflicting values",
    "near_duplicates": "the same customer with small spelling differences",
    "date_order": "ship date before order date",
    "total_mismatch": "totals that don't equal subtotal + tax - discount",
    "age_vs_birth_date": "ages that don't match birth dates",
    "impossible_values": "negative quantities and an age of 230",
    "outliers": "a 9,999,999 subtotal",
    "unit_mixup": "some weights in grams instead of kilograms",
    "pii_in_free_text": "an SSN and a card number typed into notes",
    "secrets": "an API key pasted into notes",
    "missing_by_group": "phone missing for almost every 'store' order",
    "default_value_inflation": "signup_date defaulted to the load date",
}


def generate(path: str | Path, rows: int = 5000, seed: int = 7) -> dict[str, str]:
    """Write the demo CSV. Returns the planted problems (check id → description)."""
    rng = random.Random(seed)
    path = Path(path)
    out: list[list[str]] = []
    load_date = date(2026, 11, 1)
    for n in range(rows):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        st, zp = rng.choice(STATES)
        born = date(1950, 1, 1) + timedelta(days=rng.randint(0, 365 * 50))
        age = 2026 - born.year - ((11, 23) < (born.month, born.day))
        signup = load_date - timedelta(days=rng.randint(30, 2000))
        if rng.random() < 0.35:
            signup = load_date  # default value written when the real date was missing
        order = signup + timedelta(days=rng.randint(0, 25))
        ship = order + timedelta(days=rng.randint(1, 7))
        subtotal = round(rng.uniform(15, 900), 2)
        tax = round(subtotal * 0.06, 2)
        discount = round(rng.choice([0, 0, 0, 5, 10, 15]), 2)
        total = round(subtotal + tax - discount, 2)
        channel = rng.choice(CHANNELS)
        phone = f"(302) 555-01{rng.randint(0, 99):02d}"
        if channel == "store" and rng.random() < 0.9:
            phone = ""
        lat, lon = round(rng.uniform(38.5, 41.5), 4), round(rng.uniform(-76.5, -74.0), 4)
        out.append(
            [
                str(100000 + n),
                fn,
                ln,
                f"{fn.lower()}.{ln.lower()}{n}@example.com",
                phone,
                st,
                zp,
                rng.choice(SEGMENTS),
                channel,
                born.isoformat(),
                str(age),
                signup.isoformat(),
                order.isoformat(),
                ship.isoformat(),
                f"{subtotal:.2f}",
                f"{tax:.2f}",
                f"{discount:.2f}",
                f"{total:.2f}",
                str(rng.randint(1, 6)),
                f"{rng.uniform(0.2, 9.5):.2f}",
                f"{lat}",
                f"{lon}",
                rng.choice(NOTES),
            ]
        )

    def pick(k: int) -> list[int]:
        return rng.sample(range(len(out)), k)

    c = {h: i for i, h in enumerate(HEADER)}
    for r in pick(40):
        out[r][c["email"]] = rng.choice(["N/A", "unknown", "none", "-"])
    for r in pick(15):
        out[r][c["email"]] = "   "
    for r in pick(25):
        out[r][c["birth_date"]] = "1900-01-01"
    for r in pick(10):
        out[r][c["customer_id"]] = f"{int(out[r][c['customer_id']]) * 10**9:.5E}".replace("E+", "E+")
    for r in pick(60):
        out[r][c["zip"]] = out[r][c["zip"]].lstrip("0") if out[r][c["zip"]].startswith("0") else out[r][c["zip"]]
    for r in pick(80):
        y, m, d = out[r][c["order_date"]].split("-")
        out[r][c["order_date"]] = f"{m}/{d}/{y}"
    for r in pick(5):
        out[r][c["order_date"]] = "2025-02-30"
    for r in pick(6):
        out[r][c["birth_date"]] = "2031-05-04"
    for r in pick(30):
        out[r][c["subtotal"]] = f"${float(out[r][c['subtotal']]):,.2f}"
    for r in pick(8):
        out[r][c["first_name"]] = "JosÃ©"
    for r in pick(12):
        out[r][c["last_name"]] = out[r][c["last_name"]] + "​"
    for r in pick(40):
        out[r][c["state"]] = " " + out[r][c["state"]]
    for r in pick(70):
        out[r][c["segment"]] = rng.choice(["retail", "RETAIL", "Retail "])
    for r in pick(4):
        out[r][c["segment"]] = "Retial"
    for r in pick(12):
        out[r][c["phone"]] = out[r][c["email"]]
    for r in pick(10):
        out[r][c["tax"]] = "see invoice"
    for r in pick(8):
        out[r][c["latitude"]], out[r][c["longitude"]] = str(float(out[r][c["longitude"]])), out[r][c["latitude"]]
    for r in pick(10):
        out[r][c["first_name"]] = rng.choice(["test", "asdf", "John Doe", "xxxx"])
    for r in pick(5):
        out[r][c["email"]] = "test@test.com"
    for r in pick(6):
        out[r][c["notes"]] = rng.choice(["<b>VIP</b> customer", "Traceback (most recent call last): KeyError"])
    for r in pick(25):
        a = out[r]
        a[c["ship_date"]] = (date.fromisoformat(a[c["signup_date"]]) - timedelta(days=3)).isoformat()
    for r in pick(30):
        out[r][c["total"]] = f"{float(out[r][c['total']]) + rng.choice([10, 25.5, -40]):.2f}"
    for r in pick(30):
        out[r][c["age"]] = str(int(out[r][c["age"]]) + rng.choice([7, -9, 15]))
    for r in pick(5):
        out[r][c["quantity"]] = str(-rng.randint(1, 5))
    out[rng.randrange(len(out))][c["age"]] = "230"
    out[rng.randrange(len(out))][c["subtotal"]] = "9999999.00"
    for r in pick(250):
        out[r][c["unit_weight_kg"]] = f"{float(out[r][c['unit_weight_kg']]) * 1000:.0f}"
    out[rng.randrange(len(out))][c["notes"]] = "Customer SSN 912-45-6789 for verification"
    out[rng.randrange(len(out))][c["notes"]] = "Paid with card 4111 1111 1111 1111"
    # a fake key, assembled at run time so no key-shaped string sits in the source
    fake_key = "_".join(
        [
            "sk",
            "live",
            "".join(rng.choice("abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24)),
        ]
    )
    out[rng.randrange(len(out))][c["notes"]] = f"api_key={fake_key}"
    for r in pick(8):
        out[r][c["customer_id"]] = out[(r + 1) % len(out)][c["customer_id"]]  # conflicting duplicate keys
    near = []
    for r in pick(10):
        twin = list(out[r])
        twin[c["customer_id"]] = str(900000 + r)
        twin[c["first_name"]] = twin[c["first_name"]][:-1] if len(twin[c["first_name"]]) > 4 else twin[c["first_name"]]
        twin[c["email"]] = twin[c["email"]].replace("@", "1@")
        near.append(twin)
    for r in pick(20):
        out.append(list(out[r]))  # exact duplicates
    out.extend(near)
    block_start = rng.randrange(0, len(out) - 60)
    out.extend([list(x) for x in out[block_start : block_start + 50]])  # a batch loaded twice
    insert_at = len(out) // 2
    out.insert(insert_at, list(HEADER))  # header repeated mid-file
    broken = list(out[10])
    broken[c["notes"]] = "Left at desk"
    broken.insert(c["last_name"], "Jr.")  # an unquoted extra field shifts the row
    out.insert(insert_at + 10, broken)
    total_row = ["TOTAL"] + [""] * (len(HEADER) - 1)
    total_row[c["total"]] = f"{sum(float(x[c['total']]) for x in out[:100] if x[0] != 'customer_id'):.2f}"
    out.append(total_row)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(out)
    return dict(PLANTED)
