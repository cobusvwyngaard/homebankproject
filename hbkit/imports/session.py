"""One month of import files: where they are kept, which accounts matter, and preparing them.

Files are kept in the output folder, ``data/out/imports/<YYYY-MM>/``: the files you add
in ``originals/`` and the prepared copies in ``prepared/`` (``<name>-prepared.<ext>``).
Originals are never changed. What was linked to which account is recorded in
``data/import-accounts.json`` (see memory.py).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..errors import HbkitError
from ..xhb.model import Book
from .formats import ImportFile, read_import_file, with_categories
from .memory import ImportMemory, check_month
from .predict import History, clear_winner, guess_accounts, predict

IMPORTS = "imports"
MAX_FILE = 10 * 1024 * 1024
#: An account is "active" for a month when it has transactions in it or in the months just before.
ACTIVE_MONTHS = 3


def month_dir(out_root: Path, month: str) -> Path:
    return Path(out_root) / IMPORTS / check_month(month)


def safe_name(name: str) -> str:
    """A file name that is safe to store: no folders, no characters Windows refuses."""
    base = re.split(r"[\\/]", str(name))[-1]
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    if not base:
        raise HbkitError("the file has no usable name")
    stem, dot, ext = base.rpartition(".")
    if dot and len(base) > 120:
        base = stem[: 120 - len(ext) - 1] + "." + ext
    return base[:120]


def prepared_name(name: str) -> str:
    """<name>-prepared.<ext>; a PDF statement is prepared as HomeBank CSV, so .csv."""
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem):
        return f"{name}-prepared"
    return f"{stem}-prepared.{'csv' if ext.lower() == 'pdf' else ext}"


def account_by_number(book: Book, numbers: list[str]) -> int | None:
    """The account whose number (digits only, at least 6) appears in a statement, if exactly one."""
    found = {acc.key for acc in book.accounts.values()
             if len(digits := re.sub(r"\D", "", acc.number or "")) >= 6 and digits in numbers}
    return found.pop() if len(found) == 1 else None


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def add_file(out_root: Path, month: str, name: str, raw: bytes) -> Path:
    """Keep a copy of a file you add. The same content under the same name is kept once."""
    if len(raw) > MAX_FILE:
        raise HbkitError("that file is too large for an import file")
    read_import_file(name, raw)  # refuse files HomeBank could not import
    folder = month_dir(out_root, month) / "originals"
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(name)
    target = folder / name
    n = 2
    while target.exists() and target.read_bytes() != raw:
        stem, dot, ext = name.rpartition(".")
        target = folder / (f"{stem} ({n}).{ext}" if dot and stem else f"{name} ({n})")
        n += 1
    target.write_bytes(raw)
    return target


def original(out_root: Path, month: str, name: str) -> Path:
    path = month_dir(out_root, month) / "originals" / safe_name(name)
    if not path.is_file():
        raise HbkitError("that file is not among this month's files")
    return path


def month_files(out_root: Path, month: str) -> list[Path]:
    folder = month_dir(out_root, month) / "originals"
    return sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.name.casefold()) \
        if folder.is_dir() else []


def _months_before(month: str, n: int) -> list[str]:
    y, m = int(month[:4]), int(month[5:])
    out = []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y:04d}-{m:02d}")
    return out


def month_accounts(book: Book, history: History, memory: ImportMemory, month: str) -> list[dict]:
    """Every account with what matters for this month's imports."""
    check_month(month)
    window = [month] + _months_before(month, ACTIVE_MONTHS)
    rows = []
    for acc in sorted(book.accounts.values(), key=lambda a: (a.pos, a.key)):
        counts = history.per_month.get(acc.key, {})
        active = any(counts.get(m) for m in window)
        expected = acc.key in memory.expected_accounts if memory.expected_accounts is not None \
            else active and not acc.is_closed
        last = history.last_date.get(acc.key)
        rows.append({
            "key": acc.key, "name": acc.name, "type": acc.type_name, "closed": acc.is_closed,
            "last_date": last.isoformat() if last else "", "in_month": counts.get(month, 0),
            "active": active, "expected": expected,
        })
    return rows


@dataclass
class FileInfo:
    name: str
    format: str
    transactions: int
    first: date | None
    last: date | None
    problems: list[int]
    account: int | None  # linked (recorded) account
    suggested: int | None  # suggested account when not linked yet
    reason: str
    warning: str
    prepared: str
    sha256: str


def file_info(book: Book, history: History, memory: ImportMemory, month: str, path: Path) -> FileInfo:
    raw = path.read_bytes()
    f = read_import_file(path.name, raw)
    link = memory.get(month, path.name)
    guesses = guess_accounts(history, f)
    best = clear_winner(guesses)
    account, suggested, reason, warning = None, None, "", ""
    if link:
        account = int(link["account"])
        reason = "linked by you"
    else:
        remembered = memory.predict(path.name, month)
        by_number = account_by_number(book, f.numbers) if f.numbers else None
        if by_number is not None:
            suggested, reason = by_number, "its account number matches this account in HomeBank"
        elif remembered and remembered[0] in book.accounts:
            suggested, reason = remembered
        elif best:
            suggested, reason = best.account, "from its contents: " + "; ".join(best.reasons)
        elif guesses:
            reason = "not sure from its contents; choose the account"
        else:
            reason = "nothing like it in HomeBank yet; choose the account"
    chosen = account or suggested
    if chosen and best and best.account != chosen and best.duplicates:
        warning = (f"its contents look like {book.account_name(best.account) or 'another account'}: "
                   + "; ".join(best.reasons))
    prepared_path = path.parent.parent / "prepared" / prepared_name(path.name)
    return FileInfo(path.name, f.format, len(f.txns), f.first_date, f.last_date, f.problems, account,
                    suggested, reason, warning, str(prepared_path) if prepared_path.is_file() else "",
                    sha256(raw))


def preview(book: Book, history: History, f: ImportFile, account: int) -> list[dict]:
    """Per transaction: what is in the file, the suggestion and what HomeBank would do."""
    if account not in book.accounts:
        raise HbkitError("that account does not exist in the HomeBank file")
    def cat(key: int) -> str:
        return (book.category_path(key) or "") if key else ""

    rows = []
    for t, p in zip(f.txns, predict(history, account, f)):
        rows.append({
            "index": t.index, "line": t.line, "date": t.date.isoformat() if t.date else "",
            "amount": t.amount, "memo": t.memo, "payee": t.payee, "in_file": t.category,
            "suggested": cat(p.category), "source": p.source, "evidence": p.evidence,
            "homebank": cat(p.homebank_category), "homebank_by": p.homebank_by,
            "overwritten_by": p.overwritten_by, "overwrite_category": cat(p.overwrite_category),
            "duplicate": p.duplicate, "skip": p.skip,
        })
    return rows


def prepare(book: Book, f: ImportFile, choices: dict[int, str]) -> tuple[bytes, int]:
    """The file with the chosen categories (index -> full category name) written into it.

    Categories must exist in the HomeBank file (HomeBank would otherwise create a new
    one); they are written with HomeBank's own spelling. Empty choices are left out.
    """
    resolved = {}
    for index, name in choices.items():
        if not str(name).strip():
            continue
        category = book.category_by_path(str(name))
        if category is None:
            raise HbkitError(f"the category for transaction {int(index) + 1} does not exist in the HomeBank file")
        resolved[int(index)] = book.category_path(category.key)
    return with_categories(f, resolved), len(resolved)


def save_prepared(original_path: Path, data: bytes) -> Path:
    target = original_path.parent.parent / "prepared" / prepared_name(original_path.name)
    if target.resolve() == original_path.resolve():
        raise HbkitError("the prepared file must not replace the original")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target

