"""Write elements exactly the way HomeBank 5.10.3 does (homebank_save_xml_* in hb-xml.c).

Each ``serialise_*`` function returns one element as text without the line ending.
The rules copied from HomeBank:

* integers are left out when 0 (hb_xml_append_int), except where HomeBank
  always writes them (hb_xml_append_int0, and the fixed <cur>/<grp>/<tag> formats);
* text is left out when empty and escaped with GLib's g_markup_escape_text;
  account and payee notes also escape line breaks (hb_xml_append_txt_crlf);
* amounts are always written, with g_ascii_dtostr, i.e. C's "%.17g"
  (so 9.72 becomes "9.7200000000000006");
* split categories and amounts are joined with "||" and not escaped; split memos
  are joined with "||" and escaped as a whole.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import format as F
from .model import (
    Account, Category, Currency, Entity, Filter, Group, Payee, Properties, Rule, Split, Tag,
    Template, Transaction,
)

TF_LIMIT = 1 << 1  # hb-template.h
TF_RELATIVE = 1 << 2


def _should_escape(c: int, crlf: bool) -> bool:
    if crlf:
        low = 0x1 <= c <= 0x8 or 0xA <= c <= 0xD or 0xE <= c <= 0x1F
    else:
        low = 0x1 <= c <= 0x8 or 0xB <= c <= 0xC or 0xE <= c <= 0x1F
    return low or 0x7F <= c <= 0x84 or 0x86 <= c <= 0x9F


def escape(text: str, crlf: bool = False) -> str:
    """g_markup_escape_text(), or HomeBank's variant that also escapes CR and LF."""
    out = []
    for ch in text:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == "'":
            out.append("&apos;")
        elif ch == '"':
            out.append("&quot;")
        elif _should_escape(ord(ch), crlf):
            out.append(f"&#x{ord(ch):x};")
        else:
            out.append(ch)
    return "".join(out)


def dtostr(value: float) -> str:
    """g_ascii_dtostr(): "%.17g" with a '.' decimal point whatever the locale."""
    return "%.17g" % value


class _Node:
    def __init__(self, name: str):
        self.parts = [f"<{name}"]

    def txt(self, name: str, value: str | None) -> None:
        if value:
            self.parts.append(f' {name}="{escape(value)}"')

    def txt_crlf(self, name: str, value: str | None) -> None:
        if value:
            self.parts.append(f' {name}="{escape(value, crlf=True)}"')

    def int0(self, name: str, value: int) -> None:
        self.parts.append(f' {name}="{int(value)}"')

    def int(self, name: str, value: int) -> None:
        if value != 0:
            self.int0(name, value)

    def amt(self, name: str, value: float) -> None:
        self.parts.append(f' {name}="{dtostr(value)}"')

    def raw(self, name: str, value: str) -> None:
        self.parts.append(f' {name}="{value}"')

    def extra(self, entity: Entity) -> None:
        # HomeBank would drop attributes it does not know; we keep them.
        for name, value in entity.extra.items():
            self.txt_always(name, value)

    def txt_always(self, name: str, value: str) -> None:
        self.parts.append(f' {name}="{escape(value)}"')

    def splits(self, splits: list[Split]) -> None:
        if not splits:
            return
        self.raw("scat", "||".join(str(int(s.category_key)) for s in splits))
        self.raw("samt", "||".join(dtostr(s.amount) for s in splits))
        self.raw("smem", escape("||".join(s.memo for s in splits)))

    def done(self) -> str:
        return "".join(self.parts) + "/>"


def serialise_root(file_version: str = "1.6", data_version: int = F.KNOWN_DATA_VERSION) -> str:
    """The <homebank ...> opening line (homebank_save_xml_ver)."""
    return f'<homebank v="{file_version}" d="{data_version:06d}">'


def serialise_properties(p: Properties) -> str:
    n = _Node("properties")
    n.txt("title", p.title)
    n.int("curr", p.currency_key)
    n.int("car_category", p.vehicle_category_key)
    n.int0("auto_smode", p.auto_smode)
    n.int("auto_weekday", p.auto_weekday)
    n.int("auto_nbmonths", p.auto_nbmonths)
    n.int("auto_nbdays", p.auto_nbdays)
    n.amt("earnbyh", p.earn_by_hour)
    n.extra(p)
    return n.done()


def serialise_currency(c: Currency) -> str:
    # g_markup_printf_escaped with a fixed format: every attribute always present.
    text = (
        f'<cur key="{c.key}" flags="{c.flags}" iso="{escape(c.iso)}" name="{escape(c.name)}"'
        f' symb="{escape(c.symbol)}" syprf="{c.symbol_prefix}" dchar="{escape(c.decimal_char)}"'
        f' gchar="{escape(c.grouping_char)}" frac="{c.frac_digits}" rate="{dtostr(c.rate)}"'
        f' mdate="{c.mdate}"'
    )
    n = _Node("x")
    n.parts = [text]
    n.extra(c)
    return n.done()


def serialise_group(g: Group) -> str:
    n = _Node("x")
    n.parts = [f'<grp key="{g.key}" name="{escape(g.name)}"']
    n.extra(g)
    return n.done()


def serialise_account(a: Account) -> str:
    n = _Node("account")
    n.int("key", a.key)
    n.int("flags", a.flags)
    n.int("pos", a.pos)
    n.int("type", a.type)
    n.int("curr", a.currency_key)
    n.txt("name", a.name)
    n.txt("number", a.number)
    n.txt("bankname", a.bank_name)
    n.amt("initial", a.initial)
    n.amt("minimum", a.minimum)
    n.amt("maximum", a.maximum)
    n.int("cheque1", a.cheque1)
    n.int("cheque2", a.cheque2)
    n.txt("website", a.website)
    n.txt_crlf("notes", a.notes)
    n.int("tpl", a.template_key)
    n.int("grp", a.group_key)
    n.int("ccday", a.cc_day)
    n.int("rdate", a.reconciled_julian)
    n.extra(a)
    return n.done()


def serialise_payee(p: Payee) -> str:
    n = _Node("pay")
    n.int("key", p.key)
    n.int("flags", p.flags)
    n.txt("name", p.name)
    n.int("category", p.category_key)
    n.int("paymode", p.paymode)
    n.txt_crlf("notes", p.notes)
    n.extra(p)
    return n.done()


def serialise_category(c: Category) -> str:
    n = _Node("cat")
    n.int("key", c.key)
    n.int("parent", c.parent_key)
    n.int("flags", c.flags)
    n.txt("name", c.name)
    for i in range(13):
        value = c.budget.get(i, 0.0)
        if value != 0:
            n.raw(f"b{i}", dtostr(value))
    n.extra(c)
    return n.done()


def serialise_tag(t: Tag) -> str:
    n = _Node("x")
    n.parts = [f'<tag key="{t.key}" name="{escape(t.name)}"']
    n.extra(t)
    return n.done()


def serialise_rule(r: Rule) -> str:
    n = _Node("asg")
    n.int("key", r.key)
    n.int("flags", r.flags & ~F.ASGF_PREFILLED)  # HomeBank clears this flag on save
    n.int("pos", r.pos)
    n.int("field", r.match_field)
    n.txt("name", r.search)
    n.txt("notes", r.notes)
    n.int("payee", r.payee_key)
    n.int("category", r.category_key)
    n.int("paymode", r.paymode)
    n.amt("amount", r.amount)
    n.txt("tags", " ".join(r.tags))
    n.extra(r)
    return n.done()


def serialise_template(t: Template) -> str:
    n = _Node("fav")
    n.int("key", t.key)
    n.amt("amount", t.amount)
    n.int("account", t.account_key)
    if t.flags & F.OF_ADVXFER:
        n.amt("damt", t.xfer_amount)
    n.int("dst_account", t.dst_account_key)
    n.int("paymode", t.paymode)
    n.int("grpflg", t.grpflg)
    n.int("st", t.status)
    n.int("flags", t.flags)
    n.int("payee", t.payee_key)
    n.int("category", t.category_key)
    n.txt("wording", t.memo)
    n.txt("info", t.info)
    n.txt("tags", " ".join(t.tags))
    n.int("recflg", t.rec_flags)
    n.int("nextdate", t.next_julian)
    n.int("every", t.every)
    n.int("unit", t.unit)
    if t.rec_flags & TF_LIMIT:
        n.int("limit", t.limit)
    n.int("weekend", t.weekend)
    n.int("gap", t.gap)
    if t.rec_flags & TF_RELATIVE:
        n.int("ordn", t.ordinal)
        n.int("wkdy", t.weekday)
    n.splits(t.splits)
    n.extra(t)
    return n.done()


#: The order in which serialise_transaction (HomeBank's save) writes <ope> attributes.
OPE_ATTRIBUTE_ORDER = ("date", "amount", "account", "damt", "dst_account", "paymode", "grpflg", "st", "flags",
                       "payee", "category", "wording", "info", "tags", "kxfer", "scat", "samt", "smem")


def serialise_transaction(t: Transaction) -> str:
    n = _Node("ope")
    n.int("date", t.date_julian)
    n.amt("amount", t.amount)
    n.int("account", t.account_key)
    if t.flags & F.OF_ADVXFER:
        n.amt("damt", t.xfer_amount)
    n.int("dst_account", t.dst_account_key)
    n.int("paymode", t.paymode)
    n.int("grpflg", t.grpflg)
    n.int("st", t.status)
    n.int("flags", t.flags)
    n.int("payee", t.payee_key)
    n.int("category", t.category_key)
    n.txt("wording", t.memo)
    n.txt("info", t.info)
    n.txt("tags", " ".join(t.tags))
    n.int("kxfer", t.kxfer)
    n.splits(t.splits)
    n.extra(t)
    return n.done()


def serialise_filter(f: Filter) -> str:
    n = _Node("flt")
    n.int("key", f.key)
    for g in ("dat", "acc", "pay", "cat", "tag", "txt", "amt", "mod", "sta", "typ"):
        n.txt(g, f.groups.get(g))
    n.txt("name", f.name)
    n.extra(f)
    return n.done()


def serialise_attributes(name: str, attrs: Iterable[tuple[str, str]], crlf: Iterable[str] = ()) -> str:
    """Generic form used when an existing element is edited: keeps attribute order."""
    crlf_set = set(crlf)
    parts = [f"<{name}"]
    for aname, value in attrs:
        parts.append(f' {aname}="{escape(value, crlf=aname in crlf_set)}"')
    return "".join(parts) + "/>"


#: Attributes HomeBank writes with the CR/LF-escaping variant, per element.
CRLF_ATTRIBUTES = {"account": ("notes",), "pay": ("notes",)}
