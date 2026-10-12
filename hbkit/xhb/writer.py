"""Targeted edits to a HomeBank file, saved as a new copy or (when the owner chooses) into the file itself.

The writer never re-creates the whole file. It copies the original bytes and
splices in only the intended changes (insert, replace or remove an element), so
everything else stays byte for byte identical — including HomeBank's formatting,
attributes this toolkit does not know, and the file's line endings.

New elements are formatted by hbkit.xhb.serialise exactly as HomeBank 5.10.3 would.

Before saving, the result is read back and compared element by element with what
was intended; the save is refused if anything else differs.

By default the result is a new copy and the input file is never overwritten. The owner
can choose to change the file itself (`save_in_place`): then the file must still be
exactly the one that was read, HomeBank must not be running (it would write its own
version over the change when it saves), a backup of the file as it was is written
first, and the file is replaced in one step (a temporary file renamed over it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
    #: when the file itself was changed: the copy of the file as it was before
    backup: Path | None = None

    @property
    def in_place(self) -> bool:
        return self.backup is not None


def homebank_running() -> bool | None:
    """Whether HomeBank runs on this computer; None when that cannot be told."""
    try:
        if os.name == "nt":
            res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq homebank.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if res.returncode != 0:
                return None
            return "homebank.exe" in res.stdout.casefold()
        res = subprocess.run(["pgrep", "-i", "-x", "homebank"], capture_output=True, timeout=15)
        return {0: True, 1: False}.get(res.returncode)
    except (OSError, subprocess.SubprocessError):
        return None


HOMEBANK_RUNNING = ("HomeBank is running. Close it before the toolkit changes your file itself (when HomeBank "
                    "saves, it writes its own version over the file); nothing was changed")


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

    def replace(self, entity: Entity, changes: dict[str, str | None],
                order: tuple[str, ...] = ()) -> None:
        """Change attributes of an existing element, keeping the order of the others.

        A value of None removes the attribute. A new attribute goes where HomeBank would
        write it when `order` (HomeBank's attribute order) names it, otherwise at the end.
        """
        el = self._existing(entity)
        items = [(k, v) for k, v in el.attrs.items() if changes.get(k, v) is not None]
        items = [(k, changes.get(k, v)) for k, v in items]
        rank = {name: i for i, name in enumerate(order)}
        for name, value in changes.items():
            if value is None or name in el.attrs:
                continue
            pos = len(items)
            if name in rank:
                later = [i for i, (k, _) in enumerate(items) if rank.get(k, -1) > rank[name]]
                pos = later[0] if later else len(items)
            items.insert(pos, (name, value))
        text = S.serialise_attributes(el.name, items, S.CRLF_ATTRIBUTES.get(el.name, ()))
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

    def insert_after(self, entity: Entity, text: str) -> None:
        """Insert a new element right after an element read from the file. Several
        inserts after the same element keep the order in which they were added."""
        el = entity.src
        if el is None or el.depth != 1:
            raise WriteRefusedError("new elements can only follow an element read from the file")
        self._add(_Edit(el.end, el.end, [text], "insert", el))

    def insert_after_section(self, name: str, text: str) -> None:
        """Insert after the last element of `name`, or where HomeBank would put it."""
        self._insert_after_section(name, text)

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
        _write_replace(output, data)
        return self._result(output)

    def save_in_place(self, input_path: str | Path, backup_dir: str | Path) -> SaveResult:
        """Change the file itself, after writing a copy of it as it was into `backup_dir`.

        Refused (nothing written) when the file on disk is no longer the one that was
        read, or when HomeBank is running.
        """
        from ..privacy import timestamp

        input_path = Path(input_path)
        data = self.render()
        self.verify(data)
        try:
            current = input_path.read_bytes()
        except OSError:
            raise WriteRefusedError("the HomeBank file could not be read again, so it was not changed") from None
        if current != self.book.raw:
            raise WriteRefusedError("the HomeBank file has changed since the toolkit read it (saved in HomeBank?); "
                                    "nothing was changed. Load the page again and repeat the step")
        if homebank_running():
            raise WriteRefusedError(HOMEBANK_RUNNING)
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{input_path.stem}_before_{timestamp()}"
        backup = backup_dir / f"{stem}.xhb"
        n = 2
        while backup.exists():
            backup = backup_dir / f"{stem}_{n}.xhb"
            n += 1
        _write_replace(backup, current)
        if backup.read_bytes() != current:
            raise WriteRefusedError("the backup could not be written correctly, so the file was not changed")
        try:
            _write_replace(input_path, data, mode_from=input_path)
        except OSError:
            raise WriteRefusedError("the HomeBank file could not be replaced (is it open in another program?); "
                                    "it was not changed") from None
        if input_path.read_bytes() != data:
            raise WriteRefusedError("the HomeBank file was not written correctly; put the backup back in its place")
        result = self._result(input_path)
        result.backup = backup
        return result

    def save_to(self, output: str | Path | None, input_path: str | Path,
                backup_dir: str | Path | None = None) -> SaveResult:
        """A new copy at `output`, or (output None) the file itself with a backup in `backup_dir`."""
        if output is not None:
            return self.save(output, input_path=input_path)
        if backup_dir is None:
            raise WriteRefusedError("a backup folder is needed to change the file itself")
        return self.save_in_place(input_path, backup_dir)

    def _result(self, path: Path) -> SaveResult:
        result = SaveResult(path=path)
        for e in self._edits:
            if e.kind == "insert":
                result.inserted += len(e.new)
            elif e.kind == "replace":
                result.replaced += 1
            else:
                result.removed += 1
            result.added_lines += e.new
        return result


def _write_replace(target: Path, data: bytes, mode_from: Path | None = None) -> None:
    """Write `data` to a temporary file next to `target`, then rename it over `target`."""
    tmp = target.with_name(target.name + ".hbkit-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode_from is not None:
            try:
                shutil.copymode(mode_from, tmp)
            except OSError:
                pass
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def describe_save(s, saved: SaveResult, check: str) -> None:
    """The output lines and notes of a Summary after saving (`check`: what to look at in HomeBank)."""
    if saved.in_place:
        s.output("Changed file", saved.path)
        s.output("Backup of the file as it was", saved.backup)
        s.note(f"Your HomeBank file itself was changed. Open it in HomeBank and check {check}.")
        s.note("To undo, close HomeBank and put the backup in its place (rename it to the file's name).")
    else:
        s.output("New copy", saved.path)
        s.note(f"Your original file was not changed. Open the new copy in HomeBank, check {check},")
        s.note("and use the copy once you are happy with it (keep the original as a backup).")


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
