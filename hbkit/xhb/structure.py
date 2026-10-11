"""The structure report: the shape of a HomeBank file without any of its values.

It shows the file and data version, encoding, line endings, element counts, and
for every attribute how many elements carry it. HomeBank leaves out attributes
that are empty or zero, so "present" is a good measure of how often a field is
filled. Names the toolkit does not know are marked so the reader can be adjusted.
"""

from __future__ import annotations

import re
from collections import Counter

from .. import __version__
from . import format as F
from .model import Book

_PRINTABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,40}$")


_VERSION = re.compile(r"^[0-9][0-9.]{0,11}$")


def _name(n: str) -> str:
    return n if _PRINTABLE_NAME.match(n) else "(unprintable name)"


def _version(v: str) -> str:
    if not v:
        return "(missing)"
    return v if _VERSION.match(v) else "(not a number)"


def _homebank_release(d: int) -> str:
    if d <= 0:
        return "unknown"
    return f"{d // 10000}.{(d // 100) % 100}.{d % 100}"


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def structure_report(book: Book) -> list[str]:
    sr = book.scan
    lines = [
        "HomeBank file structure report",
        "==============================",
        "No values from the file are shown: only versions, names of elements and",
        "attributes, and counts.",
        "",
        f"Toolkit version {__version__}, checked against HomeBank 5.10.3 "
        f"(file version {F.KNOWN_FILE_VERSION}).",
        "",
        f"File version (v)      {_version(book.version)}",
        f"Data version (d)      {_version(book.data_version_text)}"
        f"  -> written by HomeBank {_homebank_release(book.data_version)}",
        f"Encoding              {'UTF-8' if sr.utf8_valid else 'NOT valid UTF-8 (read as Windows-1252)'}"
        f"{', with byte-order mark' if sr.has_bom else ''}",
        f"Line endings          "
        + {"\n": "LF (Unix style)", "\r\n": "CRLF (Windows style)", "mixed": "mixed LF and CRLF",
           "none": "none"}[sr.newline],
        f"Size                  {_fmt(len(book.raw))} bytes, {_fmt(book.raw.count(b'\n'))} lines",
    ]

    top = [el for el in sr.elements if el.depth == 1]
    order = [el.name for el in top if el.name in F.SAVE_ORDER]
    ranks = [F.SAVE_ORDER.index(n) for n in order]
    in_order = all(a <= b for a, b in zip(ranks, ranks[1:]))
    lines.append(f"Element order         {'as HomeBank saves it' if in_order else 'DIFFERENT from HomeBank save order'}")

    counts: Counter = Counter(el.name for el in top)
    present: dict[str, Counter] = {}
    for el in top:
        c = present.setdefault(el.name, Counter())
        for aname, value in el.attrs.items():
            if value != "":
                c[aname] += 1

    lines += ["", "Elements", "--------"]
    names = [n for n in F.SAVE_ORDER if n in counts] + sorted(n for n in counts if n not in F.SAVE_ORDER)
    for name in names:
        known = "" if name in F.KNOWN_ATTRIBUTES else "   * not known to this toolkit"
        lines.append(f"  {_name(name):<12} {_fmt(counts[name]):>10}{known}")
    missing = [n for n in F.SAVE_ORDER if n not in counts]
    if missing:
        lines.append(f"  (none present: {', '.join(missing)})")

    lines += ["", "Attributes (how many elements carry each one; * = not known to this toolkit)",
              "----------"]
    root_attrs = ", ".join(_name(a) for a in sr.root.attrs) if sr.root else ""
    lines.append(f"  <homebank>  attributes: {root_attrs or '(none)'}")
    for name in names:
        total = counts[name]
        lines.append(f"  <{_name(name)}>  {_fmt(total)} element{'s' if total != 1 else ''}")
        known = F.KNOWN_ATTRIBUTES.get(name, ())
        order = [a for a in known if a in present[name]] + sorted(a for a in present[name] if a not in known)
        for aname in order:
            n = present[name][aname]
            mark = "" if aname in known else " *"
            lines.append(f"      {_name(aname) + mark:<16} {_fmt(n):>10}  {100.0 * n / total:6.1f}%")

    lines += ["", "Checks", "------"]
    txns = book.transactions
    checks = [
        ("transactions", len(txns)),
        ("  with a memo", sum(1 for t in txns if t.memo)),
        ("  with a payee", sum(1 for t in txns if t.payee_key)),
        ("  with a category (not split)", sum(1 for t in txns if t.category_key and not t.splits)),
        ("  split", sum(1 for t in txns if t.splits)),
        ("  internal transfers", sum(1 for t in txns if t.is_internal_transfer)),
        ("  void", sum(1 for t in txns if t.is_void)),
        ("  with an unreadable date", sum(1 for t in txns if t.date is None)),
        ("  pointing to a missing category", sum(
            1 for t in txns if t.category_key and t.category_key not in book.categories)),
        ("assignment rules", len(book.rules)),
        ("  matching on memo", sum(1 for r in book.rules if r.match_field == F.RULE_FIELD_MEMO)),
        ("  matching on payee", sum(1 for r in book.rules if r.match_field == F.RULE_FIELD_PAYEE)),
        ("  regular expressions", sum(1 for r in book.rules if r.is_regex)),
        ("categories", len(book.categories)),
        ("  subcategories", sum(1 for c in book.categories.values() if c.parent_key)),
    ]
    for label, n in checks:
        lines.append(f"  {label:<36} {_fmt(n):>10}")

    lines += ["", "Reader warnings", "---------------"]
    if book.warnings:
        for text, n in sorted(book.warnings.items()):
            lines.append(f"  {text}: {_fmt(n)}")
    else:
        lines.append("  none")
    return lines
