"""Simulate how HomeBank applies assignment rules (transaction_auto_assign in hb-assign.c).

* Rules are applied in order of position; every rule is tested.
* A rule matches when its search text occurs in the transaction's text
  (case-insensitive unless the rule is case-sensitive), or, for a regex rule, when
  the regular expression finds a non-empty match. With the amount flag the amount
  must also be equal.
* The text is the memo, or the payee's name for payee rules. A transaction without
  a payee has HomeBank's built-in "no payee" (empty name); only a payee key that
  points nowhere leaves the memo.
* "Set category" fills an empty category, so the first matching such rule wins;
  "overwrite category" always replaces it, so the last matching such rule wins.

Regular expressions are evaluated with Python's ``re`` module. HomeBank uses PCRE;
the two agree on ordinary patterns but can differ on advanced syntax.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..xhb import format as F
from ..xhb.model import Book, Rule, Transaction


@dataclass
class SimRule:
    """A rule as the simulator sees it."""

    id: str  # "E12" for existing rule key 12, "N0003" for a new candidate
    search: str
    flags: int
    match_field: int = F.RULE_FIELD_MEMO
    category_key: int = 0
    payee_key: int = 0
    amount: float = 0.0
    source: str = "existing"  # or "new"
    error: str | None = None
    _regex: re.Pattern | None = field(default=None, repr=False)

    @classmethod
    def from_rule(cls, rule: Rule) -> SimRule:
        return cls(id=f"E{rule.key}", search=rule.search, flags=rule.flags, match_field=rule.match_field,
                   category_key=rule.category_key, payee_key=rule.payee_key, amount=rule.amount)

    def __post_init__(self) -> None:
        if self.flags & F.ASGF_REGEX:
            try:
                self._regex = re.compile(self.search, 0 if self.flags & F.ASGF_EXACT else re.IGNORECASE)
            except re.error:
                self.error = "invalid regular expression"

    @property
    def sets_category(self) -> bool:
        return bool(self.flags & (F.ASGF_DOCAT | F.ASGF_OVWCAT))

    @property
    def overwrites_category(self) -> bool:
        return bool(self.flags & F.ASGF_OVWCAT)

    @property
    def cache_key(self) -> tuple:
        amount = self.amount if self.flags & F.ASGF_AMOUNT else None
        return (self.search, self.flags & (F.ASGF_EXACT | F.ASGF_REGEX | F.ASGF_AMOUNT), self.match_field, amount)


@dataclass
class TextView:
    """The texts and amount rules are tested against, for a list of transactions."""

    memo: list[str]
    payee: list[str]
    amount: list[float]
    memo_cf: list[str] = field(default_factory=list)
    payee_cf: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.memo_cf = [t.casefold() for t in self.memo]
        self.payee_cf = [t.casefold() for t in self.payee]

    @classmethod
    def build(cls, book: Book, txns: Sequence[Transaction]) -> TextView:
        payee = []
        for t in txns:
            if t.payee_key == 0:
                payee.append("")
            elif t.payee_key in book.payees:
                payee.append(book.payees[t.payee_key].name)
            else:
                payee.append(t.memo)
        return cls(memo=[t.memo for t in txns], payee=payee, amount=[t.amount for t in txns])

    def __len__(self) -> int:
        return len(self.memo)


def _regex_matches(rx: re.Pattern, text: str) -> bool:
    m = rx.search(text)
    if m is None:
        return False
    if m.end() > m.start():
        return True
    return any(x.end() > x.start() for x in rx.finditer(text))  # G_REGEX_MATCH_NOTEMPTY


class Matcher:
    """Finds which transactions each rule matches, with caching."""

    def __init__(self, view: TextView):
        self.view = view
        self._cache: dict[tuple, list[int]] = {}

    def indices(self, rule: SimRule) -> list[int]:
        key = rule.cache_key
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        v = self.view
        if rule.error or not rule.search:
            result: list[int] = []
        elif rule.flags & F.ASGF_REGEX:
            texts = v.payee if rule.match_field == F.RULE_FIELD_PAYEE else v.memo
            rx = rule._regex
            result = [i for i, t in enumerate(texts) if t and _regex_matches(rx, t)]
        elif rule.flags & F.ASGF_EXACT:
            texts = v.payee if rule.match_field == F.RULE_FIELD_PAYEE else v.memo
            needle = rule.search
            result = [i for i, t in enumerate(texts) if needle in t]
        else:
            texts = v.payee_cf if rule.match_field == F.RULE_FIELD_PAYEE else v.memo_cf
            needle = rule.search.casefold()
            result = [i for i, t in enumerate(texts) if needle in t]
        if rule.flags & F.ASGF_AMOUNT:
            result = [i for i in result if v.amount[i] == rule.amount]
        self._cache[key] = result
        return result


@dataclass
class Simulation:
    """The outcome of applying an ordered list of rules to every transaction."""

    rules: list[SimRule]
    matched: list[list[int]]  # per transaction: indices into `rules`, in order
    category: list[int]  # per transaction: category the rules would give (0 = none)
    decided_by: list[int | None]  # per transaction: index of the rule that set the category


def simulate(rules: Sequence[SimRule], matcher: Matcher, start_category: Sequence[int] | None = None) -> Simulation:
    """Apply `rules` (already in evaluation order) to every transaction of the matcher's view.

    By default every transaction starts uncategorised, as for a fresh import.
    """
    n = len(matcher.view)
    matched: list[list[int]] = [[] for _ in range(n)]
    for ri, rule in enumerate(rules):
        for i in matcher.indices(rule):
            matched[i].append(ri)
    category = list(start_category) if start_category is not None else [0] * n
    decided: list[int | None] = [None] * n
    for i, rule_ids in enumerate(matched):
        cat = category[i]
        for ri in rule_ids:
            r = rules[ri]
            if (cat == 0 and r.flags & F.ASGF_DOCAT) or r.flags & F.ASGF_OVWCAT:
                cat = r.category_key
                decided[i] = ri
        category[i] = cat
    return Simulation(list(rules), matched, category, decided)


def existing_rules(book: Book) -> list[SimRule]:
    """The file's rules in HomeBank's evaluation order."""
    return [SimRule.from_rule(r) for r in book.rules_in_evaluation_order()]
