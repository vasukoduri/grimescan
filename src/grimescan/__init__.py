"""grimescan: find the grime in your data.

Point it at a dataset and it reports null, duplicate, malformed and out-of-place values
without you writing any rules first::

    import grimescan
    report = grimescan.scan("customers.csv")
    print(report.score, report.grade)
    report.save("grimescan-report")
"""

__version__ = "0.1.0"

from .model import CATEGORIES, Example, Issue  # noqa: E402
from .profile import ScanOptions  # noqa: E402
from .report import Report  # noqa: E402
from .scanner import check_foreign_key, scan  # noqa: E402

__all__ = ["CATEGORIES", "Example", "Issue", "Report", "ScanOptions", "__version__", "check_foreign_key", "scan"]
