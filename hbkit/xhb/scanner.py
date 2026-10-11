"""A byte-exact scanner for HomeBank .xhb files.

Why not xml.etree? Three reasons:

* The writer needs the exact byte span of every element so that it can copy the
  original file unchanged and splice in only the intended edits.
* HomeBank reads its files with GLib's GMarkup parser, not a strict XML 1.0
  parser. GMarkup accepts character references such as ``&#x1;`` (which HomeBank
  itself writes for control characters, and which xml.etree rejects) and does not
  normalise whitespace inside attribute values. This scanner follows GMarkup.
* Error messages must never contain values from the file.

The .xhb format is flat: one ``<homebank>`` root whose children are empty
elements carrying attributes. The scanner nevertheless tracks nesting so that an
unexpected structure is detected rather than misread.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field

from ..errors import XhbFormatError
from .format import KNOWN_ATTRIBUTES

_NAME_RE = re.compile(rb"[A-Za-z_:][-A-Za-z0-9_:.]*")
_WS = b" \t\r\n"
_ENTITY_RE = re.compile(r"&(?:#x([0-9A-Fa-f]+)|#([0-9]+)|(amp|lt|gt|quot|apos));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(slots=True)
class RawElement:
    """One start tag as it appears in the file."""

    name: str
    attrs: dict[str, str]  # in file order, unescaped
    start: int  # byte offset of "<"
    end: int  # byte offset just after ">"
    line: int
    depth: int  # 0 for the root element
    self_closing: bool


@dataclass
class ScanResult:
    elements: list[RawElement]
    root: RawElement | None
    root_close: tuple[int, int] | None  # span of "</homebank>"
    utf8_valid: bool
    has_bom: bool
    newline: str  # "\n", "\r\n", "mixed" or "none"
    issues: Counter = field(default_factory=Counter)  # value-free descriptions


def safe_name(element: str, attribute: str | None = None) -> str:
    """Describe an element/attribute for messages, hiding names we do not know."""
    if element not in KNOWN_ATTRIBUTES:
        return "an unknown element"
    if attribute is None:
        return f"<{element}>"
    if attribute in KNOWN_ATTRIBUTES[element]:
        return f"<{element} {attribute}>"
    return f"an unknown attribute of <{element}>"


class _Lines:
    def __init__(self, data: bytes):
        self._nl = [m.start() for m in re.finditer(b"\n", data)]

    def line_of(self, offset: int) -> int:
        return bisect_left(self._nl, offset) + 1


def unescape(text: str, issues: Counter | None = None, where: str = "") -> str:
    """Decode entities the way GMarkup does (character references of any code point)."""
    if "&" not in text:
        return text

    def repl(m: re.Match) -> str:
        hex_, dec, named = m.groups()
        if named:
            return _NAMED[named]
        try:
            return chr(int(hex_, 16) if hex_ else int(dec))
        except (ValueError, OverflowError):
            if issues is not None:
                issues[f"invalid character reference in {where}"] += 1
            return m.group(0)

    out = _ENTITY_RE.sub(repl, text)
    if issues is not None and "&" in _ENTITY_RE.sub("", text):
        issues[f"unknown entity or bare '&' in {where}"] += 1
    return out


def _detect_newline(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "\r\n"
    if lf:
        return "\n"
    return "none"


def scan(data: bytes) -> ScanResult:
    """Scan the bytes of an .xhb file into raw elements with exact spans."""
    has_bom = data.startswith(_UTF8_BOM)
    try:
        data.decode("utf-8")
        utf8_valid = True
    except UnicodeDecodeError:
        utf8_valid = False
    # HomeBank converts non-UTF-8 files from the system code page; on Windows that
    # is usually cp1252, so use it as the fallback for reading values.
    encoding = "utf-8" if utf8_valid else "cp1252"

    lines = _Lines(data)
    issues: Counter = Counter()
    elements: list[RawElement] = []
    stack: list[RawElement] = []
    root: RawElement | None = None
    root_close: tuple[int, int] | None = None
    n = len(data)
    i = 3 if has_bom else 0

    def fail(message: str, offset: int) -> XhbFormatError:
        return XhbFormatError(message, lines.line_of(offset))

    while True:
        lt = data.find(b"<", i)
        stop = n if lt < 0 else lt
        if data[i:stop].strip(_WS):
            issues["text outside elements"] += 1
        if lt < 0:
            break

        if data.startswith(b"<?", lt):
            j = data.find(b"?>", lt + 2)
            if j < 0:
                raise fail("unterminated processing instruction", lt)
            i = j + 2
            continue
        if data.startswith(b"<!--", lt):
            j = data.find(b"-->", lt + 4)
            if j < 0:
                raise fail("unterminated comment", lt)
            issues["comments"] += 1
            i = j + 3
            continue
        if data.startswith(b"<![CDATA[", lt):
            j = data.find(b"]]>", lt + 9)
            if j < 0:
                raise fail("unterminated CDATA section", lt)
            issues["CDATA sections"] += 1
            i = j + 3
            continue
        if data.startswith(b"<!", lt):
            j = data.find(b">", lt + 2)
            if j < 0:
                raise fail("unterminated declaration", lt)
            i = j + 1
            continue

        if data.startswith(b"</", lt):
            j = data.find(b">", lt + 2)
            if j < 0:
                raise fail("unterminated closing tag", lt)
            name = data[lt + 2 : j].strip(_WS).decode(encoding, "replace")
            if not stack or stack[-1].name != name:
                raise fail("closing tag does not match the open element", lt)
            closed = stack.pop()
            if closed is root:
                root_close = (lt, j + 1)
            i = j + 1
            continue

        m = _NAME_RE.match(data, lt + 1)
        if not m:
            raise fail("malformed tag", lt)
        name = m.group(0).decode("ascii")
        p = m.end()
        attrs: dict[str, str] = {}
        while True:
            while p < n and data[p] in _WS:
                p += 1
            if data.startswith(b"/>", p):
                self_closing = True
                p += 2
                break
            if data.startswith(b">", p):
                self_closing = False
                p += 1
                break
            am = _NAME_RE.match(data, p)
            if not am:
                raise fail(f"malformed attribute in {safe_name(name)}", lt)
            aname = am.group(0).decode("ascii")
            p = am.end()
            while p < n and data[p] in _WS:
                p += 1
            if not data.startswith(b"=", p):
                raise fail(f"attribute without value in {safe_name(name)}", lt)
            p += 1
            while p < n and data[p] in _WS:
                p += 1
            quote = data[p : p + 1]
            if quote not in (b'"', b"'"):
                raise fail(f"unquoted attribute value in {safe_name(name)}", lt)
            close = data.find(quote, p + 1)
            if close < 0:
                raise fail(f"unterminated attribute value in {safe_name(name)}", lt)
            raw = data[p + 1 : close].decode(encoding, "replace")
            value = unescape(raw, issues, safe_name(name, aname))
            if aname in attrs:
                issues[f"duplicate attribute in {safe_name(name)}"] += 1
            else:
                attrs[aname] = value
            p = close + 1

        el = RawElement(name, attrs, lt, p, lines.line_of(lt), len(stack), self_closing)
        if el.depth == 0:
            if root is None:
                root = el
            else:
                issues["more than one root element"] += 1
        elif el.depth > 1:
            issues["nested elements below the top level"] += 1
        elements.append(el)
        if not self_closing:
            stack.append(el)
        i = p

    if stack:
        raise XhbFormatError(f"{safe_name(stack[-1].name)} is never closed", stack[-1].line)
    if root is None:
        raise XhbFormatError("no root element found; is this a HomeBank file?")

    return ScanResult(
        elements=elements,
        root=root,
        root_close=root_close,
        utf8_valid=utf8_valid,
        has_bom=has_bom,
        newline=_detect_newline(data),
        issues=issues,
    )
