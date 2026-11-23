"""The scan: profile pass, check pass, an optional example-fetch pass, then scoring."""

from __future__ import annotations

import dataclasses
import math
import re
import time
from typing import Any

from .checks import select_checks
from .checks.out_of_place import structural_row
from .checks.relationships import orphan_keys
from .checks.sensitive import mask
from .model import SEVERITY_WEIGHT, Issue
from .profile import ScanContext, ScanOptions, profile_pass
from .report import Report
from .semantics import card_number_valid, digits_only, to_float
from .sources import Source, open_source

_SENSITIVE_SHAPES = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])|(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_OPTION_FIELDS = {f.name for f in dataclasses.fields(ScanOptions)}


def scan(data: Any, options: ScanOptions | None = None, **kwargs: Any) -> Report:
    """Scan a dataset and return a :class:`~grimescan.report.Report`.

    ``data`` can be a file path (CSV, TSV, JSON Lines, Parquet), a pandas or Polars DataFrame,
    or a list of dicts. Keyword arguments set :class:`ScanOptions` fields (``sample_size``,
    ``max_examples``, ``include``, ``exclude``, ``key_columns``...) or are passed to the reader
    (for example ``delimiter=";"``).
    """
    opt_kwargs = {k: v for k, v in kwargs.items() if k in _OPTION_FIELDS}
    src_kwargs = {k: v for k, v in kwargs.items() if k not in _OPTION_FIELDS}
    options = dataclasses.replace(options, **opt_kwargs) if options else ScanOptions(**opt_kwargs)
    source = open_source(data, **src_kwargs)
    t0 = time.perf_counter()
    ctx = profile_pass(source, options)
    checks = select_checks(options.include, options.exclude)
    for c in checks:
        c.prepare(ctx)
    dispatch: list[list[Any]] = [[] for _ in ctx.header]
    for c in checks:
        for j in c.columns:
            dispatch[j].append(c.value)
    row_checks = [c.row for c in checks if c.needs_rows]
    structural_checks = [c.row for c in checks if c.needs_rows and getattr(c, "sees_structural_rows", False)]
    names = [h.strip().lower() for h in ctx.header]
    n_seen = 0
    for i, values, field_count in source.rows():
        if options.max_rows is not None and n_seen >= options.max_rows:
            break
        n_seen += 1
        if structural_row(values, names, field_count):
            for f in structural_checks:
                f(i, values, field_count)
            continue
        ctx.row_mismatches = 0
        for j, raw in enumerate(values):
            if raw is None:
                continue
            fns = dispatch[j]
            if fns:
                v = raw.strip()
                for f in fns:
                    f(j, raw, v, i)
        for f in row_checks:
            f(i, values, field_count)
    issues: list[Issue] = []
    for c in checks:
        issues.extend(c.finish())
    issues = _suppress_overlaps(issues)
    _fill_examples(source, ctx, issues)
    _mask_examples(issues)
    issues.sort(key=lambda x: (-SEVERITY_WEIGHT[x.severity], -x.count))
    return _build_report(ctx, issues, [c.id for c in checks], round(time.perf_counter() - t0, 3))


def check_foreign_key(
    child: Any, child_column: str, parent: Any, parent_column: str, max_examples: int = 10
) -> Issue | None:
    """Stream both tables once and report child rows whose key has no parent row."""
    psrc, csrc = open_source(parent), open_source(child)
    if parent_column not in psrc.header or child_column not in csrc.header:
        raise ValueError(f"column not found: {parent_column!r} in {psrc.name} or {child_column!r} in {csrc.name}")
    pj, cj = psrc.header.index(parent_column), csrc.header.index(child_column)
    keys = {v.strip() for _, vals, _ in psrc.rows() if (v := vals[pj]) and v.strip()}
    values = []
    n = 0
    for i, vals, _ in csrc.rows():
        n += 1
        v = vals[cj]
        if v and v.strip():
            values.append((i, v.strip()))
    return orphan_keys(csrc.name, child_column, values, psrc.name, parent_column, keys, max_examples, n)


#: an issue of the key type is dropped when one of the listed, more specific checks found a problem
#: in the same column (for example grams-vs-kilograms values would otherwise also show up as outliers)
_MORE_SPECIFIC = {"outliers": ("unit_mixup", "impossible_values", "spreadsheet_damage", "swapped_columns")}


def _suppress_overlaps(issues: list[Issue]) -> list[Issue]:
    by_col: dict[str, set[str]] = {}
    for x in issues:
        for col in x.columns:
            by_col.setdefault(col, set()).add(x.check)
    keep = []
    for x in issues:
        specific = _MORE_SPECIFIC.get(x.check, ())
        if x.columns and any(s in by_col.get(col, set()) for col in x.columns for s in specific):
            continue
        keep.append(x)
    return keep


def _summarize(header: list[str], values: list[str | None]) -> str:
    parts = []
    for name, v in zip(header, values, strict=False):
        if v is None or not v.strip():
            continue
        s = _SENSITIVE_SHAPES.sub(lambda m: mask(m.group(0)), v.strip())
        parts.append(f"{name}={s[:40]}")
        if len(parts) == 6:
            break
    return "; ".join(parts) if parts else "(empty row)"


def _mask_shapes(v: str | None) -> str | None:
    return _SENSITIVE_SHAPES.sub(lambda m: mask(m.group(0)), v) if v else v


def _mask_examples(issues: list[Issue]) -> None:
    """SSN- and card-shaped values never leave the scan unmasked, whichever check found them."""
    for issue in issues:
        for ex in issue.examples:
            ex.value, ex.suggestion = _mask_shapes(ex.value), _mask_shapes(ex.suggestion)


def _fill_examples(source: Source, ctx: ScanContext, issues: list[Issue]) -> None:
    wanted: dict[int, list] = {}
    for issue in issues:
        if issue.details.pop("_fetch", False):
            for ex in issue.examples:
                if ex.row is not None and ex.value is None:
                    wanted.setdefault(ex.row, []).append(ex)
    if not wanted:
        return
    remaining = len(wanted)
    for i, values, _ in source.rows():
        if i in wanted:
            text = _summarize(ctx.header, values)
            for ex in wanted[i]:
                ex.value = text
            remaining -= 1
            if not remaining:
                break


def _impact(rate: float) -> float:
    return min(1.0, 0.05 + math.sqrt(max(0.0, rate)))


def _build_report(ctx: ScanContext, issues: list[Issue], check_ids: list[str], seconds: float) -> Report:
    col_scores = {}
    for p in ctx.profiles:
        score = 1.0
        for issue in issues:
            if p.name in issue.columns:
                score *= 1 - SEVERITY_WEIGHT[issue.severity] * _impact(issue.rate)
        col_scores[p.name] = round(100 * score, 1)
    dataset_factor = 1.0
    for issue in issues:
        if not issue.columns:
            dataset_factor *= 1 - SEVERITY_WEIGHT[issue.severity] * _impact(issue.rate)
    base = sum(col_scores.values()) / len(col_scores) if col_scores else 100.0
    score = round(base * dataset_factor, 1)
    columns = []
    for p in ctx.profiles:
        info = p.to_dict(ctx.n_rows)
        info["score"] = col_scores[p.name]
        info["issues"] = sum(1 for x in issues if p.name in x.columns)
        info["histogram"] = _histogram(p) if p.ctype.label in ("integer", "decimal") else None
        sensitive = any(x.category == "sensitive" and p.name in x.columns for x in issues)
        # values seen once in a column holding sensitive data are left out; repeated ones are kept, shape-masked
        info["top_values"] = [[_mask_shapes(v), n] for v, n in info["top_values"] if n > 1 or not sensitive]
        if ctx.options.max_examples == 0:
            info["top_values"] = []  # no values from the data in the report at all
        columns.append(info)
    missing_cells = sum(p.missing for p in ctx.profiles)
    return Report(
        source=ctx.source_name,
        n_rows=ctx.n_rows,
        n_columns=len(ctx.header),
        columns=columns,
        issues=issues,
        score=score,
        missing_cells=missing_cells,
        timeline=ctx.timeline.rates() if ctx.timeline else [],
        timeline_chunk=ctx.timeline.chunk_size if ctx.timeline else 0,
        checks_run=check_ids,
        seconds=seconds,
        profile_seconds=ctx.profile_seconds,
    )


def _histogram(p: Any, bins: int = 24) -> dict | None:
    nums = sorted(x for x in (to_float(v) for v in p.sample.items) if x is not None and math.isfinite(x))
    if len(nums) < 10:
        return None
    lo, hi = nums[int(len(nums) * 0.01)], nums[int(len(nums) * 0.99) - 1]
    if hi <= lo:
        return {"lo": lo, "hi": hi, "counts": [len(nums)]}
    width = (hi - lo) / bins
    counts = [0] * bins
    for x in nums:
        if lo <= x <= hi:
            counts[min(bins - 1, int((x - lo) / width))] += 1
    return {"lo": lo, "hi": hi, "counts": counts}


_ = (card_number_valid, digits_only)
