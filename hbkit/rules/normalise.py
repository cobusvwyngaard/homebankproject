"""Clean bank-style descriptions so that the same merchant gives the same pattern.

Each step is a named regular expression, applied in order to the upper-cased text.
They remove what changes from one transaction to the next — card numbers, dates,
times, amounts, references, branch codes and other numbers — and keep the words
that identify the merchant. Example:

    "POS PURCHASE WOOLWORTHS SANDTON 458712******1234 12 MAR"
        -> "POS PURCHASE WOOLWORTHS SANDTON"
"""

from __future__ import annotations

import re

_MONTH = (r"(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|AUG(?:UST)?|"
          r"SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)")

#: (name, pattern) pairs, applied in this order. Each match is replaced by a space.
STEPS: list[tuple[str, re.Pattern]] = [
    ("control characters", re.compile(r"[\x00-\x1f\x7f]")),
    ("masked card numbers", re.compile(r"\b\d{4,6}[*X#]+\d{2,4}\b")),
    ("masked card numbers (short)", re.compile(r"(?<![A-Z0-9])[*X#]{2,}\d{2,4}\b")),
    ("full card numbers", re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b")),
    ("card number labels", re.compile(r"\bCARD\s+(?:NO\.?|NUMBER)?\s*[:#]?\s*\d{4,}\b")),
    ("ISO dates", re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b")),
    ("numeric dates", re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b")),
    ("day-month dates", re.compile(rf"\b\d{{1,2}}\s?{_MONTH}\b\.?(?:\s?\d{{2,4}}\b)?")),
    ("day-month dates (joined)", re.compile(rf"\b\d{{1,2}}{_MONTH}\d{{2,4}}\b")),
    ("month-day dates", re.compile(rf"\b{_MONTH}\s\d{{1,2}}\b(?:\s\d{{4}}\b)?")),
    ("times", re.compile(r"\b\d{1,2}[:H]\d{2}(?::\d{2})?\b")),
    ("currency amounts", re.compile(r"(?:\bR|\bZAR|\bUSD|\bEUR|\bGBP|\$)\s?\d[\d ,]*(?:[.,]\d{2})?\b")),
    ("decimal amounts", re.compile(r"\b\d+[.,]\d{2}\b")),
    ("references", re.compile(
        r"\b(?:REF(?:ERENCE)?|REFNO|RRN|TRACE|AUTH|C/ID|CID|TXN|TRX|INV(?:OICE)?|RCPT|RECEIPT|NO)\b"
        r"\s*[:.#-]?\s*[A-Z0-9/-]*\d[A-Z0-9/-]*")),
    ("references (joined)", re.compile(r"\b(?:REF|RRN|TRACE|AUTH)[:#]?[A-Z0-9]*\d[A-Z0-9]*\b")),
    ("branch codes", re.compile(r"\b(?:BRANCH|BRNCH|BR)\s*(?:CODE)?\s*[:#]?\s*\d+\b")),
    ("codes with digits", re.compile(r"\b(?=[A-Z0-9]*\d[A-Z0-9]*\d[A-Z0-9]*\d)[A-Z0-9]{4,}\b")),
    ("numbers", re.compile(r"(?<![\w-])\d+(?![\w-])")),
    ("punctuation", re.compile(r"[^\w\s&.'@+-]|_")),
]

#: Country codes often found at the end of card transactions, and reference labels
#: left behind once their reference number has been removed.
TRAILING_NOISE = frozenset({"ZA", "ZAF", "RSA", "SA", "ZAR", "REF", "REFERENCE", "REFNO", "RRN", "NO", "CID"})

#: Words that describe the kind of transaction rather than the merchant. A rule text
#: made only of these is never suggested.
GENERIC_WORDS = frozenset({
    "POS", "PURCHASE", "PURCH", "PUR", "CARD", "DEBIT", "CREDIT", "ORDER", "PAYMENT", "PAYMT",
    "PMT", "TRANSFER", "TRF", "XFER", "FROM", "TO", "THE", "AND", "OF", "FOR", "IB", "APP",
    "ONLINE", "INTERNET", "CASH", "WITHDRAWAL", "DEPOSIT", "EFT", "ACB", "MAGTAPE", "DR", "CR",
    "SETTLEMENT", "REF", "NO", "LOCAL", "INTL", "INT", "ATM", "BANKING", "MOBILE", "SEND",
    "RECEIVED", "PAID", "PAY", "TFR", "IMMEDIATE", "REAL-TIME", "INSTANT", "FNB", "ABSA",
    "NEDBANK", "CAPITEC", "STANDARD", "BANK",
})

_EDGE = ".-'+@&"


def normalise(text: str) -> str:
    """The cleaned pattern for a description (upper case, single spaces)."""
    s = text.upper()
    for _, rx in STEPS:
        s = rx.sub(" ", s)
    tokens = [t.strip(_EDGE) for t in s.split()]
    tokens = [t for t in tokens if t]
    while tokens and tokens[-1] in TRAILING_NOISE:
        tokens.pop()
    return " ".join(tokens)


def is_generic(tokens: list[str]) -> bool:
    return all(t in GENERIC_WORDS for t in tokens)


def explain(text: str) -> list[tuple[str, str]]:
    """Each step's effect, for debugging normalisation on synthetic examples."""
    s = text.upper()
    out = []
    for name, rx in STEPS:
        new = rx.sub(" ", s)
        if new != s:
            out.append((name, " ".join(new.split())))
        s = new
    return out
