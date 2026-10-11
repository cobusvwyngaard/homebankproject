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
    NEW_RULE_FLAGS, SCENARIOS, Candidate, MiningParams, Outcome, backtest, build_items,
    evaluation_order, find_overlaps, inconsistent_patterns, mine, score_existing,
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
        for c in suggested:
            c.covered_by = by_text.get(c.covered_by, c.covered_by)
            if c.covered_by:
                c.notes.append(f"also covered by merged rule {c.covered_by} (same category)")
            if len(c.text) < 8 or " " not in c.text:
                c.notes.append("short rule text: check it cannot match other things")

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
                    conflicts, existing_sim.category, params)
        write_json(folder / "summary.json", {
            "toolkit_version": __version__,
            "created": datetime.now().isoformat(timespec="seconds"),
            "source_sha256": hashlib.sha256(book.raw).hexdigest(),
            "parameters": {**params, "min_share": share},
            "transactions_considered": len(items),
            "categorised": len(labelled),
            "excluded": dict(excluded),
            "suggested": len(suggested),
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
               params) -> None:
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
            "approve", "rule_id", "rule_text", "category", "kind", "transactions_matched", "correct_pct",
            "would_fill_uncategorised", "backtest", "backtest_matched", "backtest_correct_pct",
            "patterns_covered", "covered_by", "conflicts_with_existing_rules",
            "existing_rules_already_right_pct", "first_seen", "last_seen", "notes", "examples", "pattern",
            "field", "category_key",
        ], (
            ["", c.id, c.text, cat(c.category_key), c.kind, c.matches, pct(c.correct, c.matches),
             c.would_fill, *backtest_cols(c), c.patterns_covered, c.covered_by,
             "; ".join(conflicts.get(c.id, [])), self._right_pct(c, existing_pred),
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
            "main_category_latest_year", "changed_over_time", "first_seen", "last_seen", "examples",
        ], (
            [x.pattern, x.total,
             " | ".join(f"{cat(k)} {pct(n, x.total)}% ({n})" for k, n in x.counts.most_common()),
             cat(x.top[0]), pct(x.top[1], x.total), cat(x.recent_top), _yn(x.recent_top != x.top[0]),
             x.first_seen, x.last_seen, " | ".join(x.examples)]
            for x in inconsistent
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
        s.count("  that would also fill uncategorised transactions", sum(1 for c in suggested if c.would_fill))
        statuses = Counter(c.status.split(" (")[0].split(",")[0] for c in result.rejected)
        s.count("Set aside (see rejected_candidates.csv)", len(result.rejected))
        for status, n in sorted(statuses.items()):
            s.count(f"  {status}", n)
        s.heading("Checks")
        s.count("Inconsistently categorised patterns", len(inconsistent))
        s.count("Overlapping rule pairs", len(overlaps))
        s.count("  with different categories", sum(1 for o in overlaps if o.different_categories))
        s.count("Existing rules", len(scores))
        for status, n in sorted(Counter(sc.status for sc in scores).items()):
            s.count(f"  {status}", n)
        s.heading("Output")
        s.output("Folder", folder)
        s.note("Next: open review.csv, put y in the approve column, save, then run")
        s.note("python -m hbkit apply-rules --review <that folder>")
        return s


def _yn(flag: Any) -> str:
    return "yes" if flag else "no"


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


def apply_rules(args: argparse.Namespace) -> tuple[Summary, Book]:
    review = Path(args.review)
    if review.is_dir():
        review = review / "review.csv"
    if not review.is_file():
        raise HbkitError("the review file was not found (give review.csv or the folder that contains it)")
    rows = read_review(review)
    input_path = Path(args.file) if args.file else default_input()
    book = read_file(input_path)
    editor = XhbEditor(book)

    existing = {(r.match_field, r.search.casefold()) for r in book.rules}
    skipped: Counter = Counter()
    chosen: list[Rule] = []
    seen: set[tuple[int, str]] = set()
    approved = [r for r in rows if r.get("approve", "").strip().lower() in APPROVED]
    for row in approved:
        text = row.get("rule_text", "")
        field_ = FIELDS.get(row.get("field", "memo").lower() or "memo")
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
            chosen.append(Rule(flags=NEW_RULE_FLAGS, match_field=field_, search=text, category_key=cat.key))

    s = Summary("Apply approved rules")
    s.count("Rows in the review file", len(rows))
    s.count("Approved", len(approved))
    for reason, n in sorted(skipped.items()):
        s.count(f"Skipped: {reason}", n)
    if not chosen:
        s.note("No rules to add, so no file was written.")
        return s, book

    # More specific (longer) texts first, so they are applied before general ones.
    for rule in sorted(chosen, key=lambda r: -len(r.search)):
        editor.add_rule(rule)
    if args.output:
        output = Path(args.output)
    else:
        stem = f"with-rules_{timestamp()}"
        output = Path(args.out) / f"{stem}.xhb"
        n = 2
        while output.exists():
            output = Path(args.out) / f"{stem}_{n}.xhb"
            n += 1
    result = editor.save(output, input_path=input_path)
    s.count("Rules added", result.inserted)
    s.output("New copy", result.path)

    meta = review.parent / "summary.json"
    if meta.is_file():
        try:
            source = json.loads(meta.read_text(encoding="utf-8")).get("source_sha256")
        except (ValueError, OSError):
            source = None
        if source and source != hashlib.sha256(book.raw).hexdigest():
            s.note("Note: the HomeBank file has changed since this review was made;")
            s.note("categories were matched by name, so check the new rules in HomeBank.")
    s.note("Your original file was not changed. Open the new copy in HomeBank, check the")
    s.note("rules under Manage > Assignments, and use the copy once you are happy with it.")
    return s, book


ANALYSIS = RuleMining()
