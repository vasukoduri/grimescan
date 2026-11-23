import csv
import io
import json

import pytest

from grimescan import check_foreign_key, scan
from grimescan.cli import main
from grimescan.demo import PLANTED, generate


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    p = tmp_path_factory.mktemp("demo") / "dirty.csv"
    generate(p, rows=3000)
    return p, scan(p)


def test_demo_detects_every_planted_problem(demo):
    _, r = demo
    assert set(PLANTED) <= {i.check for i in r.issues}
    assert r.grade == "F"


def test_report_outputs(demo, tmp_path):
    _, r = demo
    paths = r.save(tmp_path)
    data = json.loads(paths["json"].read_text())
    assert data["summary"]["rows"] == r.n_rows and data["issues"]
    html = paths["html"].read_text()
    assert html.startswith("<!DOCTYPE html>") and "Missing values across the file" in html and "<svg" in html
    assert "# grimescan report" in paths["md"].read_text()
    rows = list(csv.DictReader(io.StringIO(paths["fixes"].read_text())))
    assert rows and {"row", "column", "check", "severity", "value", "problem", "suggestion"} == set(rows[0])


def test_examples_for_row_level_issues_show_the_row(demo):
    _, r = demo
    dup = next(i for i in r.issues if i.title == "Exact duplicate rows")
    assert all(e.value and "customer_id=" in e.value for e in dup.examples)


def test_gate(demo):
    _, r = demo
    assert not r.check(fail_under=90)[0]
    assert not r.check(fail_on="high")[0]
    assert r.check(fail_under=0)[0]


def test_max_rows(demo):
    p, _ = demo
    assert scan(p, max_rows=100).n_rows == 100


def test_foreign_keys(tmp_path):
    parent = tmp_path / "customers.csv"
    child = tmp_path / "orders.csv"
    parent.write_text("id,name\n1,A\n2,B\n")
    child.write_text("order_id,customer_id\n10,1\n11,2\n12,3\n13,\n")
    issue = check_foreign_key(child, "customer_id", parent, "id")
    assert issue.count == 1 and issue.examples[0].value == "3"
    assert check_foreign_key(child, "customer_id", child, "customer_id") is None


def test_cli(tmp_path, demo, capsys):
    p, _ = demo
    assert main(["scan", str(p), "--out", str(tmp_path / "r"), "-q"]) == 0
    assert main(["scan", str(p), "--out", str(tmp_path / "r"), "-q", "--fail-on", "high"]) == 1
    assert main(["scan", str(p), "--json", "--max-rows", "50", "--out", str(tmp_path / "j")]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["rows"] == 50
    assert main(["checks"]) == 0
    assert main(["demo", "--rows", "800", "--out", str(tmp_path / "d"), "-q"]) == 0
    assert main(["scan", str(tmp_path / "missing.csv"), "-q"]) == 2
    parent, child = tmp_path / "p.csv", tmp_path / "c.csv"
    parent.write_text("id\n1\n")
    child.write_text("pid\n1\n2\n")
    assert main(["fk", f"{child}:pid", f"{parent}:id"]) == 1
