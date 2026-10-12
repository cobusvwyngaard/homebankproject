"""Transactions from the table of a PDF bank statement.

The transaction table is found by its header row: a date column, a description column,
and amounts as debit/credit columns or one amount column, usually with fees and a
running balance (see HEADERS). Every page is searched, so tables that continue over
several pages (with the header repeated) are read in full.

* Text columns (reference, date, description) are recognised by where they start,
  amounts by where they end (they are right-aligned under their header).
* A line without a date or amounts continues the description of the line above.
* "Opening balance", "Closing balance" and "brought/carried forward" lines are not
  transactions; their balances are used as checks.
* Debits and fees are money out, credits money in; an amount marked with * (charges
  including VAT) or from the fees column gets the payment mode "fee". A line with both
  a fee and another amount becomes two transactions.
* **Check:** every amount must lead from the previous balance to the balance printed on
  its line, and the opening balance plus all amounts to the closing balance. If not, the
  statement is refused rather than imported with a wrong amount or sign.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .formats import ImportFile, ImportFormatError, ImportTxn, date_order, parse_amount, parse_date
from .pdf import Chunk, Line, lines, page_glyphs

#: Column header words (lower case, without "(R)" and the like) and their meaning.
HEADERS = {
    "date": ("date", "transaction date", "value date", "posting date", "trans date"),
    "description": ("description", "details", "narrative", "transaction description", "transaction details"),
    "reference": ("tran list no", "reference", "ref", "ref no", "reference no"),
    "fees": ("fees", "fee", "charges", "service fee"),
    "debit": ("debits", "debit", "money out", "withdrawals", "payments"),
    "credit": ("credits", "credit", "money in", "deposits", "receipts"),
    "amount": ("amount", "amounts"),
    "balance": ("balance", "running balance"),
}
TEXT_COLUMNS = ("reference", "date", "description")
NUMBER_COLUMNS = ("fees", "debit", "credit", "amount", "balance")
FEE_PAYMODE = 10

_AMOUNT = re.compile(r"^(-)?R?\s?([\d ,]*\d[.,]\d{2})\s*(\*|-|cr|dr)?$", re.I)
_SPECIAL = re.compile(r"(opening|closing)\s+balance|balance\s+(brought|carried)\s+forward|"
                      r"(brought|carried)\s+forward", re.I)
_ACCOUNT_NUMBER = re.compile(r"(?<!\d)\d(?:[ -]?\d){5,15}(?!\d)")


def _header_key(text: str) -> str | None:
    t = re.sub(r"\(.*?\)", "", text).strip().casefold()
    t = re.sub(r"[^a-z ]", "", t).strip()
    for key, names in HEADERS.items():
        if t in names:
            return key
    return None


@dataclass
class _Column:
    key: str
    x0: float
    x1: float


def _split_headers(chunk: Chunk) -> list[tuple[str, float, float]]:
    """Header names in one chunk, e.g. "Tran list no Date" when two labels are close together:
    (key, x0, x1) for each, positioned by their share of the characters."""
    key = _header_key(chunk.text)
    if key:
        return [(key, chunk.x0, chunk.x1)]
    words = chunk.text.split()
    found, i = [], 0
    while i < len(words):
        for n in range(min(4, len(words) - i), 0, -1):
            k = _header_key(" ".join(words[i:i + n]))
            if k:
                found.append((k, i, i + n))
                i += n
                break
        else:
            return []
    if len(found) < 2:
        return []
    total = len(chunk.text)
    per = (chunk.x1 - chunk.x0) / max(total, 1)
    out = []
    for k, a, b in found:
        start = len(" ".join(words[:a])) + (1 if a else 0)
        end = len(" ".join(words[:b]))
        out.append((k, chunk.x0 + start * per, chunk.x0 + end * per))
    return out


def _find_header(line: Line) -> list[_Column] | None:
    cols = []
    for c in line.chunks:
        for key, x0, x1 in _split_headers(c):
            cols.append(_Column(key, x0, x1))
    keys = {c.key for c in cols}
    if "date" in keys and "description" in keys and ({"debit", "credit"} <= keys or "amount" in keys):
        return sorted(cols, key=lambda c: c.x0)
    return None


def amount_of(text: str) -> tuple[float, str] | None:
    """(value, marker) of an amount such as "7,281.40", "3.50 *", "12.50-" or "100.00 Cr"."""
    m = _AMOUNT.match(text.strip())
    if not m:
        return None
    value = parse_amount(m.group(2))
    if value is None:
        return None
    marker = (m.group(3) or "").casefold()
    if m.group(1) or marker in ("-", "dr"):
        value = -value
    return value, marker


@dataclass
class _Row:
    page: int
    number: int  # row number in the table, counted over all pages (1-based)
    texts: dict[str, list[str]] = field(default_factory=dict)
    amounts: dict[str, tuple[float, str]] = field(default_factory=dict)

    def text(self, key: str) -> str:
        return " ".join(self.texts.get(key, []))


def _assign(chunk: Chunk, columns: list[_Column]) -> tuple[str, object] | None:
    amount = amount_of(chunk.text)
    numbers = [c for c in columns if c.key in NUMBER_COLUMNS]
    if amount is not None and numbers:
        # Amounts are right-aligned: compare where the number itself ends (without a marker).
        digits = len(re.sub(r"\s*(\*|-|cr|dr)$", "", chunk.text.strip(), flags=re.I))
        right = chunk.x0 + (chunk.x1 - chunk.x0) * digits / max(len(chunk.text.strip()), 1)

        def score(c: _Column):
            overlap = min(chunk.x1, c.x1) - max(chunk.x0, c.x0)
            return (overlap <= 0, abs(right - c.x1))
        best = min(numbers, key=score)
        if min(chunk.x1, best.x1) - max(chunk.x0, best.x0) > 0 or abs(right - best.x1) <= 40:
            return best.key, amount
    texts = [c for c in columns if c.key in TEXT_COLUMNS]
    if not texts:
        return None
    starts = [c for c in texts if c.x0 <= chunk.x0 + 6]  # text starts at (or just after) its header
    col = max(starts, key=lambda c: c.x0) if starts else texts[0]
    return col.key, chunk.text


def read_statement_rows(raw: bytes) -> tuple[list[_Row], list[str], set[str]]:
    """The table rows of all pages, the header names found, and the digit strings of 6 to
    16 digits anywhere in the statement (one is usually the account number)."""
    rows: list[_Row] = []
    header_names: list[str] = []
    numbers: set[str] = set()
    found_header = False
    for page_no, glyphs in enumerate(page_glyphs(raw), start=1):
        columns = None
        row_size = None
        last_y = None
        for line in lines(glyphs):
            for m in _ACCOUNT_NUMBER.finditer(line.text):
                numbers.add(re.sub(r"\D", "", m.group(0)))
            numbers.update(t for t in re.findall(r"(?<!\d)\d{6,16}(?!\d)", line.text))
            header = _find_header(line)
            if header:
                columns, row_size, last_y = header, None, line.y
                found_header = True
                if not header_names:
                    header_names = [c.text for c in line.chunks if _split_headers(c)]
                continue
            if columns is None:
                continue
            if row_size is None:
                row_size = line.size
            elif abs(line.size - row_size) > 0.3 * row_size and not _SPECIAL.search(line.text):
                columns = None  # the table ended (e.g. a footer in another size)
                continue
            first_x = min(c.x0 for c in columns)
            if any(c.x0 < first_x - 15 for c in line.chunks) and not _SPECIAL.search(line.text):
                columns = None
                continue
            if last_y is not None and last_y - line.y > 4 * max(line.size, row_size):
                columns = None  # far below the table: a footer
                continue
            last_y = line.y
            row = _Row(page_no, len(rows) + 1)
            for chunk in line.chunks:
                placed = _assign(chunk, columns)
                if placed is None:
                    continue
                key, value = placed
                if isinstance(value, tuple):
                    row.amounts[key] = value
                else:
                    row.texts.setdefault(key, []).append(value)
            date_text = row.text("date")
            if not date_text and not row.amounts and rows and row.texts and not _SPECIAL.search(line.text):
                prev = rows[-1]  # a wrapped description
                prev.texts.setdefault("description", []).append(row.text("description") or line.text)
                continue
            rows.append(row)
            if re.search(r"closing\s+balance", line.text, re.I):
                columns = None
    if not found_header:
        raise ImportFormatError("no transaction table was found in this PDF (a header row with date, description "
                                "and amount or debit/credit columns)")
    return rows, header_names, numbers


def read_pdf_statement(name: str, raw: bytes) -> ImportFile:
    rows, header_names, numbers = read_statement_rows(raw)
    f = ImportFile(name, "pdf-statement", raw, "utf-8", [], layout=", ".join(header_names), numbers=sorted(numbers))
    f.date_order = order = date_order([r.text("date") for r in rows if r.text("date")])
    balance = None  # running balance as printed
    opening = closing = None
    mismatches = []
    total = 0.0
    last_date = None
    for r in rows:
        desc = " ".join(r.text("description").split())
        special = _SPECIAL.search(" ".join([desc] + [r.text(k) for k in TEXT_COLUMNS]))
        printed = r.amounts.get("balance", (None, ""))[0]
        if special:
            if printed is not None:
                if re.search(r"opening|brought", special.group(0), re.I):
                    if opening is None:
                        opening = printed
                    if balance is not None and abs(balance - printed) > 0.005:
                        mismatches.append(r.number)
                    balance = printed
                else:
                    closing = printed
                    if balance is not None and abs(balance - printed) > 0.005:
                        mismatches.append(r.number)
            continue
        when = parse_date(r.text("date"), order) if r.text("date") else last_date
        last_date = when
        parts = []
        for key in ("debit", "credit", "amount"):
            if key in r.amounts:
                value, marker = r.amounts[key]
                value = -abs(value) if key == "debit" else abs(value) if key == "credit" else value
                parts.append((value, FEE_PAYMODE if marker == "*" else 0, desc))
        if "fees" in r.amounts:
            parts.append((-abs(r.amounts["fees"][0]), FEE_PAYMODE, f"{desc} (fee)" if parts else desc))
        if when is None or not parts or not desc:
            f.problems.append(r.number)
            continue
        for value, paymode, memo in parts:
            f.txns.append(ImportTxn(index=len(f.txns), line=r.number, date=when, amount=round(value, 2),
                                    memo=memo, payee="", info=r.text("reference"), paymode=paymode))
            total += value
        if printed is not None:
            if balance is not None and abs(balance + sum(p[0] for p in parts) - printed) > 0.005:
                mismatches.append(r.number)
            balance = printed
        elif balance is not None:
            balance += sum(p[0] for p in parts)
    if opening is not None and closing is not None and abs(opening + total - closing) > 0.005:
        mismatches.append(0)
    if mismatches:
        raise ImportFormatError(f"the amounts in this statement do not add up to its balances ({len(mismatches)} "
                                "place(s)), so its layout was not read correctly; nothing was imported")
    if not f.txns:
        raise ImportFormatError("the statement's transaction table has no transactions")
    return f
