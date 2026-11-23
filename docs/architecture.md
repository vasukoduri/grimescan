# How grimescan works

grimescan reads the data up to three times, always streaming, and never holds the whole file in memory.

```
source ──▶ pass 1: profile ──▶ learn ──▶ pass 2: check ──▶ pass 3: fetch examples ──▶ report
```

## Sources (`sources.py`)

Every input becomes a stream of `(row_number, values, field_count)` where `values` is a list of strings (or `None`) aligned with the header. CSV delimiters are detected from the first lines; `field_count` keeps the true number of fields so broken rows can be found. JSON Lines, Parquet (in record batches), pandas, polars and lists of dicts are normalized to the same text form, so every check works the same on every format.

## Pass 1: profile (`profile.py`)

For each column, in a single pass:

| Sketch | Gives | Memory |
|---|---|---|
| HyperLogLog (p=12) | distinct count, ±2% | 4 KB |
| Reservoir sample (Algorithm L) | a uniform sample of values (default 5,000) | fixed |
| Space-Saving top-k | the most frequent values, exact while the column has few distinct values | fixed |
| Running (Welford) stats | mean, variance, min, max of numeric values | constant |
| Length histogram | value lengths, for truncation | bounded |
| Missing timeline | missing rate per chunk of rows, for the heatmap and "missing from row N" patterns | fixed |

A dataset-wide reservoir of whole rows (2,000) is kept for checks that need relationships between columns.

### Learning what "normal" is

From the samples, before pass 2:

- **Column types** (`semantics.py`): each sampled value is classified into labels (email, date, us_zip, phone, integer, decimal, currency_amount, person_name, ...). The most specific label that at least 80% of values fit wins. Column names act as hints: five-digit numbers in a column called `zip` are ZIP codes, not integers.
- **Keys**: a column named like an ID that is almost unique.
- **Total formulas**: for each column named like a total, grimescan tries sums of other numeric columns with + and − signs on the row sample and keeps the formula that holds for nearly all rows.
- **Date pairs**: columns whose names say they must be ordered (start/end, order/ship, created/updated, birth/death).
- **Groups**: low-cardinality columns to test whether missing values cluster in one category.
- **Distributions**: robust centre and spread (median, MAD), clusters for unit mix-ups, leading-digit counts for Benford.

## Pass 2: check (`scanner.py`, `checks/`)

Each check declares which columns it wants in `prepare()`, gets every non-null value of those columns in `value()`, and, if `needs_rows` is set, every whole row in `row()`. The scanner dispatches values column by column so a check costs nothing for columns it doesn't watch.

Structural rows (a repeated header, a row with the wrong number of fields, a `TOTAL` line) are identified first. They are reported once and skipped by the value checks, so one broken line doesn't become twenty type mismatches.

A per-row counter of values that don't fit their column type lets the row-level checks for shifted and swapped rows run only on rows that already look wrong.

### Duplicates

Each row is reduced to 64-bit values stored in compact `array('Q')`s: the row number, an exact hash, a hash normalized for case and spacing, and the key's hash, at most 32 bytes per row. After the pass numpy groups equal hashes to find duplicate rows, runs of consecutive duplicates (a batch loaded twice) and duplicate keys, and compares the row hashes inside each key group to find conflicting records.

### Near-duplicates

Rows are turned into character 3-gram shingles and a 64-permutation MinHash signature. Locality-sensitive hashing (8 bands of 8) puts similar rows in the same buckets. Candidate pairs from buckets are verified in bulk with numpy: they must have at least 80% estimated similarity **and** differ in no more than a quarter of their fields. Buckets that overflow are ignored, because they reflect shared structure (the same city, the same status) rather than duplicates.

## Pass 3: examples

Checks record row numbers. For issues about whole rows (duplicates, broken rows), the scanner re-reads only those rows and summarizes them, masking anything shaped like an SSN or card number.

## Overlaps and scoring

When two checks explain the same values the more specific one wins: a cluster 1,000× larger is a unit mix-up, so it isn't also reported as outliers.

Each column starts at 100. Every issue on the column multiplies its score by `1 − weight × impact`, where the weight depends on severity (high 1.0, medium 0.5, low 0.2) and the impact grows with the share of affected values (`min(1, 0.05 + √rate)`), so even a few bad values cost something but a column is never punished twice for the same rows. The dataset score is the average column score, reduced in the same way by dataset-wide issues like duplicate rows.
