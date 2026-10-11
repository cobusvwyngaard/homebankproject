"""Assignment-rule mining: suggest HomeBank rules from how transactions were categorised.

    python -m hbkit run rule-mining            # writes review.csv and other reports
    python -m hbkit apply-rules --review <folder or review.csv>

See hbkit/rules/mining.py for the method and README.md for how to use the results.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import HbkitError
from ..privacy import OUT_DIR, Summary, default_input, timestamp
from ..rules.engine import Matcher, SimRule, existing_rules, simulate
from ..rules.mining import (
    NEW_RULE_FLAGS, SCENARIOS, AccountDependence, Candidate, Item, MiningParams, Outcome, backtest,
    build_items, evaluation_order, find_overlaps, inconsistent_patterns, mine, score_existing,
)
from ..xhb import format as F
from ..xhb.model import Book, Rule
from ..xhb.reader import read_file
from ..xhb.writer import XhbEditor
from .base import Analysis, Command, Context, Param, pct, write_csv, write_json

FIELDS = {"memo": F.RULE_FIELD_MEMO, "payee": F.RULE_FIELD_PAYEE}
APPROVED = {"y", "yes", "x", "1", "true", "ja", "ok", "approve", "approved", "✓", "✔"}

FOLDER_README = """Rule mining results
===================

Created by hbkit {version} on {created}. Everything here comes from your own HomeBank
file and stays on your computer.

review.csv                 Suggested rules. Put y in the "approve" column of the ones you
                           want, save, then run:
                               python -m hbkit apply-rules --review "{folder}"
                           You may also edit rule_text or category (use the full category
                           name exactly as HomeBank shows it, e.g. Transport:Fuel).
rule_transactions.csv      Every transaction each suggested rule matches, so you can check
                           that the group belongs together. "result" says whether it has the
                           rule's category, another one, or none yet (the rule would fill it).
account_dependent.csv      Descriptions you categorise differently depending on the account
                           (e.g. interest on two home loans for two properties), per account,
                           with the account rule suggested for it or why there is none.
rejected_candidates.csv    Patterns that were consistent enough but did not become
                           suggestions, with the reason.
backtest.csv               How well rules built from the older years categorise the latest
                           years: existing rules, suggested rules, and both together.
backtest_by_category.csv   The same, per category.
existing_rules.csv         A score for each of your existing rules, with a status such as
                           "never matches" or "always overruled by another rule".
overlaps.csv               Pairs of rules that match the same transactions, and which one
                           HomeBank would apply (rules run in order of position).
inconsistent_patterns.csv  Descriptions that you categorised in different ways.
summary.json               Settings and totals of this run.

Notes
- Suggested rules are "contains" rules, ignoring case, on the {field} field, that set the
  category only when it is empty.
- HomeBank rules cannot be limited to one account. An "account" rule works because its
  text (often a policy or loan number) only appears in that account's descriptions.
  Where the descriptions are identical in both accounts, no rule can tell them apart.
- Your existing rules may have set many of the categories in the first place, which makes
  them look better in the back-test than they really are.
"""


class RuleMining(Analysis):
    name = "rule-mining"
    description = "Suggest assignment rules from your categorised history, back-test them, and review existing rules."
    params = (
        Param("min_count", int, 5, "minimum number of transactions with the same cleaned description"),
        Param("min_share", float, 0.95, "minimum share in one category, e.g. 0.95 (or 95) for 95%"),
        Param("test_years", int, 2, "latest calendar years used to test rules built from the years before"),
        Param("field", str, "memo", "which text to mine", ("memo", "payee")),
        Param("merge", bool, True, "also suggest merged merchant-level rules (e.g. one rule for all branches)"),
        Param("include_transfers", bool, False, "include internal transfers"),
        Param("min_text_length", int, 4, "shortest rule text to suggest, in characters"),
        Param("show_categories", bool, False,
              "also show the back-test per category on screen (shows your category names)"),
    )

    def __init__(self) -> None:
        self.commands = (
            Command("apply-rules", "write the rules you approved in review.csv into a NEW copy of your HomeBank file",
                    _apply_arguments, apply_rules),
        )

    # --- run --------------------------------------------------------------------------------------

    def run(self, ctx: Context, params: dict[str, Any]) -> Summary:
        share = params["min_share"]
        if share > 1:
            share /= 100.0
        if not 0 < share <= 1:
            raise HbkitError("--min-share must be between 0 and 1 (or a percentage up to 100)")
        mp = MiningParams(
            min_count=max(1, params["min_count"]), min_share=share, test_years=params["test_years"],
            field=FIELDS[params["field"]], merge=params["merge"],
            include_transfers=params["include_transfers"], min_text_length=max(1, params["min_text_length"]),
        )
        book = ctx.book
        folder = ctx.new_run_dir()
        items, view, excluded = build_items(book, mp)
        matcher = Matcher(view)
        existing = existing_rules(book)
        existing_sim = simulate(existing, matcher)
        bt = backtest(book, items, matcher, mp, existing) if items else None
        result = mine(items, matcher, mp, existing, existing_sim.category, set(book.categories))

        suggested = sorted(result.suggested, key=lambda c: (-c.matches, c.text))
        for i, c in enumerate(suggested, start=1):
            c.id = f"N{i:04d}"
        by_text = {c.text: c.id for c in suggested}
        account_rules = {(c.account_key, c.category_key): c.id for c in suggested if c.kind == "account"}
        for c in suggested:
            c.covered_by = by_text.get(c.covered_by, c.covered_by)
            if c.covered_by:
                c.notes.append(f"also covered by merged rule {c.covered_by} (same category)")
            if len(c.text) < 8 or " " not in c.text:
                c.notes.append("short rule text: check it cannot match other things")
            if c.kind == "account":
                c.notes.append(f"account rule: this text only appears in the descriptions of "
                               f"{book.account_name(c.account_key) or 'one account'}; it is placed "
                               "before the general rules")
            for acc, other, n in c.account_conflicts:
                where = f"in {book.account_name(acc) or 'another account'} these are usually " \
                        f"{book.category_path(other) or 'another category'} ({n})"
                handled = account_rules.get((acc, other))
                c.notes.append(f"{where}: handled by account rule {handled} if you approve it" if handled else
                               f"{where}: no rule text tells them apart, so this rule would give them "
                               "this category too")

        scores = score_existing(book, items, matcher, mp, existing, bt.test_years[0] if bt else None)
        new_rules = [c.sim_rule(mp.field) for c in evaluation_order(suggested)]
        overlaps = find_overlaps(list(existing) + new_rules, matcher, items)
        conflicts: dict[str, list[str]] = {}
        for o in overlaps:
            if o.different_categories and o.first.source != o.second.source:
                new, old = (o.second, o.first) if o.second.source == "new" else (o.first, o.second)
                conflicts.setdefault(new.id, []).append(
                    f"{old.id} ({book.category_path(old.category_key) or 'missing category'})")
        inconsistent = inconsistent_patterns(items, mp)
        labelled = [it for it in items if it.category]

        self._write(folder, book, mp, suggested, result.rejected, bt, scores, overlaps, inconsistent,
                    conflicts, existing_sim.category, params,
                    frozenset(d.pattern for d in result.account_dependent))
        self._write_accounts(folder, book, result.account_dependent)
        lookup = RuleMatches(book, items, matcher, mp.field)
        write_csv(folder / "rule_transactions.csv", ["rule_id", "rule_text", "rule_category", *MATCH_HEADER], (
            [c.id, c.text, book.category_path(c.category_key), *row]
            for c in suggested for row in lookup.rows(c.text, c.category_key)
        ))
        write_json(folder / "summary.json", {
            "toolkit_version": __version__,
            "created": datetime.now().isoformat(timespec="seconds"),
            "source_sha256": hashlib.sha256(book.raw).hexdigest(),
            "parameters": {**params, "min_share": share},
            "transactions_considered": len(items),
            "categorised": len(labelled),
            "excluded": dict(excluded),
            "suggested": len(suggested),
            "account_rules": sum(1 for c in suggested if c.kind == "account"),
            "account_dependent_descriptions": len(result.account_dependent),
            "rejected": len(result.rejected),
            "backtest": None if bt is None else {
                "train_years": bt.train_years, "test_years": bt.test_years,
                "test_transactions": bt.test_labelled,
                "overall": {k: vars(v) for k, v in bt.overall.items()},
            },
        })
        (folder / "README.txt").write_text(FOLDER_README.format(
            version=__version__, created=datetime.now().strftime("%Y-%m-%d %H:%M"), folder=folder,
            field=params["field"]), encoding="utf-8")
        return self._summary(book, mp, items, labelled, excluded, bt, suggested, result, scores, overlaps,
                             inconsistent, folder, params)

    # --- files -------------------------------------------------------------------------------------

    def _write(self, folder: Path, book: Book, mp: MiningParams, suggested: list[Candidate],
               rejected: list[Candidate], bt, scores, overlaps, inconsistent, conflicts, existing_pred,
               params, account_patterns: frozenset[str] = frozenset()) -> None:
        cat = book.category_path
        field_name = params["field"]

        def backtest_cols(c: Candidate) -> list:
            if bt is None:
                return ["not run", "", ""]
            hit = bt.per_candidate.get(c.key)
            if hit is None:
                return ["not found in the training years", "", ""]
            n, correct = hit
            return ["tested", n, pct(correct, n) if n else ""]

        write_csv(folder / "review.csv", [
            "approve", "rule_id", "rule_text", "category", "kind", "for_account", "transactions_matched",
            "correct_pct", "would_fill_uncategorised", "backtest", "backtest_matched", "backtest_correct_pct",
            "patterns_covered", "covered_by", "conflicts_with_existing_rules", "other_accounts_differ",
            "existing_rules_already_right_pct", "first_seen", "last_seen", "notes", "examples", "pattern",
            "field", "category_key",
        ], (
            ["", c.id, c.text, cat(c.category_key), c.kind,
             (book.account_name(c.account_key) or "") if c.kind == "account" else "",
             c.matches, pct(c.correct, c.matches),
             c.would_fill, *backtest_cols(c), c.patterns_covered, c.covered_by,
             "; ".join(conflicts.get(c.id, [])),
             "; ".join(f"{book.account_name(a) or '?'}: {n} in {cat(k) or '?'}" for a, k, n in c.account_conflicts),
             self._right_pct(c, existing_pred),
             c.first_seen, c.last_seen, "; ".join(c.notes), " | ".join(c.examples), c.pattern,
             field_name, c.category_key]
            for c in suggested
        ))
        write_csv(folder / "rejected_candidates.csv", [
            "status", "rule_text", "category", "kind", "pattern", "pattern_transactions", "pattern_share_pct",
            "transactions_matched", "correct_pct", "examples",
        ], (
            [c.status, c.text, cat(c.category_key), c.kind, c.pattern, c.pattern_count,
             round(100 * c.pattern_share, 1) if c.pattern_share else "", c.matches,
             pct(c.correct, c.matches), " | ".join(c.examples)]
            for c in rejected
        ))
        if bt is not None:
            years = f"{bt.train_years[0]}-{bt.train_years[1]}", f"{bt.test_years[0]}-{bt.test_years[1]}"
            write_csv(folder / "backtest.csv", [
                "scenario", "rules_built_from", "tested_on", "test_transactions", "correct", "wrong",
                "unassigned", "correct_pct", "wrong_pct", "unassigned_pct", "would_fill_uncategorised",
            ], (
                [name, *years, o.total, o.correct, o.wrong, o.unassigned, pct(o.correct, o.total),
                 pct(o.wrong, o.total), pct(o.unassigned, o.total), o.would_fill]
                for name, o in bt.overall.items()
            ))
            header = ["category", "test_transactions"]
            for name in SCENARIOS:
                header += [f"{name}: correct", f"{name}: wrong", f"{name}: unassigned", f"{name}: correct_pct"]
            rows = []
            for key, per in sorted(bt.by_category.items(), key=lambda kv: cat(kv[0]) or ""):
                total = next(iter(per.values())).total
                row = [cat(key), total]
                for name in SCENARIOS:
                    o = per.get(name, Outcome())
                    row += [o.correct, o.wrong, o.unassigned, pct(o.correct, o.total)]
                rows.append(row)
            write_csv(folder / "backtest_by_category.csv", header, rows)
        write_csv(folder / "existing_rules.csv", [
            "rule_id", "position", "rule_text", "field", "regex", "case_sensitive", "action", "category",
            "matches_all_years", "matches_test_years", "decided", "decided_correct_pct",
            "matched_with_same_category_pct", "overruled_by_other_rules", "would_fill_uncategorised", "status",
        ], (
            [s.rule.id, s.position, s.rule.search, F.RULE_FIELD_NAMES.get(s.rule.match_field, ""),
             _yn(s.rule.flags & F.ASGF_REGEX), _yn(s.rule.flags & F.ASGF_EXACT), _action(s.rule),
             cat(s.rule.category_key) or ("(missing)" if s.rule.category_key else ""), s.matches,
             s.matches_recent if bt else "", s.decided, pct(s.decided_correct, s.decided) if s.decided else "",
             pct(s.matched_correct, s.matched_labelled) if s.matched_labelled else "", s.overruled,
             s.would_fill, s.status]
            for s in scores
        ))
        write_csv(folder / "overlaps.csv", [
            "first_rule", "first_text", "first_category", "second_rule", "second_text", "second_category",
            "shared_transactions", "different_categories", "winner", "rules_involved",
        ], (
            [o.first.id, o.first.search, cat(o.first.category_key), o.second.id, o.second.search,
             cat(o.second.category_key), o.shared, _yn(o.different_categories), o.winner.id,
             f"{o.first.source} + {o.second.source}"]
            for o in overlaps
        ))
        write_csv(folder / "inconsistent_patterns.csv", [
            "pattern", "transactions", "categories", "main_category", "main_share_pct",
            "main_category_latest_year", "changed_over_time", "depends_on_account", "first_seen", "last_seen",
            "examples",
        ], (
            [x.pattern, x.total,
             " | ".join(f"{cat(k)} {pct(n, x.total)}% ({n})" for k, n in x.counts.most_common()),
             cat(x.top[0]), pct(x.top[1], x.total), cat(x.recent_top), _yn(x.recent_top != x.top[0]),
             _yn(x.pattern in account_patterns), x.first_seen, x.last_seen, " | ".join(x.examples)]
            for x in inconsistent
        ))

    @staticmethod
    def _write_accounts(folder: Path, book: Book, dependent: list[AccountDependence]) -> None:
        cat = book.category_path
        write_csv(folder / "account_dependent.csv", [
            "pattern", "account", "transactions", "category_in_this_account", "share_pct",
            "consistent", "outcome", "rule_id", "rule_text", "main_category_all_accounts",
            "transactions_all_accounts", "examples",
        ], (
            [d.pattern, book.account_name(g.account_key) or "", g.count, cat(g.category_key),
             round(100 * g.share, 1), _yn(g.consistent), g.status,
             g.rule.id if g.rule else "", g.rule.text if g.rule else "", cat(d.main_category), d.count,
             " | ".join(d.examples)]
            for d in dependent for g in d.groups
        ))

    @staticmethod
    def _right_pct(c: Candidate, existing_pred) -> float | str:
        """How often the existing rules already give these transactions their category."""
        if not c.matched or c.category_key == 0:
            return ""
        right = sum(1 for i in c.matched if existing_pred[i] == c.category_key)
        return pct(right, len(c.matched))

    # --- screen ----------------------------------------------------------------------------------

    def _summary(self, book, mp, items, labelled, excluded, bt, suggested, result, scores, overlaps,
                 inconsistent, folder, params) -> Summary:
        s = Summary("Rule mining")
        s.heading(f"Transactions considered ({params['field']} field)")
        s.count("Considered", len(items))
        s.percent("Already categorised", len(labelled), len(items))
        for reason in ("split", "internal transfer", "void", "no text in the chosen field",
                       "nothing left after cleaning", "unreadable date"):
            if excluded.get(reason):
                s.count(f"Left out: {reason}", excluded[reason])
        if bt is None:
            s.heading("Back-test")
            s.note(f"Not run: needs categorised transactions in at least {mp.test_years + 1} calendar years.")
        else:
            s.heading("Back-test")
            s.note(f"Rules built from {bt.train_years[0]}-{bt.train_years[1]}, "
                   f"tested on {bt.test_years[0]}-{bt.test_years[1]}.")
            s.count("Test transactions with a category", bt.test_labelled)
            s.count("Rules suggested from the older years", bt.train_suggested)
            for name in SCENARIOS:
                o = bt.overall[name]
                s.percent(f"{name}: correct", o.correct, o.total)
                s.percent(f"{name}: wrong", o.wrong, o.total)
                s.percent(f"{name}: unassigned", o.unassigned, o.total)
            if params["show_categories"]:
                s.heading("Back-test per category (existing + new rules: correct)")
                names = []
                for key, per in sorted(bt.by_category.items(), key=lambda kv: book.category_path(kv[0]) or ""):
                    o = per.get(SCENARIOS[2], Outcome())
                    name = book.category_path(key) or "(missing category)"
                    names.append(name)
                    s.percent(name, o.correct, o.total)
                s.permit(names)
        s.heading("Suggestions (built from all years)")
        s.count("Suggested rules", len(suggested))
        s.count("  from one description pattern", sum(1 for c in suggested if c.kind == "pattern"))
        s.count("  merged (merchant level)", sum(1 for c in suggested if c.kind == "merged"))
        s.count("  account rules (text found only in one account's descriptions)",
                sum(1 for c in suggested if c.kind == "account"))
        s.count("  that would also fill uncategorised transactions", sum(1 for c in suggested if c.would_fill))
        statuses = Counter(c.status.split(" (")[0].split(",")[0] for c in result.rejected)
        s.count("Set aside (see rejected_candidates.csv)", len(result.rejected))
        for status, n in sorted(statuses.items()):
            s.count(f"  {status}", n)
        s.heading("Checks")
        s.count("Inconsistently categorised patterns", len(inconsistent))
        groups = [g for d in result.account_dependent for g in d.groups if g.consistent]
        s.count("Descriptions whose category depends on the account", len(result.account_dependent))
        s.count("  accounts with an account rule", sum(1 for g in groups if g.rule is not None))
        s.count("  accounts no rule text can tell apart",
                sum(1 for g in groups if g.status == "no rule text tells this account's transactions apart"))
        s.count("Suggested rules that another account contradicts",
                sum(1 for c in suggested if c.account_conflicts))
        s.count("Overlapping rule pairs", len(overlaps))
        s.count("  with different categories", sum(1 for o in overlaps if o.different_categories))
        s.count("Existing rules", len(scores))
        for status, n in sorted(Counter(sc.status for sc in scores).items()):
            s.count(f"  {status}", n)
        s.heading("Output")
        s.output("Folder", folder)
        s.note("Next: open review.csv, put y in the approve column, save, then run", terminal_only=True)
        s.note("python -m hbkit apply-rules --review <that folder>", terminal_only=True)
        s.note("(or use the browser interface: python -m hbkit ui)", terminal_only=True)
        return s


def _yn(flag: Any) -> str:
    return "yes" if flag else "no"


# --- the transactions a rule text matches -----------------------------------------------------------

MATCH_HEADER = ["date", "account", "memo", "payee", "category", "amount", "result", "pattern"]
RESULT_SAME, RESULT_OTHER, RESULT_FILL = "same category", "other category", "no category: would be filled"


class RuleMatches:
    """Lists the transactions a rule text matches, the way HomeBank would match it.

    Used for rule_transactions.csv and by the browser interface, which can also try an
    edited rule text. Only the transactions rule mining considers are listed (not void,
    split or, unless asked, internal transfers).
    """

    def __init__(self, book: Book, items: list[Item], matcher: Matcher, field_: int):
        self.book = book
        self.by_idx = {it.idx: it for it in items}
        self.matcher = matcher
        self.field = field_

    @classmethod
    def for_book(cls, book: Book, field_name: str = "memo", include_transfers: bool = False) -> RuleMatches:
        mp = MiningParams(field=FIELDS.get(field_name, F.RULE_FIELD_MEMO), include_transfers=include_transfers)
        items, view, _ = build_items(book, mp)
        return cls(book, items, Matcher(view), mp.field)

    def rows(self, text: str, category_key: int = 0) -> list[list]:
        """One row per matched transaction (MATCH_HEADER), newest first. "pattern" is the
        cleaned description, which groups transactions of the same kind."""
        if not text.strip():
            return []
        rule = SimRule(id=text, search=text, flags=NEW_RULE_FLAGS, match_field=self.field)
        book = self.book
        out = []
        for i in self.matcher.indices(rule):
            it = self.by_idx.get(i)
            if it is None:
                continue
            t = it.txn
            result = RESULT_FILL if not it.category else RESULT_SAME if it.category == category_key else RESULT_OTHER
            payee = book.payees[t.payee_key].name if t.payee_key in book.payees else ""
            out.append([it.date.isoformat(), book.account_name(t.account_key) or "", t.memo, payee,
                        book.category_path(it.category) or "", t.amount, result, it.pattern])
        out.sort(key=lambda r: r[0], reverse=True)
        return out


def _action(rule: SimRule) -> str:
    if rule.flags & F.ASGF_OVWCAT:
        return "overwrite category"
    if rule.flags & F.ASGF_DOCAT:
        return "set category if empty"
    return "no category"


# --- apply-rules ------------------------------------------------------------------------------------


def _apply_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--review", required=True, metavar="PATH",
                   help="review.csv, or the rule-mining output folder that contains it")
    p.add_argument("--file", metavar="PATH",
                   help="the HomeBank .xhb file (default: the only .xhb file in the data folder)")
    p.add_argument("--out", metavar="FOLDER", default=str(OUT_DIR),
                   help="folder for the new copy (default: data/out)")
    p.add_argument("--output", metavar="PATH", help="name of the new copy (default: data/out/with-rules_<date>.xhb)")


def read_review(path: Path) -> list[dict[str, str]]:
    """Read review.csv, coping with Excel re-saving it (BOM, ';' separator, Windows encoding)."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    delimiter = max([",", ";", "\t"], key=first.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    if reader.fieldnames is None:
        raise HbkitError("the review file is empty")
    reader.fieldnames = [(h or "").strip().lower() for h in reader.fieldnames]
    missing = [c for c in ("approve", "rule_text", "category") if c not in reader.fieldnames]
    if missing:
        raise HbkitError(f"the review file has no {', '.join(missing)} column")
    return [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]


def default_output(out_root: Path) -> Path:
    """data/out/with-rules_<date_time>.xhb, never an existing file."""
    stem = f"with-rules_{timestamp()}"
    output = Path(out_root) / f"{stem}.xhb"
    n = 2
    while output.exists():
        output = Path(out_root) / f"{stem}_{n}.xhb"
        n += 1
    return output


def apply_review_rows(rows: list[dict[str, str]], book: Book, input_path: Path, output: Path,
                      review_folder: Path | None = None) -> tuple[Summary, Path | None]:
    """Add the approved rows as rules to a new copy of the file. Used by the command and the browser."""
    editor = XhbEditor(book)
    existing = {(r.match_field, r.search.casefold()) for r in book.rules}
    skipped: Counter = Counter()
    chosen: list[tuple[bool, Rule]] = []  # (account rule?, rule)
    seen: set[tuple[int, str]] = set()
    approved = [r for r in rows if r.get("approve", "").strip().lower() in APPROVED]
    for row in approved:
        text = row.get("rule_text", "").strip()
        field_ = FIELDS.get((row.get("field") or "memo").strip().lower())
        cat = book.category_by_path(row.get("category", ""))
        key = (field_, text.casefold())
        if not text:
            skipped["no rule text"] += 1
        elif field_ is None:
            skipped["unknown field"] += 1
        elif cat is None:
            skipped["category not found in the file"] += 1
        elif key in existing:
            skipped["already a rule in the file"] += 1
        elif key in seen:
            skipped["approved twice"] += 1
        else:
            seen.add(key)
            chosen.append((row.get("kind", "").strip().lower() == "account",
                           Rule(flags=NEW_RULE_FLAGS, match_field=field_, search=text, category_key=cat.key)))

    s = Summary("Apply approved rules")
    s.count("Rows in the review file", len(rows))
    s.count("Approved", len(approved))
    for reason, n in sorted(skipped.items()):
        s.count(f"Skipped: {reason}", n)
    if not chosen:
        s.note("No rules to add, so no file was written.")
        return s, None

    # Account rules first, then more specific (longer) texts, so they are applied before
    # general ones (as in evaluation_order).
    for _, rule in sorted(chosen, key=lambda ar: (not ar[0], -len(ar[1].search))):
        editor.add_rule(rule)
    result = editor.save(output, input_path=input_path)
    s.count("Rules added", result.inserted)
    s.output("New copy", result.path)

    meta = review_folder / "summary.json" if review_folder else None
    if meta is not None and meta.is_file():
        try:
            source = json.loads(meta.read_text(encoding="utf-8")).get("source_sha256")
        except (ValueError, OSError):
            source = None
        if source and source != hashlib.sha256(book.raw).hexdigest():
            s.note("Note: the HomeBank file has changed since this review was made;")
            s.note("categories were matched by name, so check the new rules in HomeBank.")
    s.note("Your original file was not changed. Open the new copy in HomeBank, check the")
    s.note("rules under Manage > Assignments, and use the copy once you are happy with it.")
    return s, result.path


def apply_rules(args: argparse.Namespace) -> tuple[Summary, Book]:
    review = Path(args.review)
    if review.is_dir():
        review = review / "review.csv"
    if not review.is_file():
        raise HbkitError("the review file was not found (give review.csv or the folder that contains it)")
    rows = read_review(review)
    input_path = Path(args.file) if args.file else default_input()
    book = read_file(input_path)
    output = Path(args.output) if args.output else default_output(Path(args.out))
    summary, _ = apply_review_rows(rows, book, input_path, output, review.parent)
    return summary, book


ANALYSIS = RuleMining()
