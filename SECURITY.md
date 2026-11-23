# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository.

Include what you found, how to reproduce it, and the version. You'll get a reply within a week.

## What counts

- A sensitive value (SSN, card number, secret) written to a report in clear.
- A crafted input file that makes grimescan execute code, read files it wasn't given, or use unbounded memory.
- Report HTML that runs script from the scanned data (an XSS in `report.html`).

## Handling data

grimescan runs locally and makes no network calls. Reports quote example values from your data, so treat `report.html`, `report.json`, `report.md` and `fix_list.csv` with the same care as the data itself. SSN- and card-shaped values are masked automatically, but other personal data (names, emails, addresses) is shown as-is. Use `--max-examples 0` to keep values out of reports entirely.
