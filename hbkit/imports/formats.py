"""Bank files for importing into HomeBank: QIF and HomeBank's own CSV layout.

What HomeBank does with them (hb-import.c, hb-import-qif.c, hb-import-csv.c, 5.10.3):

* **QIF**: records end with ``^``. ``D`` date, ``T`` (or ``U``) amount, ``N`` number,
  ``P`` payee, ``M`` memo, ``L`` category, ``S``/``E``/``$`` split lines. HomeBank looks
  the ``L`` category up by its full name ("Parent:Sub", split on the first ``:``) and
  creates it when it does not exist; ``L[Name]`` is a transfer to account Name.
* **HomeBank CSV**: exactly 8 columns — date; payment mode; info; payee; memo; amount;
  category; tags — separated by the separator chosen in HomeBank's preferences
  (semicolon by default). A field may be enclosed in double quotes, with ``""`` for a
  quote inside. Payment mode 5 is an internal transfer.
* The file is read as UTF-8 when all of it is valid UTF-8; otherwise HomeBank detects
  another character set. A prepared copy therefore keeps the original's encoding:
  categories are written in UTF-8, or in Windows-1252 for files that are not UTF-8.
* On import HomeBank keeps a category from the file. "Auto from payee" and
  "set category" rules only fill empty categories; only "overwrite category" rules
  replace it.
* **A bank's own CSV layout** (a header row naming the columns, e.g. "Value Date",
  "Type", "Description", "Beneficiary or CardHolder", "Amount") cannot be imported by
  HomeBank at all. Such a file is *converted* to HomeBank CSV when it is prepared: one
  line per transaction, dates as day-month-year, the bank's transaction type turned
  into a payment mode, and the categories in column 7.

Preparing a QIF or HomeBank CSV file only adds categories: every other byte stays as
it was. Every prepared file is read back and compared before it is used.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

from ..errors import HbkitError

QIF_TRANSACTION_TYPES = {"bank", "cash", "ccard", "oth a", "oth l"}
CSV_COLUMNS = 8
CSV_CATEGORY = 6
CSV_TRANSFER_PAYMODE = 5

#: Column names (lower case) recognised in the header row of a bank's own CSV export.
BANK_COLUMNS = {
    "date": ("value date", "transaction date", "date"),
    "type": ("type", "transaction type"),
    "memo": ("description",),
    "payee": ("beneficiary or cardholder", "beneficiary"),
    "amount": ("amount",),
}
#: Words in the bank's transaction type and the HomeBank payment mode they become
#: (enums.h); the first match wins, otherwise 0 (none).
BANK_TYPE_PAYMODES = (
    ("debit order", 11), ("transfer", 4), ("re-direct", 4), ("eft", 8), ("payment", 8), ("card", 6),
    ("pos", 6), ("atm", 3), ("cash", 3), ("fee", 10), ("charge", 10), ("deposit", 9),
)


class ImportFormatError(HbkitError):
    """The file is not a QIF or HomeBank CSV file the toolkit understands."""


@dataclass
class ImportTxn:
    """One transaction of an import file."""

    index: int  # position among the file's transactions
    line: int  # line number (1-based) where it starts
    date: date | None
    amount: float | None
    memo: str
    payee: str
    info: str = ""
    category: str = ""  # category already in the file
    transfer: bool = False  # internal transfer: never given a category
    split: bool = False  # split transaction: its categories are in the split lines
    paymode: int = 0
    tags: str = ""  # HomeBank CSV: the tags column (names separated by spaces)
    #: memo and info as HomeBank stores them on import, where they differ from the
    #: cleaned values above (HomeBank keeps a QIF memo's and a CSV memo's spaces)
    raw_memo: str | None = None
    raw_info: str | None = None

    @property
    def memo_as_imported(self) -> str:
        return self.memo if self.raw_memo is None else self.raw_memo

    @property
    def info_as_imported(self) -> str:
        return self.info if self.raw_info is None else self.raw_info

    @property
    def can_take_category(self) -> bool:
        return not (self.category or self.transfer or self.split)


@dataclass
class ImportFile:
    name: str
    format: str  # "qif", "csv" (HomeBank's layout), "bank-csv" or "pdf-statement" (converted)
    raw: bytes
    encoding: str  # "utf-8" or "cp1252"
    txns: list[ImportTxn]
    date_order: str = "DMY"  # DMY, MDY or YMD
    qif_type: str = ""  # QIF: Bank, CCard, ...
    separator: str = ""  # CSV
    layout: str = ""  # bank CSV or PDF: the columns used, e.g. "Value Date, Description, Amount"
    #: PDF statements: digit strings of 6-16 digits in the document (one is usually the account number)
    numbers: list[str] = field(default_factory=list)
    #: line numbers HomeBank would not read as transactions (no values)
    problems: list[int] = field(default_factory=list)
    #: where a category goes, per transaction: ("insert", line) before that line,
    #: ("replace", line) an empty L line, or ("field", line, start, end) a CSV field
    _slots: dict[int, tuple] = field(default_factory=dict, repr=False)

    @property
    def first_date(self) -> date | None:
        dates = [t.date for t in self.txns if t.date]
        return min(dates) if dates else None

    @property
    def last_date(self) -> date | None:
        dates = [t.date for t in self.txns if t.date]
        return max(dates) if dates else None

    @property
    def lines(self) -> list[bytes]:
        return self.raw.splitlines(keepends=True)


# --- values ------------------------------------------------------------------------------------

_DATE = re.compile(r"^\s*(\d{1,4})\s*[-/.' ]\s*(\d{1,2})\s*[-/.' ]\s*'?\s*(\d{1,4})\s*$")


def _date_parts(text: str) -> tuple[int, int, int] | None:
    m = _DATE.match(text)
    if m is None or (len(m[1]) > 2 and len(m[3]) > 2) or len(m[1]) == 3 or len(m[3]) == 3:
        return None
    return int(m[1]), int(m[2]), int(m[3])


def date_order(texts: list[str]) -> str:
    """The order of day and month used by a file's dates (South African default: day first)."""
    parts = [p for p in map(_date_parts, texts) if p]
    if parts and all(p[0] > 31 for p in parts):
        return "YMD"
    if any(p[0] > 12 for p in parts if p[0] <= 31):
        return "DMY"
    if any(p[1] > 12 for p in parts):
        return "MDY"
    return "DMY"


def parse_date(text: str, order: str = "DMY") -> date | None:
    p = _date_parts(text)
    if p is None:
        return None
    a, b, c = p
    if a > 31:  # year first
        y, m, d = a, b, c
    elif order == "MDY":
        m, d, y = a, b, c
    else:
        d, m, y = a, b, c
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_amount(text: str) -> float | None:
    """'-1,234.56', '1234.56', '1 234,56' and similar."""
    s = text.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _decode(raw: bytes) -> str:
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def _text(line: bytes, encoding: str) -> str:
    return line.decode(encoding, errors="replace").rstrip("\r\n")


# --- reading -------------------------------------------------------------------------------------


def read_import_file(name: str, raw: bytes) -> ImportFile:
    """Read a QIF, HomeBank CSV or bank CSV file, or a PDF statement. The format is detected from the contents."""
    if raw.startswith(b"%PDF-"):
        from .statement import read_pdf_statement  # here: statement.py imports this module

        return read_pdf_statement(name, raw)
    encoding = _decode(raw)
    lines = raw.splitlines(keepends=True)
    first = next((_text(x, encoding).strip() for x in lines if x.strip()), "")
    if first.startswith("﻿"):
        first = first[1:]
    if first.startswith("!"):
        return _read_qif(name, raw, lines, encoding)
    f = _read_csv(name, raw, lines, encoding) or _read_bank_csv(name, raw, encoding)
    if f is None:
        raise ImportFormatError("this is neither a QIF file, a HomeBank CSV file (8 columns: date; payment mode; "
                                "info; payee; memo; amount; category; tags), a bank CSV with a header row "
                                "naming its date, description and amount columns, nor a PDF statement")
    return f


def _read_qif(name: str, raw: bytes, lines: list[bytes], encoding: str) -> ImportFile:
    f = ImportFile(name, "qif", raw, encoding, [])
    section = ""
    record: dict[str, list[tuple[int, str]]] = {}
    start = None
    pending: list[tuple[int, dict, int]] = []  # (record start line, fields, ^ line)
    for i, line in enumerate(lines):
        text = _text(line, encoding)
        if i == 0:
            text = text.lstrip("﻿")
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            head = stripped[1:]
            if head.lower().startswith("type:"):
                section = head[5:].strip()
                if not f.qif_type and section.lower() in QIF_TRANSACTION_TYPES:
                    f.qif_type = section
            elif head.lower().startswith("account"):
                section = "account"
            record, start = {}, None
            continue
        if stripped == "^":
            if record and section.lower() in QIF_TRANSACTION_TYPES:
                pending.append((start, record, i))
            record, start = {}, None
            continue
        if start is None:
            start = i
        body = text.lstrip()
        record.setdefault(body[0].upper(), []).append((i, body[1:]))
    if record and section.lower() in QIF_TRANSACTION_TYPES:
        f.problems.append(start + 1)  # last record has no closing ^: HomeBank does not store it
    order = date_order([fields["D"][0][1] for _, fields, _ in pending if "D" in fields])
    f.date_order = order
    for start, fields, end in pending:
        value = lambda k: fields[k][0][1].strip() if k in fields else ""  # noqa: E731
        amount = parse_amount(value("T") or value("U"))
        category = value("L")
        txn = ImportTxn(
            index=len(f.txns), line=start + 1, date=parse_date(value("D"), order), amount=amount,
            memo=value("M"), payee=value("P"), info=value("N"),
            category="" if category.startswith("[") else category,
            transfer=category.startswith("["), split="S" in fields,
            raw_memo=fields["M"][0][1].rstrip("\r\n") if "M" in fields else None,
        )
        if txn.date is None or amount is None:
            f.problems.append(start + 1)
        if "L" in fields and not category:
            f._slots[txn.index] = ("replace", fields["L"][0][0])
        elif "L" not in fields:
            f._slots[txn.index] = ("insert", end)
        f.txns.append(txn)
    if not f.qif_type:
        raise ImportFormatError("this QIF file has no bank, cash or credit-card transactions (!Type:Bank etc.)")
    return f


def _split_fields(line: bytes, sep: int) -> list[tuple[int, int]]:
    """Byte spans of the fields of a CSV line, as HomeBank splits them (quotes respected)."""
    spans, start, enclosed = [], 0, False
    end = len(line.rstrip(b"\r\n"))
    for pos in range(end):
        c = line[pos]
        if c == sep and not enclosed:
            spans.append((start, pos))
            start = pos + 1
        elif c == 0x22:  # "
            enclosed = not enclosed
    spans.append((start, end))
    return spans


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _read_csv(name: str, raw: bytes, lines: list[bytes], encoding: str) -> ImportFile | None:
    best = None
    for sep in (";", ",", "\t"):
        rows = []
        for i, line in enumerate(lines):
            spans = _split_fields(line, ord(sep))
            if len(spans) == CSV_COLUMNS:
                texts = [_unquote(_text(line[a:b], encoding)) for a, b in spans]
                values = [t.strip() for t in texts]
                if i == 0:
                    values[0] = values[0].lstrip("﻿")
                if _date_parts(values[0]):
                    rows.append((i, spans, values, texts))
        if rows and (best is None or len(rows) > len(best[1])):
            best = (sep, rows)
    if best is None:
        return None
    sep, rows = best
    content = [i for i, line in enumerate(lines) if line.strip()]
    if len(rows) * 2 < len(content):
        return None
    f = ImportFile(name, "csv", raw, encoding, [], separator=sep)
    f.date_order = order = date_order([v[0] for _, _, v, _ in rows])
    valid = {i for i, _, _, _ in rows}
    f.problems = [i + 1 for i in content if i not in valid]
    for i, spans, v, texts in rows:
        paymode = int(v[1]) if v[1].isdigit() else 0
        txn = ImportTxn(
            index=len(f.txns), line=i + 1, date=parse_date(v[0], order), amount=parse_amount(v[5]),
            memo=v[4], payee=v[3], info=v[2], category=v[CSV_CATEGORY],
            transfer=paymode == CSV_TRANSFER_PAYMODE, paymode=paymode, tags=v[7],
            raw_memo=texts[4], raw_info=texts[2],
        )
        if txn.date is None or txn.amount is None:
            f.problems.append(i + 1)
        if not txn.category:
            a, b = spans[CSV_CATEGORY]
            f._slots[txn.index] = ("field", i, a, b)
        f.txns.append(txn)
    f.problems.sort()
    return f


def bank_paymode(kind: str) -> int:
    kind = kind.casefold()
    for word, paymode in BANK_TYPE_PAYMODES:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", kind):
            return paymode
    return 0


def _read_bank_csv(name: str, raw: bytes, encoding: str) -> ImportFile | None:
    """A bank's own CSV export with a header row (see BANK_COLUMNS)."""
    text = raw.decode("utf-8-sig" if encoding == "utf-8" else encoding, errors="replace")
    first = next((x for x in text.splitlines() if x.strip()), "")
    sep = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=sep)
    header, columns = None, {}
    for row in reader:
        if any(c.strip() for c in row):
            header = [c.strip().casefold() for c in row]
            break
    if header is None:
        return None
    for key, names in BANK_COLUMNS.items():
        found = next((header.index(n) for n in names if n in header), None)
        if found is not None:
            columns[key] = found
    if not {"date", "memo", "amount"} <= set(columns):
        return None
    rows = []
    for row in reader:
        if any(c.strip() for c in row):
            rows.append((reader.line_num, row))
    width = max(columns.values()) + 1
    get = lambda row, key: row[columns[key]].strip() if key in columns and len(row) > columns[key] else ""  # noqa: E731
    order = date_order([get(r, "date") for _, r in rows])
    f = ImportFile(name, "bank-csv", raw, encoding, [], date_order=order, separator=sep,
                   layout=", ".join(first.split(sep)[i].strip().strip('"').lstrip("\ufeff")
                                    for i in sorted(columns.values())))
    for line, row in rows:
        when, amount = parse_date(get(row, "date"), order), parse_amount(get(row, "amount"))
        if len(row) < width or when is None or amount is None:
            f.problems.append(line)
            continue
        f.txns.append(ImportTxn(index=len(f.txns), line=line, date=when, amount=amount,
                                memo=" ".join(get(row, "memo").split()), payee=" ".join(get(row, "payee").split()),
                                paymode=bank_paymode(get(row, "type"))))
    return f if f.txns else None


# --- writing -------------------------------------------------------------------------------------


def _check_category(f: ImportFile, category: str) -> bytes:
    if not category or category != category.strip() or any(ord(c) < 32 for c in category):
        raise ImportFormatError("a category name is empty, has spaces around it or contains control characters")
    if category.startswith("["):
        raise ImportFormatError("a category name starting with [ would be read as a transfer")
    try:
        data = category.encode(f.encoding)
    except UnicodeEncodeError:
        raise ImportFormatError("a category name has characters this file's encoding cannot hold") from None
    if f.format == "csv" and (f.separator in category or '"' in category):
        data = b'"' + data.replace(b'"', b'""') + b'"'
    return data


def _eol(lines: list[bytes], i: int) -> bytes:
    for candidate in (lines[i] if i < len(lines) else b"", lines[i - 1] if i else b""):
        stripped = candidate.rstrip(b"\r\n")
        if len(stripped) < len(candidate):
            return candidate[len(stripped):]
    return b"\r\n" if b"\r\n" in b"".join(lines) else b"\n"


def with_categories(f: ImportFile, categories: dict[int, str]) -> bytes:
    """The file with these categories (transaction index -> full category name) added.

    Only transactions without a category, transfer or split can take one. The result
    is read back and compared with the original before it is returned. A bank CSV or a
    PDF statement is converted to HomeBank CSV.
    """
    if f.format in ("bank-csv", "pdf-statement"):
        return _to_homebank_csv(f, categories)
    lines = f.lines
    inserts: dict[int, list[bytes]] = {}
    for index, category in categories.items():
        slot = f._slots.get(index)
        txn = f.txns[index] if 0 <= index < len(f.txns) else None
        if slot is None or txn is None or not txn.can_take_category:
            raise ImportFormatError(f"transaction {index + 1} cannot take a category")
        data = _check_category(f, category)
        if slot[0] == "insert":
            inserts.setdefault(slot[1], []).append(b"L" + data + _eol(lines, slot[1]))
        elif slot[0] == "replace":
            line = lines[slot[1]]
            body = line.rstrip(b"\r\n")
            lines[slot[1]] = b"L" + data + line[len(body):]
        else:
            _, i, a, b = slot
            lines[i] = lines[i][:a] + data + lines[i][b:]
    out = []
    for i, line in enumerate(lines):
        out += inserts.get(i, [])
        out.append(line)
    data = b"".join(out)
    _verify(f, data, categories)
    return data


def _csv_field(value: str) -> str:
    value = " ".join(value.split())  # HomeBank reads one line per transaction
    return f'"{value.replace(chr(34), chr(34) * 2)}"' if (";" in value or '"' in value) else value


def _to_homebank_csv(f: ImportFile, categories: dict[int, str]) -> bytes:
    """A bank CSV converted to HomeBank CSV (semicolons, day-month-year, UTF-8, CRLF)."""
    for index, category in categories.items():
        if not 0 <= index < len(f.txns) or not f.txns[index].can_take_category:
            raise ImportFormatError(f"transaction {index + 1} cannot take a category")
        _check_category(f, category)
    lines = []
    for t in f.txns:
        cents = round(t.amount * 100)
        amount = f"{cents / 100:.2f}" if abs(t.amount * 100 - cents) < 1e-6 else repr(t.amount)
        lines.append(";".join([
            t.date.strftime("%d-%m-%Y"),
            str(t.paymode), _csv_field(t.info), _csv_field(t.payee), _csv_field(t.memo), amount,
            _csv_field(categories.get(t.index, "")), "",
        ]))
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    again = read_import_file(f.name, data)
    ok = again.format == "csv" and len(again.txns) == len(f.txns)
    for old, new in zip(f.txns, again.txns) if ok else ():
        ok = ok and (old.date, round(old.amount, 2), old.memo, old.payee, old.paymode) == \
            (new.date, round(new.amount, 2), new.memo, new.payee, new.paymode) \
            and new.category == categories.get(old.index, "")
    if not ok:
        raise ImportFormatError("the converted file did not read back the same; nothing was written")
    return data


def _verify(f: ImportFile, data: bytes, categories: dict[int, str]) -> None:
    again = read_import_file(f.name, data)
    if again.format != f.format or len(again.txns) != len(f.txns) or again.encoding != f.encoding:
        raise ImportFormatError("the prepared file did not read back the same; nothing was written")
    for old, new in zip(f.txns, again.txns):
        expected = categories.get(old.index, old.category)
        same = (old.date, old.amount, old.memo, old.payee, old.info, old.transfer, old.split) == \
               (new.date, new.amount, new.memo, new.payee, new.info, new.transfer, new.split)
        if not same or new.category != expected:
            raise ImportFormatError(f"the prepared file did not read back the same (transaction "
                                    f"{old.index + 1}); nothing was written")
