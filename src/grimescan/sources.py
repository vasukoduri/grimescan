"""Data sources. Each source can be read twice (grimescan makes two streaming passes).

Every source yields ``(row_number, values, field_count)`` where ``values`` is a list of
strings or ``None`` (one per header column) and ``field_count`` is how many fields the
raw record actually had (only meaningful for CSV, where a mismatch means a broken row).
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

Row = tuple[int, list[str | None], int]

csv.field_size_limit(1 << 27)


def to_text(v: Any) -> str | None:
    """Normalize a typed value to the string form grimescan analyses."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return repr(v) if not v.is_integer() or abs(v) >= 1e16 else str(int(v))
    if isinstance(v, int):
        return str(v)
    try:
        if v != v:  # NaN-like values (pandas NaT, numpy nan, Decimal NaN)
            return None
    except Exception:
        pass
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    s = str(v)
    return None if s in ("NaT", "<NA>", "nan") and type(v).__name__ in ("NaTType", "NAType", "float") else s


class Source:
    name: str = "data"
    header: list[str]

    def rows(self) -> Iterator[Row]:  # pragma: no cover - interface
        raise NotImplementedError


class CSVSource(Source):
    """Streaming CSV/TSV reader. The delimiter is sniffed unless given."""

    def __init__(
        self, path: str | Path, delimiter: str | None = None, encoding: str = "utf-8-sig", has_header: bool = True
    ):
        self.path = Path(path)
        self.name = self.path.name
        self.encoding = encoding
        self.delimiter = delimiter or self._sniff()
        self.has_header = has_header
        with self._open() as f:
            first = next(csv.reader(f, delimiter=self.delimiter), [])
        self.header = _unique_names(first if has_header else [f"column_{i + 1}" for i in range(len(first))])

    def _open(self) -> io.TextIOWrapper:
        return open(self.path, encoding=self.encoding, errors="replace", newline="")

    def _sniff(self) -> str:
        if self.path.suffix.lower() in (".tsv", ".tab"):
            return "\t"
        with self._open() as f:
            sample = f.read(65536)
        lines = [ln for ln in sample.splitlines()[:50] if ln.strip()]
        if not lines:
            return ","
        best, best_score = ",", -1.0
        for d in ",;\t|":
            counts = [ln.count(d) for ln in lines]
            if not counts[0]:
                continue
            # the delimiter that splits most lines into as many fields as the header wins
            score = sum(1 for c in counts if c == counts[0]) / len(counts) + 0.01 * min(counts[0], 50)
            if score > best_score:
                best, best_score = d, score
        return best

    def rows(self) -> Iterator[Row]:
        n = len(self.header)
        with self._open() as f:
            reader = csv.reader(f, delimiter=self.delimiter)
            if self.has_header:
                next(reader, None)
            for i, rec in enumerate(reader, start=1):
                if not rec:
                    continue
                count = len(rec)
                vals: list[str | None] = [v if v != "" else None for v in rec[:n]]
                if count < n:
                    vals.extend([None] * (n - count))
                yield i, vals, count


class JSONLinesSource(Source):
    """JSON Lines: one object per line. Nested values are kept as JSON text."""

    def __init__(self, path: str | Path, header_scan_lines: int = 2000):
        self.path = Path(path)
        self.name = self.path.name
        keys: dict[str, None] = {}
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= header_scan_lines:
                    break
                obj = _loads(line)
                if isinstance(obj, dict):
                    keys.update(dict.fromkeys(obj))
        self.header = list(keys)

    def rows(self) -> Iterator[Row]:
        with open(self.path, encoding="utf-8", errors="replace") as f:
            i = 0
            for line in f:
                if not line.strip():
                    continue
                i += 1
                obj = _loads(line)
                if not isinstance(obj, dict):
                    yield i, [line.strip()] + [None] * (len(self.header) - 1), -1
                    continue
                yield i, [to_text(obj.get(k)) for k in self.header], len(obj)


class RecordsSource(Source):
    """A list of dicts (or a list of lists with a header)."""

    def __init__(self, records: list[Any], header: list[str] | None = None, name: str = "records"):
        self.records = records
        self.name = name
        if header is None:
            keys: dict[str, None] = {}
            for r in records[:2000]:
                if isinstance(r, dict):
                    keys.update(dict.fromkeys(r))
            header = list(keys)
        self.header = [str(h) for h in header]

    def rows(self) -> Iterator[Row]:
        for i, r in enumerate(self.records, start=1):
            if isinstance(r, dict):
                yield i, [to_text(r.get(k)) for k in self.header], len(r)
            else:
                vals = [to_text(v) for v in r][: len(self.header)]
                vals += [None] * (len(self.header) - len(vals))
                yield i, vals, len(r)


class PandasSource(Source):
    def __init__(self, df: Any, name: str = "dataframe", chunk_size: int = 50_000):
        self.df = df
        self.name = name
        self.chunk_size = chunk_size
        self.header = _unique_names([str(c) for c in df.columns])

    def rows(self) -> Iterator[Row]:
        n = len(self.header)
        i = 0
        for start in range(0, len(self.df), self.chunk_size):
            chunk = self.df.iloc[start : start + self.chunk_size]
            for rec in chunk.itertuples(index=False, name=None):
                i += 1
                yield i, [to_text(v) for v in rec], n


class PolarsSource(Source):
    def __init__(self, df: Any, name: str = "dataframe"):
        self.df = df
        self.name = name
        self.header = _unique_names([str(c) for c in df.columns])

    def rows(self) -> Iterator[Row]:
        n = len(self.header)
        for i, rec in enumerate(self.df.iter_rows(), start=1):
            yield i, [to_text(v) for v in rec], n


class ParquetSource(Source):
    """Streams record batches with pyarrow (``pip install "grimescan[parquet]"``)."""

    def __init__(self, path: str | Path, batch_size: int = 65_536):
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError('Parquet support needs pyarrow: pip install "grimescan[parquet]"') from exc
        self.path = Path(path)
        self.name = self.path.name
        self.batch_size = batch_size
        self._pq = pq
        self.header = _unique_names(list(pq.ParquetFile(self.path).schema_arrow.names))

    def rows(self) -> Iterator[Row]:
        pf = self._pq.ParquetFile(self.path)
        n = len(self.header)
        i = 0
        for batch in pf.iter_batches(batch_size=self.batch_size):
            cols = [batch.column(j).to_pylist() for j in range(batch.num_columns)]
            for r in range(batch.num_rows):
                i += 1
                yield i, [to_text(c[r]) for c in cols], n


def open_source(data: Any, **kwargs: Any) -> Source:
    """Build a :class:`Source` from a path, a DataFrame, or a list of records."""
    if isinstance(data, Source):
        return data
    if isinstance(data, (str, Path)):
        path = Path(data)
        suffix = "".join(path.suffixes[-1:]).lower()
        if suffix in (".jsonl", ".ndjson"):
            return JSONLinesSource(path)
        if suffix in (".parquet", ".pq"):
            return ParquetSource(path)
        return CSVSource(path, **kwargs)
    mod = type(data).__module__
    if mod.startswith("pandas"):
        return PandasSource(data)
    if mod.startswith("polars"):
        return PolarsSource(data)
    if isinstance(data, list):
        return RecordsSource(data, **kwargs)
    raise TypeError(f"unsupported data type {type(data).__name__}; pass a path, DataFrame or list of records")


def _loads(line: str) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, n in enumerate(names):
        n = (n or "").strip() or f"column_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 1
        out.append(n)
    return out
