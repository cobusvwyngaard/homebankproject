"""Typed objects for the contents of a HomeBank file.

Field names are descriptive; the matching XML attribute names are listed in
hbkit/xhb/reader.py and docs/xhb-format.md. Every object keeps:

* ``extra`` — attributes this toolkit does not know (from other HomeBank versions),
  kept as text so nothing is lost;
* ``src`` — the raw element it was read from (byte span and line), or ``None``
  for objects created by the toolkit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from . import format as F
from .scanner import RawElement, ScanResult


def julian_to_date(julian: int) -> date | None:
    """Convert a GLib Julian day number (1 = 0001-01-01) to a date."""
    if 1 <= julian <= date.max.toordinal():
        return date.fromordinal(julian)
    return None


def date_to_julian(d: date) -> int:
    return d.toordinal()


@dataclass(kw_only=True)
class Entity:
    extra: dict[str, str] = field(default_factory=dict)
    src: RawElement | None = field(default=None, repr=False, compare=False)


@dataclass(kw_only=True)
class Properties(Entity):
    title: str = ""
    currency_key: int = 0
    vehicle_category_key: int = 0
    auto_smode: int = 0
    auto_weekday: int = 0
    auto_nbmonths: int = 0
    auto_nbdays: int = 0
    earn_by_hour: float = 0.0


@dataclass(kw_only=True)
class Currency(Entity):
    key: int = 0
    flags: int = 0
    iso: str = ""
    name: str = ""
    symbol: str = ""
    symbol_prefix: int = 0
    decimal_char: str = ""
    grouping_char: str = ""
    frac_digits: int = 2
    rate: float = 0.0
    mdate: int = 0


@dataclass(kw_only=True)
class Group(Entity):
    key: int = 0
    name: str = ""


@dataclass(kw_only=True)
class Account(Entity):
    key: int = 0
    flags: int = 0
    pos: int = 0
    type: int = 0
    currency_key: int = 0
    name: str = ""
    number: str = ""
    bank_name: str = ""
    initial: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0
    cheque1: int = 0
    cheque2: int = 0
    website: str = ""
    notes: str = ""
    template_key: int = 0
    group_key: int = 0
    cc_day: int = 0
    reconciled_julian: int = 0

    @property
    def is_closed(self) -> bool:
        return bool(self.flags & F.AF_CLOSED)

    @property
    def type_name(self) -> str:
        return F.ACCOUNT_TYPE_NAMES.get(self.type, f"type {self.type}")


@dataclass(kw_only=True)
class Payee(Entity):
    key: int = 0
    flags: int = 0
    name: str = ""
    category_key: int = 0
    paymode: int = 0
    notes: str = ""


@dataclass(kw_only=True)
class Category(Entity):
    key: int = 0
    parent_key: int = 0
    flags: int = 0
    name: str = ""
    budget: dict[int, float] = field(default_factory=dict)  # 0 = every month, 1..12 = months

    @property
    def is_income(self) -> bool:
        return bool(self.flags & F.GF_INCOME)

    @property
    def is_hidden(self) -> bool:
        return bool(self.flags & F.GF_HIDDEN)


@dataclass(kw_only=True)
class Tag(Entity):
    key: int = 0
    name: str = ""


@dataclass(kw_only=True)
class Rule(Entity):
    """An assignment rule (<asg>)."""

    key: int = 0
    flags: int = 0
    pos: int = 0
    match_field: int = F.RULE_FIELD_MEMO  # 0 = memo, 1 = payee name
    search: str = ""
    notes: str = ""
    payee_key: int = 0
    category_key: int = 0
    paymode: int = 0
    amount: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def case_sensitive(self) -> bool:
        return bool(self.flags & F.ASGF_EXACT)

    @property
    def is_regex(self) -> bool:
        return bool(self.flags & F.ASGF_REGEX)

    @property
    def sets_category(self) -> bool:
        return bool(self.flags & (F.ASGF_DOCAT | F.ASGF_OVWCAT))

    @property
    def sets_payee(self) -> bool:
        return bool(self.flags & (F.ASGF_DOPAY | F.ASGF_OVWPAY))


@dataclass(kw_only=True)
class Split(Entity):
    category_key: int = 0
    amount: float = 0.0
    memo: str = ""


@dataclass(kw_only=True)
class Transaction(Entity):
    """A transaction (<ope>). ``index`` is its position among <ope> elements in the file."""

    index: int = 0
    date_julian: int = 0
    amount: float = 0.0
    account_key: int = 0
    xfer_amount: float = 0.0
    dst_account_key: int = 0
    paymode: int = 0
    grpflg: int = 0
    status: int = F.TXN_STATUS_NONE  # normalised to HomeBank 5.9+ meaning
    flags: int = 0
    payee_key: int = 0
    category_key: int = 0
    memo: str = ""
    info: str = ""
    tags: list[str] = field(default_factory=list)
    kxfer: int = 0
    splits: list[Split] = field(default_factory=list)

    @property
    def date(self) -> date | None:
        return julian_to_date(self.date_julian)

    @property
    def is_split(self) -> bool:
        return bool(self.splits)

    @property
    def is_income(self) -> bool:
        return bool(self.flags & F.OF_INCOME)

    @property
    def is_internal_transfer(self) -> bool:
        return bool(self.flags & F.OF_INTXFER) or self.paymode == F.OLDPAYMODE_INTXFER

    @property
    def is_void(self) -> bool:
        return self.status == F.TXN_STATUS_VOID

    @property
    def is_remind(self) -> bool:
        return bool(self.flags & F.OF_REMIND)


@dataclass(kw_only=True)
class Template(Entity):
    """A transaction template or scheduled transaction (<fav>)."""

    key: int = 0
    amount: float = 0.0
    account_key: int = 0
    xfer_amount: float = 0.0
    dst_account_key: int = 0
    paymode: int = 0
    grpflg: int = 0
    status: int = 0
    flags: int = 0
    payee_key: int = 0
    category_key: int = 0
    memo: str = ""
    info: str = ""
    tags: list[str] = field(default_factory=list)
    rec_flags: int = 0
    next_julian: int = 0
    every: int = 0
    unit: int = 0
    limit: int = 0
    weekend: int = 0
    gap: int = 0
    ordinal: int = 0
    weekday: int = 0
    splits: list[Split] = field(default_factory=list)


@dataclass(kw_only=True)
class Filter(Entity):
    """A saved filter (<flt>). Its group attributes are kept as text."""

    key: int = 0
    name: str = ""
    groups: dict[str, str] = field(default_factory=dict)


@dataclass
class Book:
    """Everything read from one .xhb file."""

    raw: bytes = field(repr=False)
    scan: ScanResult = field(repr=False)
    version: str = ""  # the v="..." attribute as written
    data_version_text: str = ""  # the d="..." attribute as written
    file_version: float = 0.0  # as HomeBank interprets v
    data_version: int = 0  # as HomeBank interprets d
    properties: Properties = field(default_factory=Properties)
    currencies: dict[int, Currency] = field(default_factory=dict)
    groups: dict[int, Group] = field(default_factory=dict)
    accounts: dict[int, Account] = field(default_factory=dict)
    payees: dict[int, Payee] = field(default_factory=dict)
    categories: dict[int, Category] = field(default_factory=dict)
    tags: dict[int, Tag] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)  # file order
    templates: list[Template] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)  # file order
    filters: list[Filter] = field(default_factory=list)
    unknown_elements: list[RawElement] = field(default_factory=list, repr=False)
    warnings: Counter = field(default_factory=Counter)  # value-free descriptions

    # --- look-ups -------------------------------------------------------------------

    def category_path(self, key: int) -> str | None:
        """Full category name as HomeBank shows it ("Parent:Child")."""
        cat = self.categories.get(key)
        if cat is None:
            return None
        parent = self.categories.get(cat.parent_key) if cat.parent_key else None
        if parent is None:
            return cat.name
        return f"{parent.name}{F.CATEGORY_SEPARATOR}{cat.name}"

    def category_by_path(self, path: str) -> Category | None:
        """Find a category by its full path, ignoring case and surrounding spaces."""
        wanted = path.strip().casefold()
        for key, cat in self.categories.items():
            full = self.category_path(key)
            if full is not None and full.casefold() == wanted:
                return cat
        return None

    def payee_name(self, key: int) -> str | None:
        pay = self.payees.get(key)
        return pay.name if pay else None

    def account_name(self, key: int) -> str | None:
        acc = self.accounts.get(key)
        return acc.name if acc else None

    def currency_for_account(self, key: int) -> Currency | None:
        acc = self.accounts.get(key)
        if acc is None:
            return None
        return self.currencies.get(acc.currency_key or self.properties.currency_key)

    def rules_in_evaluation_order(self) -> list[Rule]:
        """Rules sorted the way HomeBank applies them (by position, then key)."""
        return sorted(self.rules, key=lambda r: (r.pos, r.key))

    def sensitive_strings(self, min_length: int = 6) -> set[str]:
        """Free text from the file, used to stop it reaching the terminal."""
        out: set[str] = {self.properties.title}
        for coll in (self.accounts, self.payees, self.categories, self.tags, self.groups):
            for obj in coll.values():
                out.add(obj.name)
        for acc in self.accounts.values():
            out.update((acc.number, acc.bank_name, acc.notes, acc.website))
        for rule in self.rules:
            out.update((rule.search, rule.notes))
        for txn in self.transactions:
            out.update((txn.memo, txn.info))
            out.update(s.memo for s in txn.splits)
        for tpl in self.templates:
            out.update((tpl.memo, tpl.info))
        for flt in self.filters:
            out.add(flt.name)
        return {s for s in out if s and len(s.strip()) >= min_length}
