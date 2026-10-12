"""Link transfers between your own accounts after importing bank files.

HomeBank pairs transfers by itself only when it imports OFX files. Transfers that
arrive from QIF or CSV files are ordinary transactions in two accounts: one with money
out, one with the same amount in, a few days apart, usually without a category. This
module finds those pairs and links them in a NEW copy of the HomeBank file, the way
HomeBank does when you link a transfer by hand (``transaction_xfer_change_to_child`` in
hb-transaction.c): both get the internal-transfer flag (8), the other account in
``dst_account`` and a shared transfer number ``kxfer`` one above the highest in use.
Nothing else changes; the payment mode stays as it is.

Which transactions are considered: not void, not split, not already a transfer,
without a category, in open accounts (HomeBank refuses transfers with a closed
account), with an amount.

* **Pairs**: money out in one account and exactly the same amount in another account
  with the same currency, at most ``days`` apart, where neither has any other possible
  partner.
* **Choices**: groups where a transaction has more than one possible partner; you
  choose.
* **Without another side**: transactions that look like transfers (payment mode
  "bank transfer", a description naming another of your accounts, or described like
  earlier transfers) but have no partner. They are only listed.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import HbkitError
from ..privacy import Summary, timestamp
from ..rules.normalise import normalise
from ..xhb import format as F
from ..xhb import serialise as S
from ..xhb.model import Book, Transaction
from ..xhb.writer import XhbEditor, describe_save

DEFAULT_DAYS = 4
MAX_DAYS = 31


def _cents(amount: float) -> int:
    return round(amount * 100)


def _text(book: Book, t: Transaction) -> str:
    return " ".join(x for x in (t.memo, book.payee_name(t.payee_key) or "", t.info) if x).casefold()


@dataclass
class Pair:
    out: Transaction  # money leaves this account
    into: Transaction  # and arrives in this one
    gap: int  # days apart
    signs: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return self.out.index, self.into.index


@dataclass
class Lonely:
    """A transaction that looks like a transfer but has no other side."""

    txn: Transaction
    reasons: list[str]
    likely_account: int = 0


@dataclass
class TransferResult:
    days: int
    considered: int
    pairs: list[Pair]
    choices: list[list[Pair]]
    lonely: list[Lonely]
    source_sha256: str

    def all_pairs(self) -> dict[tuple[int, int], Pair]:
        return {p.key: p for p in self.pairs + [p for group in self.choices for p in group]}


class _Clues:
    """What the file already says about transfers: account names, earlier transfers."""

    def __init__(self, book: Book):
        self.book = book
        self.names: dict[int, list[str]] = {}
        for acc in book.accounts.values():
            words = [w.casefold() for w in (acc.name, acc.number) if w and len(w.strip()) >= 4]
            self.names[acc.key] = words
        self.linked: Counter = Counter()  # frozenset(account, account) -> transfers already linked
        self.described: dict[tuple[int, str], Counter] = defaultdict(Counter)  # (account, pattern) -> other accounts
        for t in book.transactions:
            if t.is_internal_transfer and t.dst_account_key and not t.is_void:
                if t.amount < 0:
                    self.linked[frozenset((t.account_key, t.dst_account_key))] += 1
                pattern = normalise(t.memo)
                if pattern:
                    self.described[(t.account_key, pattern)][t.dst_account_key] += 1

    def mentions(self, t: Transaction) -> list[int]:
        """Other accounts whose name or number appears in this transaction's text."""
        text = _text(self.book, t)
        return [acc for acc, words in self.names.items()
                if acc != t.account_key and any(w in text for w in words)]

    def earlier(self, t: Transaction) -> Counter:
        return self.described.get((t.account_key, normalise(t.memo)), Counter())


def _eligible(book: Book, t: Transaction) -> bool:
    acc = book.accounts.get(t.account_key)
    return (acc is not None and not acc.is_closed and t.date is not None and not t.is_void
            and not t.is_split and not t.is_internal_transfer and t.category_key == 0
            and _cents(t.amount) != 0 and t.kxfer == 0)


def find_transfers(book: Book, days: int = DEFAULT_DAYS) -> TransferResult:
    if not 0 <= days <= MAX_DAYS:
        raise HbkitError(f"the number of days must be between 0 and {MAX_DAYS}")
    clues = _Clues(book)
    cands = [t for t in book.transactions if _eligible(book, t)]
    incoming: dict[int, list[Transaction]] = defaultdict(list)
    for t in cands:
        if t.amount > 0:
            incoming[_cents(t.amount)].append(t)
    currency = {a.key: a.currency_key for a in book.accounts.values()}
    edges: list[Pair] = []
    for t in cands:
        if t.amount >= 0:
            continue
        for u in incoming.get(-_cents(t.amount), []):
            gap = abs(t.date_julian - u.date_julian)
            if u.account_key != t.account_key and gap <= days and currency[u.account_key] == currency[t.account_key]:
                edges.append(Pair(t, u, gap))

    # Group transactions connected by possible pairs (union-find on transaction index).
    parent: dict[int, int] = {}

    def root(i: int) -> int:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in edges:
        parent[root(e.out.index)] = root(e.into.index)
    groups: dict[int, list[Pair]] = defaultdict(list)
    for e in edges:
        groups[root(e.out.index)].append(e)
    for e in edges:
        e.signs = _signs(book, clues, e)
    pairs, choices = [], []
    for group in groups.values():
        if len(group) == 1:
            pairs.append(group[0])
        else:
            group.sort(key=lambda p: (p.out.date_julian, p.out.index, -len(p.signs), p.gap))
            choices.append(group)
    pairs.sort(key=lambda p: (p.out.date_julian, p.out.index))
    choices.sort(key=lambda g: (g[0].out.date_julian, g[0].out.index))

    paired = {i for e in edges for i in e.key}
    lonely = []
    for t in cands:
        if t.index in paired:
            continue
        reasons, likely = [], 0
        if t.paymode in (F.PAYMODE_XFER, F.OLDPAYMODE_INTXFER):
            reasons.append("payment mode is a transfer")
        named = clues.mentions(t)
        if named:
            reasons.append("its text names another of your accounts")
            likely = named[0]
        earlier = clues.earlier(t)
        if earlier:
            reasons.append("described like earlier transfers in this account")
            likely = likely or earlier.most_common(1)[0][0]
        if reasons:
            lonely.append(Lonely(t, reasons, likely))
    lonely.sort(key=lambda x: (x.txn.date_julian, x.txn.index))
    return TransferResult(days, len(cands), pairs, choices, lonely, hashlib.sha256(book.raw).hexdigest())


def _signs(book: Book, clues: _Clues, p: Pair) -> list[str]:
    signs = []
    if p.into.account_key in clues.mentions(p.out) or p.out.account_key in clues.mentions(p.into):
        signs.append("the text names the other account")
    n = clues.linked.get(frozenset((p.out.account_key, p.into.account_key)), 0)
    if n:
        signs.append(f"{n} earlier transfer{'s' if n != 1 else ''} between these accounts")
    if clues.earlier(p.out).get(p.into.account_key) or clues.earlier(p.into).get(p.out.account_key):
        signs.append("described like earlier transfers")
    if F.PAYMODE_XFER in (p.out.paymode, p.into.paymode):
        signs.append("payment mode is a transfer")
    return signs


# --- linking ----------------------------------------------------------------------------------------


def default_output(out_root: Path) -> Path:
    stem = f"with-transfers_{timestamp()}"
    output = Path(out_root) / f"{stem}.xhb"
    n = 2
    while output.exists():
        output = Path(out_root) / f"{stem}_{n}.xhb"
        n += 1
    return output


def link_transfers(book: Book, chosen: list[tuple[int, int]], days: int, input_path: Path, output: Path | None,
                   expected_sha256: str | None = None, backup_dir: Path | None = None) -> tuple[Summary, Path | None]:
    """Link the chosen pairs (out index, in index) in a new copy at `output`, or (output
    None) in the file itself after a backup in `backup_dir`.

    The pairs are checked again against the file as it is now: each must still be a
    possible transfer, and no transaction may be used twice.
    """
    result = find_transfers(book, days)
    if expected_sha256 and expected_sha256 != result.source_sha256:
        raise HbkitError("the HomeBank file has changed since the transfers were listed; list them again")
    possible = result.all_pairs()
    used: set[int] = set()
    skipped: Counter = Counter()
    pairs: list[Pair] = []
    for key in dict.fromkeys(tuple(int(x) for x in k) for k in chosen):
        pair = possible.get(key)
        if pair is None:
            skipped["no longer a possible transfer"] += 1
        elif used & set(key):
            skipped["a transaction was chosen twice"] += 1
        else:
            used.update(key)
            pairs.append(pair)

    s = Summary("Link transfers")
    s.count("Pairs chosen", len(chosen))
    for reason, n in sorted(skipped.items()):
        s.count(f"Skipped: {reason}", n)
    if not pairs:
        s.note("No transfers to link, so no file was written.")
        return s, None
    editor = XhbEditor(book)
    kxfer = max((t.kxfer for t in book.transactions), default=0)
    for p in pairs:
        kxfer += 1
        for this, other in ((p.out, p.into), (p.into, p.out)):
            editor.replace(this, {"flags": str(this.flags | F.OF_INTXFER), "dst_account": str(other.account_key),
                                  "kxfer": str(kxfer)}, order=S.OPE_ATTRIBUTE_ORDER)
    saved = editor.save_to(output, input_path, backup_dir)
    s.count("Transfers linked", len(pairs))
    s.count("Transactions changed", saved.replaced)
    describe_save(s, saved, "the transfers")
    return s, saved.path
