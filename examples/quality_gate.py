"""Stop a pipeline when a new extract is worse than an agreed bar.

python examples/quality_gate.py data/extract.csv
"""

import sys

import grimescan


def gate(path: str, min_score: float = 80, fail_on: str = "high") -> None:
    report = grimescan.scan(path, exclude=["benford", "round_number_heaping"])  # skip the "possibly invented" screens
    report.save("grimescan-report")
    ok, reasons = report.check(fail_under=min_score, fail_on=fail_on)
    if not ok:
        print(f"{path}: data quality gate failed")
        for reason in reasons:
            print("  -", reason)
        for issue in report.issues:
            if issue.severity == "high":
                print(f"  * {issue.title}: {issue.count:,} affected")
        sys.exit(1)
    print(f"{path}: score {report.score} ({report.grade}), gate passed")


if __name__ == "__main__":
    gate(sys.argv[1] if len(sys.argv) > 1 else "data.csv")
