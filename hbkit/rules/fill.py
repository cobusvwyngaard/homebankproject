"""Fill empty categories with your assignment rules, in a new copy of the file.

HomeBank's own "Auto. assignments" works one account at a time, runs every rule (an
"overwrite category" rule also replaces categories you chose yourself) and shows no
preview. This fills only transactions **without** a category, with the rules you choose,
deciding between rules the way HomeBank does (``transaction_auto_assign`` in
hb-assign.c): rules in order of position; for "set category" the first matching rule
wins, for "overwrite category" the last. Only the category is written; what the rules
would do to payees, payment modes or tags is not applied.

Considered: not void, not split, not an internal transfer, category empty, and not in a
closed account (HomeBank does not run assignments there either). Rules whose category
no longer exists, or whose regular expression is invalid, are left out.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..errors import HbkitError
from ..privacy import Summary, timestamp
from ..xhb import format as F
from ..xhb import serialise as S
from ..xhb.model import Book, Transaction
from ..xhb.writer import XhbEditor, describe_save
from .engine import Matcher, SimRule, TextView, existing_rules


@dataclass
class FillPlan:
    rules: list[SimRule]  # rules that can set a category, in HomeBank's order
    positions: dict[str, int]  # rule id -> position (1 = first)
    txns: list[Transaction]  # transactions without a category that may be filled
    matched: list[list[int]]  # per transaction: indices into `rules` of the rules that match, in order
    left_out_rules: Counter  # reason -> number of rules
    closed_skipped: int  # uncategorised transactions in closed accounts
    source_sha256: str

    def matches(self) -> Counter:
        """Rule id -> transactions without a category that the rule matches."""
        out: Counter = Counter()
        for rule_ids in self.matched:
            for ri in rule_ids:
                out[self.rules[ri].id] += 1
        return out

    def decide(self, selected: set[str]) -> dict[int, tuple[SimRule, int]]:
        """Transaction position in `txns` -> (deciding rule, category), with only these rules."""
        out = {}
        for i, rule_ids in enumerate(self.matched):
            cat, decider = 0, None
            for ri in rule_ids:
                r = self.rules[ri]
                if r.id not in selected:
                    continue
                if (cat == 0 and r.flags & F.ASGF_DOCAT) or r.flags & F.ASGF_OVWCAT:
                    cat, decider = r.category_key, r
            if decider is not None:
                out[i] = (decider, cat)
        return out


def plan_fill(book: Book) -> FillPlan:
    left_out: Counter = Counter()
    rules, positions = [], {}
    for pos, r in enumerate(existing_rules(book), start=1):
        positions[r.id] = pos
        if not r.sets_category:
            continue
        if r.error:
            left_out["invalid regular expression"] += 1
        elif r.category_key not in book.categories:
            left_out["its category no longer exists"] += 1
        else:
            rules.append(r)
    txns, closed = [], 0
    for t in book.transactions:
        if t.category_key != 0 or t.is_void or t.is_split or t.is_internal_transfer:
            continue
        acc = book.accounts.get(t.account_key)
        if acc is None or acc.is_closed:
            closed += 1
            continue
        txns.append(t)
    matcher = Matcher(TextView.build(book, txns))
    matched: list[list[int]] = [[] for _ in txns]
    for ri, r in enumerate(rules):
        for i in matcher.indices(r):
            matched[i].append(ri)
    return FillPlan(rules, positions, txns, matched, left_out, closed, hashlib.sha256(book.raw).hexdigest())


def default_output(out_root: Path) -> Path:
    stem = f"with-categories_{timestamp()}"
    output = Path(out_root) / f"{stem}.xhb"
    n = 2
    while output.exists():
        output = Path(out_root) / f"{stem}_{n}.xhb"
        n += 1
    return output


def fill_categories(book: Book, rule_ids: list[str], input_path: Path, output: Path | None,
                    expected_sha256: str | None = None, backup_dir: Path | None = None) -> tuple[Summary, Path | None]:
    """Fill empty categories with the chosen rules (by id, e.g. "E12") in a new copy at
    `output`, or (output None) in the file itself after a backup in `backup_dir`."""
    plan = plan_fill(book)
    if expected_sha256 and expected_sha256 != plan.source_sha256:
        raise HbkitError("the HomeBank file has changed since the rules were listed; list them again")
    known = {r.id for r in plan.rules}
    chosen = {str(x) for x in rule_ids}
    unknown = chosen - known
    decided = plan.decide(chosen & known)
    by_rule = Counter(rule.id for rule, _ in decided.values())

    s = Summary("Fill empty categories")
    s.count("Transactions without a category (open accounts)", len(plan.txns))
    s.count("Rules chosen", len(chosen))
    if unknown:
        s.count("Skipped: rules not found or not able to set a category", len(unknown))
    s.count("Rules that fill at least one", len(by_rule))
    if not decided:
        s.note("No categories to fill, so no file was written.")
        return s, None
    editor = XhbEditor(book)
    for i, (_, cat) in sorted(decided.items()):
        editor.replace(plan.txns[i], {"category": str(cat)}, order=S.OPE_ATTRIBUTE_ORDER)
    saved = editor.save_to(output, input_path, backup_dir)
    s.count("Categories filled", saved.replaced)
    s.count("Still without a category", len(plan.txns) - saved.replaced)
    describe_save(s, saved, "the categories")
    return s, saved.path
