# Changelog

## 0.1.0

First release.

- **35 rule-free checks in 7 categories**: missing and disguised values, format and encoding damage, values that don't belong, duplicates and conflicts, relationships between columns, plausibility and possible fabrication, and sensitive data in the wrong place.
- **Semantic type inference** for emails, dates, ZIP codes, phone numbers, amounts, percentages, names, IDs, UUIDs, IBANs, card numbers, URLs, IP addresses and state codes, using both values and column names.
- **Streaming two-pass scanner** with constant memory: HyperLogLog, reservoir sampling, Space-Saving top-k, Bloom filters and MinHash/LSH for near-duplicates.
- **Discovered relationships**: total formulas (`total = subtotal + tax - discount`), date pairs that must be in order, age vs date of birth, and foreign keys across tables with `check_foreign_key()`.
- **Sources**: CSV/TSV (delimiter sniffed), JSON Lines, Parquet (`pyarrow`), pandas and polars DataFrames, lists of dicts.
- **Reports**: self-contained HTML with a quality score, column profiles and a missing-value heatmap; JSON; Markdown; and a CSV fix list with suggested corrections. SSN- and card-shaped values are always masked.
- **CLI**: `grimescan scan`, `fk`, `checks` and `demo`, with `--fail-under` and `--fail-on` for CI quality gates.
- **Demo dataset** with 25 kinds of planted problems, all detected.
