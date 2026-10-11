"""Categories for an import file, predicted per account from your HomeBank history.

HomeBank's rules cannot look at the account (see docs/xhb-format.md), but the toolkit
knows which account a file belongs to. For each transaction in the file:

1. Its description is cleaned as in rule mining (``normalise``): the memo, or the
   payee when there is no memo.
2. **This account's recent history.** The latest ``RECENT`` categorised transactions
   with the same cleaned description *in this account*. When at least ``MIN_RECENT``
   exist and ``RECENT_SHARE`` of them share one category, that category is predicted.
   Only recent ones count, so a change of habit is followed.
3. Otherwise **all accounts**: when the description is consistent over every account
   (at least 5 transactions, 95% in one category, the test for a suggested rule).
4. **What HomeBank will do on import** is simulated as well: a payee's default category
   ("auto from payee") and your assignment rules. HomeBank keeps a category from the
   file; only an "overwrite category" rule replaces it, which is flagged.

Transactions already in HomeBank (same account, date, amount and description) are
flagged as possible duplicates; HomeBank's import checks for them too.

``guess_accounts`` ranks the accounts a file probably belongs to, from its contents.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..rules.engine import Matcher, SimRule, TextView, existing_rules, simulate
from ..rules.mining import MiningParams
from ..rules.normalise import normalise
from ..xhb import format as F
from ..xhb.model import Book
from .formats import ImportFile

RECENT = 10
MIN_RECENT = 3
RECENT_SHARE = 0.8
#: An amount within this fraction of the account's latest amount for the description counts as close.
CLOSE_AMOUNT = 0.1


def description_key(memo: str, payee: str) -> str:
    key = normalise(memo)
    if key:
        return key
    key = normalise(payee)
    return f"payee: {key}" if key else ""


def duplicate_key(when: date, amount: float, memo: str, payee: str) -> tuple:
    """Same date, amount and exact description (ignoring case and spacing): already imported.

    The exact text, not the cleaned one: cleaning removes the policy or loan numbers
    that tell two accounts' otherwise identical transactions apart.
    """
    return when, round(amount * 100), exact_text(memo, payee)


def exact_text(memo: str, payee: str) -> str:
    return " ".join((memo or payee).split()).casefold()


@dataclass
class Past:
    date: date
    amount: float
    category: int


class History:
    """The HomeBank file's transactions, per account and cleaned description."""

    def __init__(self, book: Book):
        self.book = book
        self.by_account: dict[int, dict[str, list[Past]]] = defaultdict(lambda: defaultdict(list))
        self.everywhere: dict[str, Counter] = defaultdict(Counter)
        self.accounts_using: dict[str, set[int]] = defaultdict(set)
        self.exact_accounts: dict[str, set[int]] = defaultdict(set)
        self.seen: dict[int, Counter] = defaultdict(Counter)
        self.last_date: dict[int, date] = {}
        self.per_month: dict[int, Counter] = defaultdict(Counter)
        for t in book.transactions:
            d = t.date
            if t.is_void or d is None:
                continue
            acc = t.account_key
            key = description_key(t.memo, book.payee_name(t.payee_key) or "")
            self.last_date[acc] = max(d, self.last_date.get(acc, d))
            self.per_month[acc][f"{d.year:04d}-{d.month:02d}"] += 1
            payee = book.payee_name(t.payee_key) or ""
            self.seen[acc][duplicate_key(d, t.amount, t.memo, payee)] += 1
            self.exact_accounts[exact_text(t.memo, payee)].add(acc)
            if t.is_split or t.is_internal_transfer or not key:
                continue
            cat = t.category_key if t.category_key in book.categories else 0
            self.by_account[acc][key].append(Past(d, t.amount, cat))
            self.accounts_using[key].add(acc)
            if cat:
                self.everywhere[key][cat] += 1
        for per_key in self.by_account.values():
            for past in per_key.values():
                past.sort(key=lambda p: p.date)

    def account_category(self, account: int, key: str) -> tuple[int, int, int] | None:
        """(category, agreeing, looked at) from this account's recent history, if clear."""
        recent = [p.category for p in self.by_account.get(account, {}).get(key, []) if p.category][-RECENT:]
        if len(recent) < MIN_RECENT:
            return None
        cat, top = Counter(recent).most_common(1)[0]
        return (cat, top, len(recent)) if top / len(recent) >= RECENT_SHARE else None

    def general_category(self, key: str) -> tuple[int, int, int] | None:
        """(category, agreeing, total) over all accounts, when consistent enough for a rule."""
        counts = self.everywhere.get(key)
        if not counts:
            return None
        p = MiningParams()
        n = sum(counts.values())
        cat, top = counts.most_common(1)[0]
        return (cat, top, n) if n >= p.min_count and top / n >= p.min_share else None

    def latest_amount(self, account: int, key: str) -> float | None:
        past = self.by_account.get(account, {}).get(key)
        return past[-1].amount if past else None


@dataclass
class Prediction:
    index: int
    key: str
    category: int = 0  # suggested category key (0 = none)
    source: str = ""  # "this account", "all accounts" or ""
    evidence: str = ""  # e.g. "6 of the last 6 in this account"
    homebank_category: int = 0  # what HomeBank would set on import if the file has none
    homebank_by: str = ""  # e.g. "rule E12", "payee default"
    overwritten_by: str = ""  # an overwrite rule that would replace a category from the file
    overwrite_category: int = 0
    duplicate: bool = False
    skip: str = ""  # why no category can be written: transfer, split, already has one


def predict(history: History, account: int, f: ImportFile,
            rules: list[SimRule] | None = None) -> list[Prediction]:
    book = history.book
    rules = existing_rules(book) if rules is None else rules
    view = TextView(memo=[t.memo for t in f.txns], payee=[t.payee for t in f.txns],
                    amount=[t.amount or 0.0 for t in f.txns])
    payee_cat = []
    by_name = {p.name: p for p in book.payees.values()}
    for t in f.txns:
        payee = by_name.get(t.payee)
        payee_cat.append(payee.category_key if payee and payee.category_key in book.categories else 0)
    sim = simulate(rules, Matcher(view), payee_cat)
    seen = Counter(history.seen.get(account, Counter()))
    out = []
    for t in f.txns:
        key = description_key(t.memo, t.payee)
        p = Prediction(t.index, key)
        dup = duplicate_key(t.date, t.amount, t.memo, t.payee) if t.date and t.amount is not None else None
        if dup is not None and seen[dup] > 0:
            seen[dup] -= 1
            p.duplicate = True
        p.homebank_category = sim.category[t.index]
        decided = sim.decided_by[t.index]
        if decided is not None:
            p.homebank_by = f"rule {sim.rules[decided].id}"
        elif payee_cat[t.index]:
            p.homebank_by = "payee default"
        overwrite = [sim.rules[i] for i in sim.matched[t.index] if sim.rules[i].flags & F.ASGF_OVWCAT]
        if overwrite:
            p.overwritten_by = overwrite[-1].id
            p.overwrite_category = overwrite[-1].category_key
        if t.transfer:
            p.skip = "transfer"
        elif t.split:
            p.skip = "split"
        elif t.category:
            p.skip = "already has a category"
        elif key:
            hit = history.account_category(account, key)
            if hit:
                p.category, p.source = hit[0], "this account"
                p.evidence = f"{hit[1]} of the last {hit[2]} in this account"
            else:
                hit = history.general_category(key)
                if hit:
                    p.category, p.source = hit[0], "all accounts"
                    p.evidence = f"{hit[1]} of {hit[2]} in all accounts"
        out.append(p)
    return out


@dataclass
class AccountGuess:
    account: int
    score: float
    duplicates: int = 0
    exact: int = 0
    known: int = 0
    close: int = 0
    reasons: list[str] = field(default_factory=list)


def guess_accounts(history: History, f: ImportFile, accounts: list[int] | None = None) -> list[AccountGuess]:
    """Accounts the file probably belongs to, best first, judged by its contents.

    Per transaction of the file, points go to the accounts that:
    * already have it (same date, amount and description: overlapping statements): 2;
    * have its exact description: 1, shared by the accounts that have it;
    * otherwise have its cleaned description: 0.5, shared likewise;
    * have an amount close to their latest one for that description (e.g. this month's
      interest on the same loan): 1, shared by the accounts whose amount is close.
    The score is the average per transaction.
    """
    rows = [(t, description_key(t.memo, t.payee)) for t in f.txns]
    rows = [(t, k) for t, k in rows if k]
    if not rows:
        return []
    wanted = set(history.book.accounts if accounts is None else accounts)
    guesses: dict[int, AccountGuess] = {}

    def give(acc: int, points: float) -> AccountGuess | None:
        if acc not in wanted:
            return None
        g = guesses.setdefault(acc, AccountGuess(acc, 0.0))
        g.score += points
        return g

    for t, key in rows:
        if t.date and t.amount is not None:
            dup = duplicate_key(t.date, t.amount, t.memo, t.payee)
            for acc in wanted:
                if history.seen.get(acc, {}).get(dup):
                    give(acc, 2).duplicates += 1
        exact = history.exact_accounts.get(exact_text(t.memo, t.payee), set()) & wanted
        similar = history.accounts_using.get(key, set()) & wanted
        for acc in exact:
            give(acc, 1 / len(exact)).exact += 1
        for acc in similar - exact if not exact else ():
            give(acc, 0.5 / len(similar)).known += 1
        if t.amount is not None:
            close = []
            for acc in similar:
                last = history.latest_amount(acc, key)
                if last and (t.amount > 0) == (last > 0) and abs(t.amount - last) <= CLOSE_AMOUNT * abs(last):
                    close.append(acc)
            for acc in close:
                give(acc, 1 / len(close)).close += 1
    n = len(rows)
    out = []
    for g in guesses.values():
        g.score /= n
        if g.duplicates:
            g.reasons.append(f"{g.duplicates} of its transactions are already in this account")
        if g.exact:
            g.reasons.append(f"{g.exact} of {n} descriptions exactly as in this account")
        if g.known:
            g.reasons.append(f"{g.known} of {n} descriptions like this account's")
        if g.close:
            g.reasons.append(f"{g.close} amounts close to this account's latest ones")
        out.append(g)
    out.sort(key=lambda g: (-g.score, g.account))
    return out


def clear_winner(guesses: list[AccountGuess]) -> AccountGuess | None:
    """The best guess when it is clearly better than the next one."""
    if not guesses or guesses[0].score < 0.5:
        return None
    if len(guesses) > 1 and guesses[0].score < 1.5 * guesses[1].score:
        return None
    return guesses[0]
