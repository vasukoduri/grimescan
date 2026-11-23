import json
import math

import pytest

from grimescan.semantics import (
    card_number_valid,
    classify,
    describe,
    fits,
    iban_valid,
    infer_column_type,
    parse_date,
)
from grimescan.sketches import BloomFilter, HyperLogLog, MinHasher, Reservoir, TopK
from grimescan.sources import (
    CSVSource,
    JSONLinesSource,
    RecordsSource,
    open_source,
    to_text,
)


# ------------------------------------------------------------------ sketches
@pytest.mark.parametrize("n", [10, 5_000, 100_000])
def test_hyperloglog_accuracy(n):
    h = HyperLogLog(12)
    for i in range(n):
        h.add(f"value-{i}")
    assert abs(h.count() - n) / n < 0.05


def test_reservoir_is_uniform_and_bounded():
    r = Reservoir(1000, seed=1)
    for i in range(100_000):
        r.add(i)
    assert len(r.items) == 1000
    mean = sum(r.items) / len(r.items)
    assert 40_000 < mean < 60_000  # a uniform sample of 0..99,999 has mean ~50,000


def test_topk_heavy_hitters():
    t = TopK(10)
    for i in range(1000):
        t.add("hot")
        t.add(f"cold-{i}")
    assert t.most_common(1)[0][0] == "hot"


def test_bloom_filter():
    b = BloomFilter(10_000)
    assert not b.add_and_check(42)
    assert b.add_and_check(42)


def test_minhash_similarity():
    m = MinHasher()
    a = m.signature(m.shingles("John Smith, 12 Oak Street, Wilmington"))
    b = m.signature(m.shingles("Jon Smith, 12 Oak Street, Wilmington"))
    c = m.signature(m.shingles("Completely different text about trains"))
    assert MinHasher.similarity(a, b) > 0.6 > MinHasher.similarity(a, c)


# ------------------------------------------------------------------ semantics
@pytest.mark.parametrize(
    "value,label",
    [
        ("alex@example.com", "email"),
        ("4111 1111 1111 1111", "card_number"),
        ("2025-01-31", "date"),
        ("03/04/2025", "date"),
        ("$1,200.50", "currency_amount"),
        ("12%", "percent"),
        ("5 kg", "number_with_unit"),
        ("1.23457E+15", "scientific"),
        ("07030", "us_zip"),
        ("(302) 555-0100", "phone"),
        ("John Smith", "person_name"),
        ("GB82 WEST 1234 5698 7654 32", "iban"),
        ("550e8400-e29b-41d4-a716-446655440000", "uuid"),
    ],
)
def test_classify(value, label):
    assert label in classify(value)


def test_dates():
    assert parse_date("2025-02-30").value is None  # impossible
    assert parse_date("03/04/2025").ambiguous
    assert "day first" in parse_date("13/04/2025").fmt
    assert parse_date("2025-01-01T00:00:00Z").midnight
    assert parse_date("hello") is None


def test_validators():
    assert card_number_valid("4111111111111111") and not card_number_valid("4111111111111112")
    assert not card_number_valid("1234567812345670")  # Luhn-valid but no card network
    assert iban_valid("GB82WEST12345698765432") and not iban_valid("GB82WEST12345698765433")


def test_infer_column_type_uses_name_hints():
    assert infer_column_type("zip", ["19801", "07030", "10001", "60601"]).label == "us_zip"
    assert infer_column_type("amount", ["19801", "17030", "10001", "60601"]).label == "integer"
    assert infer_column_type("email", ["a@b.co", "c@d.org", "e@f.net", "x"]).label == "email"
    assert infer_column_type("notes", ["hello there", "ok", "call back", "fine"]).label in ("text", "word")


def test_fits_and_describe():
    assert fits("email", "a@b.co") and not fits("email", "John")
    assert describe("alex@example.com") == "an email address"
    assert describe("(302) 555-0100") == "a phone number"


# ------------------------------------------------------------------ sources
def test_csv_source_sniffs_and_reports_field_counts(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a;b;c\n1;2;3\n4;5\n6;7;8;9\n", encoding="utf-8")
    src = CSVSource(p)
    assert src.delimiter == ";" and src.header == ["a", "b", "c"]
    rows = list(src.rows())
    assert [r[2] for r in rows] == [3, 2, 4]
    assert rows[1][1] == ["4", "5", None]


def test_jsonl_source(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1, "b": {"x": 1}}\n{"a": null, "c": true}\nnot json\n', encoding="utf-8")
    src = JSONLinesSource(p)
    rows = list(src.rows())
    assert src.header == ["a", "b", "c"]
    assert rows[0][1] == ["1", '{"x": 1}', None] and rows[1][1] == [None, None, "true"]
    assert rows[2][2] == -1


def test_records_and_to_text():
    src = RecordsSource([{"a": 1.0, "b": float("nan")}, {"a": 2.5}])
    assert [r[1] for r in src.rows()] == [["1", None], ["2.5", None]]
    assert to_text(True) == "true" and to_text(None) is None and math.isnan(float("nan"))


def test_open_source_dispatch(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a\n1\n")
    assert isinstance(open_source(p), CSVSource)
    assert isinstance(open_source([{"a": 1}]), RecordsSource)
    with pytest.raises(TypeError):
        open_source(42)


def test_pandas_source():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": [1, None], "when": pd.to_datetime(["2025-01-01", None])})
    src = open_source(df)
    rows = [r[1] for r in src.rows()]
    assert rows[0][0] == "1" and rows[1] == [None, None]
    assert rows[0][1].startswith("2025-01-01")


def test_parquet_source(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    p = tmp_path / "x.parquet"
    pq.write_table(pa.table({"a": [1, 2, None], "b": ["x", None, "z"]}), p)
    rows = [r[1] for r in open_source(p).rows()]
    assert rows == [["1", "x"], ["2", None], [None, "z"]]


def test_json_roundtrip_of_nested():
    assert json.loads(to_text({"k": [1, 2]})) == {"k": [1, 2]}
