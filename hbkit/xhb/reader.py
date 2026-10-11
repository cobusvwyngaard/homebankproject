"""Read a HomeBank .xhb file into typed objects (see model.py).

Behaviour follows homebank_load_xml() in HomeBank's src/hb-xml.c:

* numbers are parsed leniently (like C's atoi / g_ascii_strtod): a value that is
  not a number becomes 0 and is counted as a warning — never shown;
* the text "(null)" written by very old versions counts as empty;
* attributes the toolkit does not know are kept in ``extra``;
* elements it does not know are kept in ``Book.unknown_elements``;
* old meanings are normalised: transaction status before HomeBank 5.9, and the
  ``exact`` attribute of assignment rules before file version 0.8.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..errors import HbkitError, XhbFormatError
from . import format as F
from .model import (
    Account, Book, Category, Currency, Filter, Group, Payee, Properties, Rule, Split,
    Tag, Template, Transaction,
)
from .scanner import RawElement, safe_name, scan

_INT_PREFIX = re.compile(r"\s*[-+]?\d+")
_FLOAT_PREFIX = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class _Attrs:
    """Typed access to one element's attributes, recording value-free warnings."""

    def __init__(self, el: RawElement, warnings: Counter):
        self.el = el
        self.warnings = warnings
        self.used: set[str] = set()

    def _raw(self, name: str) -> str | None:
        self.used.add(name)
        return self.el.attrs.get(name)

    def int(self, name: str, default: int = 0) -> int:
        v = self._raw(name)
        if v is None:
            return default
        try:
            return int(v)
        except ValueError:
            self.warnings[f"non-integer value in {safe_name(self.el.name, name)}"] += 1
            m = _INT_PREFIX.match(v)
            return int(m.group(0)) if m else 0

    def float(self, name: str, default: float = 0.0) -> float:
        v = self._raw(name)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            self.warnings[f"non-numeric value in {safe_name(self.el.name, name)}"] += 1
            m = _FLOAT_PREFIX.match(v)
            return float(m.group(0)) if m else 0.0

    def text(self, name: str, null_is_empty: bool = False) -> str:
        """Text value; HomeBank ignores the literal "(null)" only for some attributes."""
        v = self._raw(name)
        if v is None or (null_is_empty and v == "(null)"):
            return ""
        return v

    def text_n(self, name: str) -> str:
        return self.text(name, null_is_empty=True)

    def tags(self, name: str = "tags") -> list[str]:
        return [t for t in self.text_n(name).split(" ") if t]

    def splits(self) -> list[Split]:
        scat, samt, smem = (self._raw(a) for a in ("scat", "samt", "smem"))
        if scat is None and samt is None and smem is None:
            return []
        cats = (scat or "").split("||")
        amts = (samt or "").split("||")
        mems = (smem or "").split("||")
        if not (len(cats) == len(amts) == len(mems)):
            self.warnings[f"split attributes of different lengths in {safe_name(self.el.name)}"] += 1
            return []
        out = []
        for c, a, m in zip(cats, amts, mems):
            try:
                kcat = int(c) if c else 0
                amount = float(a) if a else 0.0
            except ValueError:
                self.warnings[f"non-numeric split value in {safe_name(self.el.name)}"] += 1
                kcat, amount = 0, 0.0
            out.append(Split(category_key=kcat, amount=amount, memo=m))
        return out

    def extra(self) -> dict[str, str]:
        known = F.KNOWN_ATTRIBUTES.get(self.el.name, ())
        extra = {k: v for k, v in self.el.attrs.items() if k not in known and k not in self.used}
        if extra:
            self.warnings[f"unknown attributes kept on <{self.el.name}>"] += len(extra)
        return extra


def parse_version(v: str) -> float:
    """Interpret v="..." the way hb_xml_get_version() does."""
    m = _FLOAT_PREFIX.match(v or "")
    version = float(m.group(0)) if m else 0.0
    if version == 0.0:
        return 0.1
    if version == 5.0:  # "was a mistake" in HomeBank's own words
        return 1.0
    return version


def read_bytes(data: bytes) -> Book:
    sr = scan(data)
    root = sr.root
    assert root is not None
    if root.name != "homebank":
        raise XhbFormatError("the root element is not <homebank>; is this a HomeBank file?", root.line)

    warnings: Counter = Counter()
    book = Book(raw=data, scan=sr, warnings=warnings)
    book.version = root.attrs.get("v", "")
    book.data_version_text = root.attrs.get("d", "")
    book.file_version = parse_version(book.version)
    m = _INT_PREFIX.match(book.data_version_text)
    book.data_version = int(m.group(0)) if m else 0
    if book.file_version > F.KNOWN_FILE_VERSION + 1e-9:
        warnings["file structure version is newer than this toolkit knows"] += 1
    for issue, count in sr.issues.items():
        warnings[issue] += count

    handlers = {
        "properties": _properties,
        "cur": _currency,
        "grp": _group,
        "account": _account,
        "pay": _payee,
        "cat": _category,
        "tag": _tag,
        "asg": _rule,
        "fav": _template,
        "ope": _transaction,
        "flt": _filter,
    }
    for el in sr.elements:
        if el is root:
            continue
        handler = handlers.get(el.name) if el.depth == 1 else None
        if handler is None:
            book.unknown_elements.append(el)
            warnings["unknown elements kept"] += 1
            continue
        handler(book, el, _Attrs(el, warnings))
    return book


def read_file(path: str | Path) -> Book:
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise HbkitError("the HomeBank file was not found (check the path you gave)") from None
    except IsADirectoryError:
        raise HbkitError("the path given is a folder, not a HomeBank file") from None
    except PermissionError:
        raise HbkitError("the HomeBank file could not be opened (permission denied)") from None
    return read_bytes(data)


# --- one function per element ----------------------------------------------------------


def _properties(book: Book, el: RawElement, a: _Attrs) -> None:
    book.properties = Properties(
        title=a.text("title"),
        currency_key=a.int("curr"),
        vehicle_category_key=a.int("car_category"),
        auto_smode=a.int("auto_smode"),
        auto_weekday=a.int("auto_weekday"),
        auto_nbmonths=a.int("auto_nbmonths"),
        auto_nbdays=a.int("auto_nbdays"),
        earn_by_hour=a.float("earnbyh"),
        extra=a.extra(),
        src=el,
    )


def _currency(book: Book, el: RawElement, a: _Attrs) -> None:
    cur = Currency(
        key=a.int("key"),
        flags=a.int("flags"),
        iso=a.text("iso"),
        name=a.text("name"),
        symbol=a.text("symb"),
        symbol_prefix=a.int("syprf"),
        decimal_char=a.text("dchar"),
        grouping_char=a.text("gchar"),
        frac_digits=a.int("frac", 2),
        rate=a.float("rate"),
        mdate=a.int("mdate"),
        extra=a.extra(),
        src=el,
    )
    book.currencies[cur.key] = cur


def _group(book: Book, el: RawElement, a: _Attrs) -> None:
    grp = Group(key=a.int("key"), name=a.text("name"), extra=a.extra(), src=el)
    book.groups[grp.key] = grp


def _account(book: Book, el: RawElement, a: _Attrs) -> None:
    acc = Account(
        key=a.int("key"),
        flags=a.int("flags"),
        pos=a.int("pos"),
        type=a.int("type"),
        currency_key=a.int("curr"),
        name=a.text_n("name"),
        number=a.text_n("number"),
        bank_name=a.text_n("bankname"),
        initial=a.float("initial"),
        minimum=a.float("minimum"),
        maximum=a.float("maximum"),
        cheque1=a.int("cheque1"),
        cheque2=a.int("cheque2"),
        website=a.text_n("website"),
        notes=a.text_n("notes"),
        template_key=a.int("tpl"),
        group_key=a.int("grp"),
        cc_day=a.int("ccday"),
        reconciled_julian=a.int("rdate"),
        extra=a.extra(),
        src=el,
    )
    book.accounts[acc.key] = acc


def _payee(book: Book, el: RawElement, a: _Attrs) -> None:
    pay = Payee(
        key=a.int("key"),
        flags=a.int("flags"),
        name=a.text("name"),
        category_key=a.int("category"),
        paymode=a.int("paymode"),
        notes=a.text_n("notes"),
        extra=a.extra(),
        src=el,
    )
    book.payees[pay.key] = pay


def _category(book: Book, el: RawElement, a: _Attrs) -> None:
    budget = {}
    for i in range(13):
        if f"b{i}" in el.attrs:
            budget[i] = a.float(f"b{i}")
    cat = Category(
        key=a.int("key"),
        parent_key=a.int("parent"),
        flags=a.int("flags"),
        name=a.text("name"),
        budget=budget,
        extra=a.extra(),
        src=el,
    )
    book.categories[cat.key] = cat


def _tag(book: Book, el: RawElement, a: _Attrs) -> None:
    tag = Tag(key=a.int("key"), name=a.text("name"), extra=a.extra(), src=el)
    book.tags[tag.key] = tag


def _rule(book: Book, el: RawElement, a: _Attrs) -> None:
    flags = a.int("flags")
    exact = a.int("exact")
    if book.file_version <= 0.7:  # "in v08 exact moved to flag"
        flags = F.ASGF_DOCAT | F.ASGF_DOPAY | (F.ASGF_EXACT if exact > 0 else 0)
    book.rules.append(
        Rule(
            key=a.int("key"),
            flags=flags,
            pos=a.int("pos"),
            match_field=a.int("field"),
            search=a.text_n("name"),
            notes=a.text_n("notes"),
            payee_key=a.int("payee"),
            category_key=a.int("category"),
            paymode=a.int("paymode"),
            amount=a.float("amount"),
            tags=a.tags(),
            extra=a.extra(),
            src=el,
        )
    )


def _normalise_status(book: Book, status: int, flags: int) -> tuple[int, int]:
    """Map pre-5.9 status values to today's meaning (homebank_upgrade_to_v14_59)."""
    if book.data_version < F.STATUS_CHANGE_DATA_VERSION:
        if status == F.TXN_OLDSTATUS_REMIND:
            return F.TXN_STATUS_NONE, flags | F.OF_REMIND
        if status == F.TXN_OLDSTATUS_VOID:
            return F.TXN_STATUS_VOID, flags
    return status, flags


def _transaction(book: Book, el: RawElement, a: _Attrs) -> None:
    status, flags = _normalise_status(book, a.int("st"), a.int("flags"))
    txn = Transaction(
        index=len(book.transactions),
        date_julian=a.int("date"),
        amount=a.float("amount"),
        account_key=a.int("account"),
        xfer_amount=a.float("damt"),
        dst_account_key=a.int("dst_account"),
        paymode=a.int("paymode"),
        grpflg=a.int("grpflg"),
        status=status,
        flags=flags,
        payee_key=a.int("payee"),
        category_key=a.int("category"),
        memo=a.text_n("wording"),
        info=a.text_n("info"),
        tags=a.tags(),
        kxfer=a.int("kxfer"),
        splits=a.splits(),
        extra=a.extra(),
        src=el,
    )
    if txn.date is None:
        book.warnings["transactions with an invalid date"] += 1
    book.transactions.append(txn)


def _template(book: Book, el: RawElement, a: _Attrs) -> None:
    book.templates.append(
        Template(
            key=a.int("key"),
            amount=a.float("amount"),
            account_key=a.int("account"),
            xfer_amount=a.float("damt"),
            dst_account_key=a.int("dst_account"),
            paymode=a.int("paymode"),
            grpflg=a.int("grpflg"),
            status=a.int("st"),
            flags=a.int("flags"),
            payee_key=a.int("payee"),
            category_key=a.int("category"),
            memo=a.text_n("wording"),
            info=a.text_n("info"),
            tags=a.tags(),
            rec_flags=a.int("recflg"),
            next_julian=a.int("nextdate"),
            every=a.int("every"),
            unit=a.int("unit"),
            limit=a.int("limit"),
            weekend=a.int("weekend"),
            gap=a.int("gap"),
            ordinal=a.int("ordn"),
            weekday=a.int("wkdy"),
            splits=a.splits(),
            extra=a.extra(),
            src=el,
        )
    )


def _filter(book: Book, el: RawElement, a: _Attrs) -> None:
    groups = {}
    for g in ("dat", "acc", "pay", "cat", "tag", "txt", "amt", "mod", "sta", "typ"):
        if g in el.attrs:
            groups[g] = a.text(g)
    book.filters.append(
        Filter(key=a.int("key"), name=a.text("name"), groups=groups, extra=a.extra(), src=el)
    )
