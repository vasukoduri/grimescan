"""Sensitive data where it shouldn't be. Example values are always masked in reports."""

from __future__ import annotations

import re

from ..model import Check
from ..semantics import card_number_valid, digits_only, iban_valid, ssn_plausible

SSN_IN_TEXT = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
CARD_IN_TEXT = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
IBAN_IN_TEXT = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

SECRETS = [
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe secret key", re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI-style API key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}")),
    ("private key", re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("password in a connection string", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@[\w.-]+", re.I)),
    (
        "password assignment",
        re.compile(r"\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?[^\s'\"]{6,}", re.I),
    ),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.I)),
]


def mask(value: str) -> str:
    """Keep the shape and the last four characters; hide the rest."""
    v = value.strip()
    if len(v) <= 4:
        return "****"
    keep = v[-4:]
    return re.sub(r"[A-Za-z0-9]", "•", v[:-4]) + keep


class PIIInText(Check):
    id = "pii_in_free_text"
    category = "sensitive"
    title = "Personal data hidden in free-text columns"
    description = (
        "SSNs, card numbers and IBANs typed into notes, comments and description fields, where nobody "
        "expects them, so they escape masking and access controls."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.ctype.label in ("text", "word") and p.non_null]
        self.c = {j: self.collector() for j in self.columns}
        self.kinds: dict[int, dict[str, int]] = {j: {} for j in self.columns}

    def value(self, j, raw, v, i):
        if len(v) < 9 or sum(ch.isdigit() for ch in v) < 9:
            return
        found = None
        for m in SSN_IN_TEXT.finditer(v):
            if ssn_plausible(m.group(0)):
                found = ("SSN", m.group(0))
                break
        if not found:
            for m in CARD_IN_TEXT.finditer(v):
                if card_number_valid(m.group(0)):
                    found = ("card number", m.group(0))
                    break
        if not found:
            for m in IBAN_IN_TEXT.finditer(v):
                if iban_valid(m.group(0)):
                    found = ("IBAN", m.group(0))
                    break
        if found:
            kind, val = found
            k = self.kinds[j]
            k[kind] = k.get(kind, 0) + 1
            self.c[j].add(
                i,
                self.ctx.header[j],
                v.replace(val, mask(val))[:120],
                f"{kind} inside free text",
                "remove or mask the identifier",
            )

    def finish(self):
        out = []
        for j, c in self.c.items():
            if c.count:
                kinds = ", ".join(f"{n:,} {k}" for k, n in self.kinds[j].items())
                out.append(
                    self.issue(
                        "high",
                        f"Personal identifiers in `{self.ctx.header[j]}`",
                        f"{c.count:,} free-text value(s) in `{self.ctx.header[j]}` contain {kinds}.",
                        [self.ctx.header[j]],
                        c,
                        denominator=self.ctx.profiles[j].non_null,
                        sensitive=True,
                    )
                )
        return out


class Secrets(Check):
    id = "secrets"
    category = "sensitive"
    title = "Passwords, API keys and tokens"
    description = (
        "Credentials that leaked into the data: cloud and API keys, tokens, private keys, and passwords in "
        "connection strings or key=value text."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.non_null]
        self.c = {j: self.collector() for j in self.columns}
        self.kinds: dict[int, dict[str, int]] = {j: {} for j in self.columns}

    def value(self, j, raw, v, i):
        if len(v) < 16:
            return
        for kind, pat in SECRETS:
            m = pat.search(v)
            if m:
                k = self.kinds[j]
                k[kind] = k.get(kind, 0) + 1
                self.c[j].add(
                    i,
                    self.ctx.header[j],
                    v.replace(m.group(0), mask(m.group(0)))[:120],
                    kind,
                    "revoke the credential and remove it from the data",
                )
                return

    def finish(self):
        out = []
        for j, c in self.c.items():
            if c.count:
                kinds = ", ".join(f"{n:,} {k}" for k, n in self.kinds[j].items())
                out.append(
                    self.issue(
                        "high",
                        f"Secrets in `{self.ctx.header[j]}`",
                        f"{c.count:,} value(s) in `{self.ctx.header[j]}` look like credentials ({kinds}). "
                        "Treat them as compromised.",
                        [self.ctx.header[j]],
                        c,
                        sensitive=True,
                    )
                )
        return out


class UnmaskedCards(Check):
    id = "unmasked_card_numbers"
    category = "sensitive"
    title = "Unmasked card numbers"
    description = (
        "Full payment card numbers (Luhn-valid, with a real card-network prefix) stored in clear. PCI DSS "
        "requires stored card numbers to be unreadable, for example masked or tokenized."
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        self.columns = [p.index for p in ctx.profiles if p.ctype.label not in ("text", "word") and p.non_null]
        self.c = {j: self.collector() for j in self.columns}

    def value(self, j, raw, v, i):
        if not 13 <= len(v) <= 23:
            return
        d = digits_only(v)
        if 13 <= len(d) <= 19 and len(d) >= len(v) - 4 and card_number_valid(d):
            self.c[j].add(i, self.ctx.header[j], mask(v), "full card number", "mask or tokenize")

    def finish(self):
        return [
            self.issue(
                "high",
                f"Unmasked card numbers in `{self.ctx.header[j]}`",
                f"{c.count:,} value(s) in `{self.ctx.header[j]}` are complete card numbers stored in clear.",
                [self.ctx.header[j]],
                c,
                denominator=self.ctx.profiles[j].non_null,
                sensitive=True,
            )
            for j, c in self.c.items()
            if c.count
        ]


CHECKS = [PIIInText, Secrets, UnmaskedCards]
