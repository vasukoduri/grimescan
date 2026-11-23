"""Each check on a small dataset built to contain (and not contain) its problem."""

import random

import pytest

from grimescan import scan


def checks(report):
    return {i.check for i in report.issues}


def issue(report, check):
    return next(i for i in report.issues if i.check == check)


def base_rows(n=300, seed=1):
    rng = random.Random(seed)
    rows = []
    for k in range(n):
        rows.append(
            {
                "id": str(1000 + k),
                "name": rng.choice(["Ana Lee", "Bo Chen", "Cy Diaz", "Di Moss", "Ed Park"]) + f" {k}",
                "email": f"user{k}@example.com",
                "city": rng.choice(["Dover", "Newark", "Wilmington", "Lewes"]),
                "amount": f"{rng.uniform(10, 500):.2f}",
                "created": f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            }
        )
    return rows


def test_clean_data_scores_well():
    r = scan(base_rows())
    assert r.score >= 95, [(i.check, i.title) for i in r.issues]
    assert not [i for i in r.issues if i.severity in ("high", "medium")]


def test_disguised_nulls_and_whitespace():
    rows = base_rows()
    for k in range(0, 40, 2):
        rows[k]["email"] = "N/A"
    rows[1]["city"] = "   "
    r = scan(rows)
    assert {"disguised_nulls", "whitespace_only"} <= checks(r)
    assert issue(r, "disguised_nulls").count == 20


def test_numeric_sentinels():
    rows = base_rows(500)
    for k in range(0, 100, 5):
        rows[k]["amount"] = "-999"
    r = scan(rows)
    assert any(e.value == "-999" for e in issue(r, "disguised_nulls").examples)


def test_type_mismatch_describes_the_value():
    rows = base_rows()
    rows[5]["email"] = "(302) 555-0100"
    rows[6]["amount"] = "pending"
    r = scan(rows)
    notes = [e.note for i in r.issues if i.check == "type_mismatch" for e in i.examples]
    assert any("phone number in a column of email addresses" in n for n in notes)
    assert any("column of numbers" in n for n in notes)


def test_date_problems():
    rows = base_rows()
    rows[3]["created"] = "2025-02-30"
    for k in range(10, 30):
        y, m, d = rows[k]["created"].split("-")
        rows[k]["created"] = f"{m}/{d}/{y}"
    titles = [i.title for i in scan(rows).issues if i.check == "date_problems"]
    assert any("Impossible dates" in t for t in titles) and any("Mixed date formats" in t for t in titles)


def test_spreadsheet_damage():
    rows = base_rows()
    for row in rows:
        row["zip"] = random.Random(row["id"]).choice(["07030", "02108", "19801", "08540"])
    rows[2]["zip"] = "7030"
    rows[4]["id"] = "1.23457E+15"
    titles = [i.title for i in scan(rows).issues]
    assert any("leading zeros" in t for t in titles) and any("scientific notation" in t for t in titles)


def test_encoding_and_hidden_characters():
    rows = base_rows()
    rows[1]["name"] = "JosÃ© Ruiz"
    rows[2]["name"] = "Ana​ Lee"
    rows[3]["city"] = "Dover "
    r = scan(rows)
    garbled = next(i for i in r.issues if "Garbled" in i.title)
    assert garbled.examples[0].suggestion == "José Ruiz"
    assert any("Invisible" in i.title for i in r.issues) and any("Untrimmed" in i.title for i in r.issues)


def test_inconsistent_categories():
    rows = base_rows()
    for k in range(0, 30, 3):
        rows[k]["city"] = "dover"
    rows[7]["city"] = "Wilmigton"
    r = scan(rows)
    fixes = {e.value: e.suggestion for e in issue(r, "inconsistent_categories").examples}
    assert fixes.get("dover") == "Dover" and fixes.get("Wilmigton") == "Wilmington"


def test_placeholders_and_markup():
    rows = base_rows()
    rows[1]["name"] = "test"
    rows[2]["name"] = "asdf"
    rows[3]["city"] = "<b>Dover</b>"
    rows[4]["city"] = "Traceback (most recent call last): KeyError"
    assert {"placeholder_values", "markup_and_code"} <= checks(scan(rows))


def test_duplicates_blocks_keys_and_conflicts():
    rows = base_rows(200)
    rows += [dict(r) for r in rows[10:40]]  # a batch loaded twice
    rows.append(dict(rows[0], amount="999.99"))  # same id, different values
    r = scan(rows)
    titles = {i.title for i in r.issues}
    assert "Exact duplicate rows" in titles and "A batch of rows was loaded twice" in titles
    assert issue(r, "duplicates").count >= 30
    assert any("Conflicting records" in t for t in titles)


def test_near_duplicates_with_different_ids():
    rows = base_rows(200)
    twin = dict(rows[5], id="9999", name=rows[5]["name"].replace("Lee", "Le"))
    rows.append(twin)
    r = scan(rows)
    nd = issue(r, "near_duplicates")
    assert nd.details["different_ids"] >= 1


def test_broken_header_and_total_rows(tmp_path):
    p = tmp_path / "x.csv"
    lines = ["id,name,amount"] + [f"{k},Name {k},{k}.50" for k in range(1, 60)]
    lines.insert(20, "id,name,amount")
    lines.insert(30, "77,Bad, Name,1.00")
    lines.append("TOTAL,,999.00")
    p.write_text("\n".join(lines) + "\n")
    r = scan(p)
    titles = {i.title for i in r.issues}
    assert {
        "Header row repeated inside the data",
        "Rows with the wrong number of fields",
        "Total or summary rows inside the data",
    } <= titles
    assert "type_mismatch" not in checks(r)  # broken rows don't spill into per-column noise


def test_shifted_rows():
    rows = []
    for k in range(200):
        rows.append(
            {
                "id": str(k),
                "email": f"u{k}@example.com",
                "phone": f"(302) 555-01{k % 100:02d}",
                "created": "2025-01-0" + str(k % 9 + 1),
                "amount": f"{k}.25",
            }
        )
    for k in (50, 51, 52):
        r = rows[k]
        rows[k] = {"id": r["id"], "email": "", "phone": r["email"], "created": r["phone"], "amount": r["created"]}
    assert any(i.title == "Shifted rows" for i in scan(rows).issues)


def test_swapped_lat_lon_and_columns():
    rng = random.Random(3)
    rows = [
        {
            "lat": f"{rng.uniform(38, 41):.4f}",
            "lon": f"{rng.uniform(-77, -74):.4f}",
            "email": f"u{k}@example.com",
            "phone": f"(302) 555-01{k % 100:02d}",
        }
        for k in range(300)
    ]
    rows[5]["lat"], rows[5]["lon"] = rows[5]["lon"], rows[5]["lat"]
    rows[6]["email"], rows[6]["phone"] = rows[6]["phone"], rows[6]["email"]
    titles = {i.title for i in scan(rows).issues if i.check == "swapped_columns"}
    assert "Latitude and longitude swapped" in titles and any("swapped in some rows" in t for t in titles)


def test_relationships():
    rng = random.Random(2)
    rows = []
    for k in range(300):
        sub = round(rng.uniform(10, 300), 2)
        tax = round(sub * 0.08, 2)
        rows.append(
            {
                "start_date": "2025-03-01",
                "end_date": "2025-03-10",
                "subtotal": f"{sub:.2f}",
                "tax": f"{tax:.2f}",
                "total": f"{sub + tax:.2f}",
                "birth_date": "1980-06-15",
                "age": str(46 if k % 2 else 46),
            }
        )
    rows[4]["end_date"] = "2025-02-01"
    rows[7]["total"] = "1.00"
    rows[9]["age"] = "20"
    r = scan(rows, reference_date=__import__("datetime").date(2026, 11, 23))
    assert {"date_order", "total_mismatch", "age_vs_birth_date"} <= checks(r)
    assert "total = subtotal + tax" in issue(r, "total_mismatch").details["formula"]


def test_missing_patterns():
    rows = base_rows(400)
    for k, row in enumerate(rows):
        row["region"] = ["north", "south", "east", "west"][k % 4]
        row["phone"] = "" if row["region"] == "west" else f"(302) 555-01{k % 100:02d}"
        if k % 7 == 0:
            row["street"] = row["zip"] = ""
        else:
            row["street"], row["zip"] = f"{k} Main Rd", "19801"
    r = scan(rows)
    assert "missing_by_group" in checks(r)
    together = issue(r, "missing_together")
    assert {"street", "zip"} <= set(together.columns)


def test_plausibility():
    rng = random.Random(5)
    rows = [
        {
            "age": str(rng.randint(18, 80)),
            "quantity": str(rng.randint(1, 9)),
            "weight_kg": f"{rng.uniform(1, 9):.2f}",
            "price": f"{rng.uniform(5, 50):.2f}",
        }
        for _ in range(600)
    ]
    rows[1]["age"] = "230"
    rows[2]["quantity"] = "-3"
    for k in range(10, 60):
        rows[k]["weight_kg"] = f"{float(rows[k]['weight_kg']) * 1000:.0f}"
    rows[70]["price"] = "99999"
    r = scan(rows)
    assert {"impossible_values", "unit_mixup", "outliers"} <= checks(r)
    assert all(i.columns != ["weight_kg"] for i in r.issues if i.check == "outliers")  # reported as unit mix-up


def test_benford_and_round_numbers():
    rng = random.Random(9)
    natural = [{"amount": f"{10 ** rng.uniform(0, 5):.2f}"} for _ in range(3000)]
    assert "benford" not in checks(scan(natural))
    made_up = [
        {
            "amount": f"{rng.choice([5, 6, 7, 8, 9]) * 10 ** rng.randint(2, 4):.2f}"
            if k % 3
            else f"{rng.uniform(500, 9000):.2f}"
        }
        for k in range(3000)
    ]
    r = scan(made_up)
    assert "benford" in checks(r) and "round_number_heaping" in checks(r)


def test_sensitive_values_are_masked():
    rows = base_rows()
    rows[3]["city"] = "Customer said SSN is 912-45-6789"
    rows[4]["city"] = "token=ghp_" + "a" * 36
    rows[5]["amount"] = "4111111111111111"
    r = scan(rows)
    assert {"pii_in_free_text", "secrets", "unmasked_card_numbers"} <= checks(r)
    blob = r.to_json() + r.to_html() + r.to_markdown() + r.to_fix_list()
    assert "912-45-6789" not in blob and "4111111111111111" not in blob and "a" * 36 not in blob


def test_include_and_exclude():
    rows = base_rows()
    rows[1]["email"] = "N/A"
    assert checks(scan(rows, include=["missing"])) <= {
        "disguised_nulls",
        "whitespace_only",
        "high_missing_rate",
        "mostly_empty_rows",
        "missing_together",
        "missing_by_group",
        "default_value_inflation",
    }
    assert "disguised_nulls" not in checks(scan(rows, exclude=["disguised_nulls"]))


@pytest.mark.parametrize("bad", [[], [{}]])
def test_degenerate_inputs(bad):
    r = scan(bad)
    assert r.n_rows <= 1 and r.score >= 0


def test_no_values_in_report_with_zero_examples():
    rows = base_rows()
    rows[1]["email"] = "N/A"
    r = scan(rows, max_examples=0)
    assert r.issues and not any(i.examples for i in r.issues)
    assert "Dover" not in r.to_json() and "Dover" not in r.to_html()


def test_report_html_escapes_values():
    rows = base_rows()
    rows[2]["city"] = "<script>alert(1)</script>"
    html = scan(rows).to_html()
    assert "<script>alert" not in html and "&lt;script&gt;" in html
