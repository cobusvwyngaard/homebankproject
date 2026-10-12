"""Import bank files into the HomeBank file in one go: what HomeBank's File > Import does, for many files.

Each file must be linked to its account (Imports page or import record). Per transaction,
following HomeBank 5.10.3's import (``hb_import_convert_txn``, ``transaction_add`` and
``hb_import_apply`` in hb-import.c and hb-transaction.c):

* date, amount, memo, info and payment mode as in the file (a CSV payment mode 5 becomes
  4, "bank transfer", as HomeBank's CSV import does); the income flag when the amount is
  positive; no status;
* the payee by name, ignoring case and surrounding spaces; a payee that does not exist
  is added, as HomeBank does;
* the category from the file: the prepared copy when the file was prepared (with the
  categories you chose), otherwise the toolkit's suggestion. Only categories that exist
  in the HomeBank file are used (HomeBank would create a new one);
* tags from a CSV file's tag column, when they exist in the HomeBank file;
* nothing into a closed account (HomeBank refuses those too). Split transactions and
  QIF transfers ("[Account]") are not imported as such: splits are left out (enter them
  in HomeBank), transfers come in as ordinary transactions to link on the Transfers page;
* then, as chosen (HomeBank's "Run automation" options): the payee's default category
  and payment mode for empty ones, and the assignment rules — set or overwrite payee,
  category, payment mode and tags — on the imported transactions only. (When a rule
  replaces a payee that is new, the toolkit does not add that unused payee; HomeBank
  would.)

Duplicates are left out by default and listed, as in HomeBank's import assistant:

* **already in HomeBank**: a transaction in the same account with the same amount, at
  most `days` days apart (HomeBank's "date tolerance", 0 by default). Each existing
  transaction stands for one imported transaction only, so two identical purchases on
  one day are not both hidden by one existing transaction;
* **twice in these files**: the same account, date, amount, memo, payee and info in two
  different files (two downloads that overlap). HomeBank also compares within one
  file; the toolkit does not, because a statement does not list a transaction twice.

New ``<ope>`` elements are placed after the last transaction of the same account with
the same or an earlier date (HomeBank sorts every account's transactions when it opens
a file, so the position does not change anything); new ``<pay>`` elements after the
existing payees, with the next free keys.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..errors import HbkitError
from ..privacy import Summary, timestamp
from ..rules.engine import Matcher, TextView, existing_rules
from ..xhb import format as F
from ..xhb import serialise as S
from ..xhb.model import Book, Payee, Transaction
from ..xhb.writer import XhbEditor, describe_save
from .formats import ImportFile, ImportTxn

MAX_DAYS = 10

#: Why a transaction is not imported by default (or at all).
NEW, IN_HOMEBANK, TWICE, CLOSED, SPLIT, UNREADABLE = "new", "in HomeBank", "twice", "closed", "split", "unreadable"
IMPORTABLE = (NEW, IN_HOMEBANK, TWICE)

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _fold(name: str) -> str:
    """HomeBank compares payee names with strcasecmp: ASCII letters ignore case, nothing else."""
    return name.translate(_ASCII_LOWER)


@dataclass
class Source:
    """One bank file, linked to its account."""

    name: str  # the file's name for the month (originals folder)
    account: int
    file: ImportFile  # the prepared copy when there is one, else the file itself
    prepared: bool = False
    #: categories to use where the file has none (index -> full category name): the toolkit's suggestions
    suggestions: dict[int, str] = field(default_factory=dict)


@dataclass
class Candidate:
    id: str  # "<file name>#<index>"
    source: int  # index into BulkPlan.sources
    txn: ImportTxn
    account: int
    status: str
    match: Transaction | None = None  # the transaction already in HomeBank
    payee_name: str = ""
    payee_key: int = 0  # 0 = no payee, or a new payee (payee_name not empty)
    category_key: int = 0
    category_from: str = ""  # "file", "suggested", "payee default", "rule", ""
    paymode: int = 0
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def date(self) -> date | None:
        return self.txn.date


@dataclass
class BulkPlan:
    sources: list[Source]
    candidates: list[Candidate]
    days: int
    run_rules: bool
    payee_defaults: bool
    source_sha256: str
    new_payees: list[str]  # names, in the order they would be added

    def default_ids(self) -> set[str]:
        return {c.id for c in self.candidates if c.status == NEW}

    def counts(self) -> Counter:
        return Counter(c.status for c in self.candidates)


def plan_import(book: Book, sources: list[Source], days: int = 0, run_rules: bool = True,
                payee_defaults: bool = False) -> BulkPlan:
    if not 0 <= days <= MAX_DAYS:
        raise HbkitError(f"the date tolerance must be 0 to {MAX_DAYS} days")
    payees_by_name: dict[str, int] = {}
    for key in sorted(book.payees):
        if key:
            payees_by_name.setdefault(_fold(book.payees[key].name), key)
    tag_names = {t.name for t in book.tags.values()}
    candidates: list[Candidate] = []
    new_payees: dict[str, str] = {}
    for si, src in enumerate(sources):
        acc = book.accounts.get(src.account)
        for t in src.file.txns:
            c = Candidate(f"{src.name}#{t.index}", si, t, src.account, NEW)
            candidates.append(c)
            if acc is None or acc.is_closed:
                c.status = CLOSED
                continue
            if t.date is None or t.amount is None:
                c.status = UNREADABLE
                continue
            if t.split:
                c.status = SPLIT
                continue
            c.paymode = F.PAYMODE_XFER if src.file.format == "csv" and t.paymode == F.OLDPAYMODE_INTXFER \
                else t.paymode if 0 <= t.paymode < len(F.PAYMODE_NAMES) else 0
            c.payee_name = t.payee.strip()
            if c.payee_name:
                c.payee_key = payees_by_name.get(_fold(c.payee_name), 0)
                if not c.payee_key:
                    new_payees.setdefault(_fold(c.payee_name), c.payee_name)
            path, origin = (t.category, "file") if t.category else (src.suggestions.get(t.index, ""), "suggested")
            if t.transfer:
                c.notes.append("a transfer in the file: link it on the Transfers page")
            elif path:
                cat = book.category_by_path(path)
                if cat is None:
                    c.notes.append("its category is not in HomeBank: left empty")
                else:
                    c.category_key, c.category_from = cat.key, origin
            if t.tags:
                c.tags = [x for x in t.tags.split() if x in tag_names]
                if len(c.tags) < len(t.tags.split()):
                    c.notes.append("tags that are not in HomeBank were left out")
    _mark_twice(sources, candidates)
    _mark_in_homebank(book, candidates, days)
    live = [c for c in candidates if c.status in IMPORTABLE]
    if payee_defaults:
        for c in live:
            pay = book.payees.get(c.payee_key)
            if pay is None:
                continue
            if c.category_key == 0 and pay.category_key:
                c.category_key, c.category_from = pay.category_key, "payee default"
            if c.paymode == 0:
                c.paymode = pay.paymode
    if run_rules:
        _apply_rules(book, live)
    return BulkPlan(sources, candidates, days, run_rules, payee_defaults, hashlib.sha256(book.raw).hexdigest(),
                    list(new_payees.values()))


def _mark_twice(sources: list[Source], candidates: list[Candidate]) -> None:
    first_file: dict[tuple, int] = {}
    for c in candidates:
        if c.status != NEW:
            continue
        t = c.txn
        key = (c.account, t.date, round(t.amount * 100), t.memo_as_imported, c.payee_name, t.info_as_imported)
        seen = first_file.setdefault(key, c.source)
        if seen != c.source:
            c.status = TWICE
            c.notes.append(f"also in {sources[seen].name}")


def _mark_in_homebank(book: Book, candidates: list[Candidate], days: int) -> None:
    existing: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
    for t in book.transactions:
        existing[(t.account_key, round(t.amount * 100))].append(t)
    used: set[int] = set()
    todo = sorted((c for c in candidates if c.status == NEW), key=lambda c: (c.date, c.source, c.txn.index))
    for c in todo:
        day = c.date.toordinal()
        options = [t for t in existing.get((c.account, round(c.txn.amount * 100)), ())
                   if t.index not in used and abs(t.date_julian - day) <= days]
        if not options:
            continue
        memo = " ".join(c.txn.memo.split()).casefold()
        best = min(options, key=lambda t: (abs(t.date_julian - day), " ".join(t.memo.split()).casefold() != memo,
                                           t.index))
        used.add(best.index)
        c.status, c.match = IN_HOMEBANK, best


def _apply_rules(book: Book, live: list[Candidate]) -> None:
    """HomeBank's transaction_auto_assign on the imported transactions: every rule is tested
    against the text as imported, then the matching rules act in position order."""
    rules = existing_rules(book)
    if not rules or not live:
        return
    # A payee rule tests the payee's name as HomeBank has it (a new payee: the name in the file).
    view = TextView(memo=[c.txn.memo_as_imported for c in live],
                    payee=[(book.payee_name(c.payee_key) or "") if c.payee_key else c.payee_name for c in live],
                    amount=[c.txn.amount for c in live])
    matcher = Matcher(view)
    matched: list[list[int]] = [[] for _ in live]
    for ri, rule in enumerate(rules):
        for i in matcher.indices(rule):
            matched[i].append(ri)
    by_id = {f"E{r.key}": r for r in book.rules}
    for c, rule_ids in zip(live, matched):
        for ri in rule_ids:
            sim = rules[ri]
            rule = by_id[sim.id]
            fl = rule.flags
            has_payee = bool(c.payee_key or c.payee_name)
            if ((not has_payee and fl & F.ASGF_DOPAY) or fl & F.ASGF_OVWPAY) and \
                    (rule.payee_key == 0 or rule.payee_key in book.payees):
                c.payee_key, c.payee_name = rule.payee_key, book.payee_name(rule.payee_key) or ""
            if ((c.category_key == 0 and fl & F.ASGF_DOCAT) or fl & F.ASGF_OVWCAT) and \
                    (rule.category_key == 0 or rule.category_key in book.categories):
                if c.category_key != rule.category_key:
                    c.category_from = "rule" if rule.category_key else ""
                c.category_key = rule.category_key
            if (c.paymode == 0 and fl & F.ASGF_DOMOD) or fl & F.ASGF_OVWMOD:
                c.paymode = rule.paymode
            if (not c.tags and fl & F.ASGF_DOTAG) or fl & F.ASGF_OVWTAG:
                c.tags = list(rule.tags)


# --- writing --------------------------------------------------------------------------------------


def default_output(out_root: Path) -> Path:
    stem = f"with-imports_{timestamp()}"
    output = Path(out_root) / f"{stem}.xhb"
    n = 2
    while output.exists():
        output = Path(out_root) / f"{stem}_{n}.xhb"
        n += 1
    return output


def import_transactions(book: Book, plan: BulkPlan, chosen: set[str] | None, input_path: Path,
                        output: Path | None, expected_sha256: str | None = None,
                        backup_dir: Path | None = None) -> tuple[Summary, Path | None]:
    """Add the chosen candidates (default: those not flagged) to a new copy at `output`, or
    (output None) to the file itself after a backup in `backup_dir`."""
    if expected_sha256 and expected_sha256 != plan.source_sha256:
        raise HbkitError("the HomeBank file has changed since the files were checked; check them again")
    if plan.source_sha256 != hashlib.sha256(book.raw).hexdigest():
        raise HbkitError("the import was checked against another version of the HomeBank file; check again")
    wanted = plan.default_ids() if chosen is None else set(chosen)
    picked = [c for c in plan.candidates if c.id in wanted and c.status in IMPORTABLE]
    not_importable = len(wanted - {c.id for c in picked})

    s = Summary("Import into HomeBank")
    s.count("Files", len(plan.sources))
    s.count("Transactions in the files", len(plan.candidates))
    counts = plan.counts()
    for status, label in ((IN_HOMEBANK, "Already in HomeBank (left out unless ticked)"),
                          (TWICE, "Twice in these files (left out unless ticked)"),
                          (CLOSED, "In a closed account (not imported)"),
                          (SPLIT, "Split transactions (not imported; enter them in HomeBank)"),
                          (UNREADABLE, "Without a readable date or amount (not imported)")):
        if counts.get(status):
            s.count(label, counts[status])
    if not_importable:
        s.count("Chosen but not importable (skipped)", not_importable)
    if not picked:
        s.note("Nothing to import, so no file was written.")
        return s, None

    editor = XhbEditor(book)
    payee_keys = _add_payees(book, editor, picked)
    _add_transactions(book, editor, picked, payee_keys)
    saved = editor.save_to(output, input_path, backup_dir)
    s.count("Transactions imported", len(picked))
    s.count("  with a category", sum(1 for c in picked if c.category_key))
    s.count("  by your assignment rules", sum(1 for c in picked if c.category_from == "rule"))
    s.count("New payees added", len(payee_keys))
    s.count("Transfers to link on the Transfers page", sum(1 for c in picked if c.txn.transfer))
    describe_save(s, saved, "the new transactions")
    return s, saved.path


def _add_payees(book: Book, editor: XhbEditor, picked: list[Candidate]) -> dict[str, int]:
    """Payee keys by folded name for the picked candidates, adding payees that do not exist."""
    keys: dict[str, int] = {}
    next_key = max(book.payees, default=0) + 1
    for c in picked:
        if c.payee_key or not c.payee_name:
            continue
        folded = _fold(c.payee_name)
        if folded in keys:
            continue
        keys[folded] = next_key
        editor.insert_after_section("pay", S.serialise_payee(Payee(key=next_key, name=c.payee_name)))
        next_key += 1
    return keys


def _add_transactions(book: Book, editor: XhbEditor, picked: list[Candidate], payee_keys: dict[str, int]) -> None:
    by_account: dict[int, list[Transaction]] = defaultdict(list)
    for t in book.transactions:
        by_account[t.account_key].append(t)
    last_ope = book.transactions[-1] if book.transactions else None
    for c in sorted(picked, key=lambda c: (c.account, c.date, c.source, c.txn.index)):
        day = c.date.toordinal()
        payee = c.payee_key or (payee_keys.get(_fold(c.payee_name), 0) if c.payee_name else 0)
        amount = round(c.txn.amount, 8)
        txn = Transaction(date_julian=day, amount=amount, account_key=c.account, paymode=c.paymode,
                          flags=F.OF_INCOME if amount > 0 else 0, payee_key=payee, category_key=c.category_key,
                          memo=c.txn.memo_as_imported, info=c.txn.info_as_imported, tags=list(c.tags))
        before = [t for t in by_account.get(c.account, ()) if t.date_julian <= day]
        anchor = max(before, key=lambda t: t.index) if before else None
        if anchor is not None:
            editor.insert_after(anchor, S.serialise_transaction(txn))
        elif last_ope is not None:
            editor.insert_after(last_ope, S.serialise_transaction(txn))
        else:
            editor.insert_after_section("ope", S.serialise_transaction(txn))
