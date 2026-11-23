# Contributing to grimescan

Thanks for helping make messy data easier to trust.

## Development setup

```bash
git clone https://github.com/vasukoduri/grimescan && cd grimescan
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest
ruff check src tests && ruff format src tests
```

## Adding a check

A check is a class in `src/grimescan/checks/` that subclasses `Check` (see `model.py`):

```python
class MyCheck(Check):
    id = "my_check"                  # stable id, used by --only / --skip
    category = "plausibility"         # one of model.CATEGORIES
    title = "Short name"
    description = "One or two sentences: what it finds and why it matters."
    needs_rows = False                # True if it needs whole rows, not single values

    def prepare(self, ctx):           # after pass 1: read ctx.profiles, decide which columns to check
        super().prepare(ctx)
        self.columns = [...]          # column indexes this check wants values for
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):    # pass 2: one call per non-null value (v is stripped)
        ...

    def finish(self):                 # return a list of Issues, built with self.issue(...)
        ...
```

Add it to the module's `CHECKS` list. Then:

1. Write a test in `tests/test_checks.py` with a small dataset that **contains** the problem and assert it's found, and make sure `test_clean_data_scores_well` still passes, so the check stays quiet on clean data.
2. If it's a problem people really have, plant it in `demo.py` and add it to `PLANTED`.
3. Check speed on a large file: `grimescan demo --rows 50000`. Checks run on every value, so keep `value()` cheap and do the expensive work in `prepare()` or `finish()`.

## Other good contributions

- **Type recognizers** in `semantics.py`: national IDs, postcodes and phone formats beyond the US/EU ones.
- **Sources** in `sources.py`: Excel, Avro, database cursors.
- **False positives**: if grimescan flags something on real data that isn't a problem, an issue with a small anonymized sample is very valuable.

## Ground rules

- **No real personal data** in tests, issues or examples. Use the demo generator or synthetic values.
- Keep the core dependency light: `numpy` only. Optional formats go in extras.
- Never print a sensitive value in clear: run it through `mask()`.

## Releasing

Releases are published by `.github/workflows/release.yml`, using PyPI trusted publishing (no tokens are stored in the repo).

1. Update `version` in `pyproject.toml` and `__version__` in `src/grimescan/__init__.py`, and add a `## X.Y.Z` section to `CHANGELOG.md`.
2. Push to `main` and check that CI is green.
3. Either push a tag (`git tag vX.Y.Z && git push origin vX.Y.Z`), or open **Actions → Release → Run workflow** on `main`, which creates the `vX.Y.Z` tag from the package version.

The workflow refuses to publish if the tag and the version differ. It then runs the tests, builds the package, uploads it to https://pypi.org/project/grimescan/ and creates the GitHub release with that version's CHANGELOG section as the notes.
