"""Scan a pandas DataFrame, print the findings and save the reports.

pip install "grimescan[pandas]"
python examples/scan_dataframe.py
"""

import pandas as pd

import grimescan

df = pd.DataFrame(
    {
        "order_id": [1001, 1002, 1003, 1003, 1005, 1006],
        "email": ["ana@example.com", "N/A", "bo@example.com", "bo@example.com", "(302) 555-0100", "cy@example.com"],
        "order_date": ["2025-03-01", "2025-03-02", "2025-03-02", "2025-03-02", "2025-02-30", "2025-03-04"],
        "ship_date": ["2025-03-03", "2025-03-01", "2025-03-05", "2025-03-05", "2025-03-06", "2025-03-06"],
        "subtotal": [120.00, 80.50, 42.00, 42.00, 15.25, 300.00],
        "tax": [9.60, 6.44, 3.36, 3.36, 1.22, 24.00],
        "total": [129.60, 86.94, 45.36, 45.36, 16.47, 3240.00],
    }
)

report = grimescan.scan(df)
print(f"quality score {report.score} ({report.grade})")
for issue in report.issues:
    print(f"[{issue.severity}] {issue.title} ({issue.count})")
    for ex in issue.examples[:2]:
        print(f"    row {ex.row}: {ex.value!r}  {ex.note}")

paths = report.save("grimescan-report/orders")
print("open", paths["html"])
