"""Targeted edits to a NEW copy of a HomeBank file.

The writer never re-creates the whole file. It copies the original bytes and
splices in only the intended changes (insert, replace or remove an element), so
everything else stays byte for byte identical — including HomeBank's formatting,
attributes this toolkit does not know, and the file's line endings.

New elements are formatted by hbkit.xhb.serialise exactly as HomeBank 5.10.3 would.

Before saving, the result is read back and compared element by element with what
was intended; the save is refused if anything else differs. The input file is
never overwritten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import WriteRefusedError
from . import format as F
from . import serialise as S
from .model import Book, Entity, Rule
from .scanner import RawElement, scan


@dataclass
class _Edit:
    start: int
    end: int
    new: list[str]  # element texts to put in place of bytes[start:end] (or insert)
    kind: str  # "insert", "replace" or "remove"
    target: RawElement | None = None  # element replaced/removed, or the anchor of an insert
    seq: int = 0


@dataclass
class SaveResult:
    path: Path
    inserted: int = 0
    replaced: int = 0
    removed: int = 0
    added_lines: list[str] = field(default_factory=list, repr=False)


class XhbEditor:
    """Collects edits to one book and writes them to a new file."""

    def __init__(self, book: Book):
        if not book.scan.utf8_valid:
            raise WriteRefusedError(
                "the file is not valid UTF-8, so the toolkit will not write a modified copy of it"
            )
        self.book = book
        nl = book.scan.newline
        if nl == "mixed":
            nl = "\r\n" if book.raw.count(b"\r\n") * 2 > book.raw.count(b"\n") else "\n"
        self.newline = "\n" if nl == "none" else nl
        self._edits: list[_Edit] = []
        self._touched: set[int] = set()  # start offsets of replaced/removed elements
        self._new_rules: list[Rule] = []

    # --- rules -----------------------------------------------------------------------------

    def next_rule_key(self) -> int:
        keys = [r.key for r in self.book.rules] + [r.key for r in self._new_rules]
        return max(keys, default=0) + 1

    def next_rule_pos(self) -> int:
        positions = [r.pos for r in self.book.rules] + [r.pos for r in self._new_rules]
        return max(positions, default=0) + 1

    def add_rule(self, rule: Rule) -> Rule:
        """Add an assignment rule after the existing ones (key and position assigned if 0)."""
        if not rule.search:
            raise WriteRefusedError("a rule needs a search text")
        if rule.category_key and rule.category_key not in self.book.categories:
            raise WriteRefusedError("a rule points to a category that does not exist in the file")
        if rule.payee_key and rule.payee_key not in self.book.payees:
            raise WriteRefusedError("a rule points to a payee that does not exist in the file")
        if not rule.key:
            rule.key = self.next_rule_key()
        if not rule.pos:
            rule.pos = self.next_rule_pos()
        if any(r.key == rule.key for r in self.book.rules + self._new_rules):
            raise WriteRefusedError("a rule with this key already exists")
        self._new_rules.append(rule)
        self._insert_after_section("asg", S.serialise_rule(rule))
        return rule

    # --- generic edits --------------------------------------------------------------------

    def replace(self, entity: Entity, changes: dict[str, str | None]) -> None:
        """Change attributes of an existing element, keeping the order of the others.

        A value of None removes the attribute; new attributes are added at the end.
        """
        el = self._existing(entity)
        attrs = dict(el.attrs)
        for name, value in changes.items():
            if value is None:
                attrs.pop(name, None)
            else:
                attrs[name] = value
        text = S.serialise_attributes(el.name, attrs.items(), S.CRLF_ATTRIBUTES.get(el.name, ()))
        self._add(_Edit(el.start, el.end, [text], "replace", el))

    def remove(self, entity: Entity) -> None:
        """Remove an element together with its line ending."""
        el = self._existing(entity)
        end = el.end
        nl = self.newline.encode()
        if self.book.raw.startswith(nl, end):
            end += len(nl)
        self._add(_Edit(el.start, end, [], "remove", el))

    def _existing(self, entity: Entity) -> RawElement:
        el = entity.src
        if el is None:
            raise WriteRefusedError("only elements read from the file can be replaced or removed")
        if el.start in self._touched:
            raise WriteRefusedError("an element can only be changed once per copy")
        self._touched.add(el.start)
        return el

    def _insert_after_section(self, name: str, text: str) -> None:
        """Insert after the last element of `name`, or where HomeBank would put it."""
        rank = F.SAVE_ORDER.index(name)
        top = [el for el in self.book.scan.elements if el.depth == 1]
        before = [el for el in top if el.name in F.SAVE_ORDER and F.SAVE_ORDER.index(el.name) <= rank]
        anchor = max(before, key=lambda e: e.end) if before else self.book.scan.root
        self._add(_Edit(anchor.end, anchor.end, [text], "insert", anchor))

    def _add(self, edit: _Edit) -> None:
        edit.seq = len(self._edits)
        self._edits.append(edit)

    # --- output ----------------------------------------------------------------------------

    @property
    def has_changes(self) -> bool:
        return bool(self._edits)

    def render(self) -> bytes:
        raw = self.book.raw
        edits = sorted(self._edits, key=lambda e: (e.start, e.seq))
        out: list[bytes] = []
        pos = 0
        nl = self.newline
        for e in edits:
            if e.start < pos:
                raise WriteRefusedError("overlapping edits")
            out.append(raw[pos:e.start])
            if e.kind == "insert":
                out.append("".join(nl + t for t in e.new).encode("utf-8"))
            else:
                out.append("".join(e.new).encode("utf-8"))
            pos = e.end
        out.append(raw[pos:])
        return b"".join(out)

    def _expected_elements(self) -> list[tuple[str, dict[str, str]]]:
        replaced = {e.target.start: e for e in self._edits if e.kind in ("replace", "remove")}
        inserts: dict[int, list[_Edit]] = {}
        for e in self._edits:
            if e.kind == "insert":
                inserts.setdefault(e.target.start, []).append(e)
        expected = []
        for el in self.book.scan.elements:
            edit = replaced.get(el.start)
            if edit is None:
                expected.append((el.name, el.attrs))
            elif edit.kind == "replace":
                expected += [(x.name, x.attrs) for x in scan(edit.new[0].encode()).elements]
            for ins in sorted(inserts.get(el.start, []), key=lambda e: e.seq):
                for text in ins.new:
                    expected += [(x.name, x.attrs) for x in scan(text.encode()).elements]
        return expected

    def verify(self, data: bytes) -> None:
        """Read the result back and check it differs from the input only as intended."""
        actual = [(el.name, el.attrs) for el in scan(data).elements]
        if actual != self._expected_elements():
            raise WriteRefusedError(
                "the modified copy did not match the intended changes, so it was not saved"
            )

    def save(self, output: str | Path, input_path: str | Path | None = None,
             overwrite: bool = False) -> SaveResult:
        """Write the modified copy to `output`. Never writes over the input file."""
        output = Path(output)
        if input_path is not None and _same_file(Path(input_path), output):
            raise WriteRefusedError("the output file must not be the input file")
        if output.exists() and not overwrite:
            raise WriteRefusedError("the output file already exists (choose another name)")
        data = self.render()
        self.verify(data)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(output.name + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, output)
        finally:
            if tmp.exists():
                tmp.unlink()
        result = SaveResult(path=output)
        for e in self._edits:
            if e.kind == "insert":
                result.inserted += len(e.new)
            elif e.kind == "replace":
                result.replaced += 1
            else:
                result.removed += 1
            result.added_lines += e.new
        return result


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
