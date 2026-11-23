"""Command line: ``grimescan scan | demo | checks | fk``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .checks import ALL_CHECKS
from .model import CATEGORIES
from .profile import ScanOptions
from .report import Report
from .scanner import check_foreign_key, scan

_ICON = {"high": "✖", "medium": "▲", "low": "•", "info": "·"}


def _print(rep: Report, paths: dict[str, Path], top: int = 12) -> None:
    s = rep.summary()
    sev = s["by_severity"]
    speed = f", {s['rows_per_second']:,} rows/s" if s["rows_per_second"] else ""
    print(
        f"\ngrimescan {__version__}  {rep.source}  ({rep.n_rows:,} rows × {rep.n_columns} columns, "
        f"{rep.seconds}s{speed})"
    )
    print(f"  quality score {rep.score}/100  grade {rep.grade}")
    print(
        f"  issues: {sev['high']} high, {sev['medium']} medium, {sev['low']} low   "
        f"missing cells {s['missing_cell_rate']:.1%}   duplicate rows {s['duplicate_rows']:,}"
    )
    for i in rep.issues[:top]:
        cnt = f"{i.count:,}" if i.count else ""
        print(f"  {_ICON[i.severity]} {i.severity:<6} {cnt:>8}  {i.title.replace('`', '')}")
    if len(rep.issues) > top:
        print(f"  ... and {len(rep.issues) - top} more in the report")
    for k, p in paths.items():
        print(f"  {k:<5} -> {p}")


def cmd_scan(args: argparse.Namespace) -> int:
    opts = ScanOptions(
        sample_size=args.sample_size,
        max_examples=args.max_examples,
        near_duplicate_limit=args.near_duplicate_limit,
        max_rows=args.max_rows,
        include=args.only,
        exclude=args.skip,
        key_columns=args.key,
    )
    worst = 0
    for path in args.paths:
        kwargs = {"delimiter": args.delimiter} if args.delimiter else {}
        rep = scan(path, opts, **kwargs)
        out = Path(args.out) / Path(path).stem if len(args.paths) > 1 else Path(args.out)
        paths = rep.save(out)
        if args.json:
            print(rep.to_json())
        elif not args.quiet:
            _print(rep, paths)
        ok, reasons = rep.check(args.fail_under, args.fail_on)
        if not ok:
            worst = 1
            if not args.quiet and not args.json:
                print("  GATE: FAIL\n    - " + "\n    - ".join(reasons))
    return worst


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import generate

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = out / "dirty_customers.csv"
    planted = generate(data, rows=args.rows, seed=args.seed)
    rep = scan(data)
    paths = rep.save(out)
    if not args.quiet:
        print(f"Generated {data} with {len(planted)} kinds of planted problems.")
        _print(rep, paths, top=10)
        found = {i.check for i in rep.issues}
        hit = [k for k in planted if k in found]
        print(f"\n  planted problems detected: {len(hit)}/{len(planted)}")
        for k, desc in planted.items():
            print(f"    {'✔' if k in found else '✖'} {k:<26} {desc}")
    return 0


def cmd_checks(args: argparse.Namespace) -> int:
    for cat, label in CATEGORIES.items():
        print(f"\n{label}")
        for c in ALL_CHECKS:
            if c.category == cat:
                print(f"  {c.id:<26} {c.description}")
    return 0


def cmd_fk(args: argparse.Namespace) -> int:
    child, child_col = args.child.rsplit(":", 1)
    parent, parent_col = args.parent.rsplit(":", 1)
    issue = check_foreign_key(child, child_col, parent, parent_col)
    if issue is None:
        print(f"OK: every {child_col} in {child} exists in {parent}.{parent_col}")
        return 0
    print(f"✖ {issue.title.replace('`', '')}: {issue.message.replace('`', '')}")
    for ex in issue.examples:
        print(f"    row {ex.row:,}: {ex.value}  ({ex.note.replace('`', '')})")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="grimescan", description="Find the grime in your data.")
    p.add_argument("--version", action="version", version=f"grimescan {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan one or more files (CSV, TSV, JSON Lines, Parquet)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out", default="grimescan-report", help="report directory")
    s.add_argument("--delimiter", help="CSV delimiter (default: detected)")
    s.add_argument("--max-rows", type=int, help="only scan the first N rows (quick look at a huge file)")
    s.add_argument("--sample-size", type=int, default=5000, help="values per column kept for type inference")
    s.add_argument("--max-examples", type=int, default=10, help="example rows kept per issue")
    s.add_argument("--near-duplicate-limit", type=int, default=20_000, help="rows checked for fuzzy duplicates")
    s.add_argument("--key", action="append", help="primary-key column (default: detected)")
    s.add_argument("--only", nargs="*", help="check ids or categories to run")
    s.add_argument("--skip", nargs="*", help="check ids or categories to skip")
    s.add_argument("--fail-under", type=float, help="exit 1 if the quality score is below this")
    s.add_argument("--fail-on", choices=["high", "medium", "low"], help="exit 1 on any issue this severe or worse")
    s.add_argument("--json", action="store_true", help="print the JSON report")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("demo", help="generate a dirty dataset with planted problems and scan it")
    d.add_argument("--rows", type=int, default=5000)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--out", default="grimescan-demo")
    d.add_argument("-q", "--quiet", action="store_true")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("checks", help="list every check")
    c.set_defaults(func=cmd_checks)

    f = sub.add_parser("fk", help="find child rows whose key has no parent row")
    f.add_argument("child", help="child file and column, e.g. orders.csv:customer_id")
    f.add_argument("parent", help="parent file and column, e.g. customers.csv:id")
    f.set_defaults(func=cmd_fk)

    args = p.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"grimescan: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
