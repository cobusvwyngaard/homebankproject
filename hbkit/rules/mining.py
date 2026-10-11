"""Mine assignment-rule suggestions from categorised transactions.

The steps:

1. Every eligible transaction (not void, not split, not an internal transfer unless
   asked, with text in the chosen field) gets a *pattern*: its cleaned description.
2. A **pattern candidate** is a pattern seen at least ``min_count`` times with at
   least ``min_share`` of them in one category.
3. HomeBank matches a rule's text literally ("contains", ignoring case), so the
   candidate's **rule text** is the longest run of whole words of the pattern that
   occurs in *every* raw description of that pattern (e.g. "WOOLWORTHS SANDTON").
   That rule text is then measured the way HomeBank would apply it, over all
   transactions — it may match more than its own pattern — and is only suggested
   if it still reaches ``min_share``.
4. **Merged candidates** (optional): shorter runs of words shared by the rule texts
   of two or more patterns (e.g. "WOOLWORTHS" for several branches), suggested when
   they meet the same thresholds.
5. Back-test, scoring of existing rules, overlaps and inconsistent patterns are
   computed with the HomeBank simulator in engine.py.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..xhb import format as F
from ..xhb.model import Book, Transaction
from .engine import Matcher, SimRule, Simulation, TextView, simulate
from .normalise import is_generic, normalise

NEW_RULE_FLAGS = F.ASGF_DOCAT  # "set category if empty", case-insensitive, contains


@dataclass
class MiningParams:
    min_count: int = 5
    min_share: float = 0.95
    test_years: int = 2
    field: int = F.RULE_FIELD_MEMO
    merge: bool = True
    include_transfers: bool = False
    min_text_length: int = 4


@dataclass
class Item:
    """One eligible transaction."""

    idx: int  # position in the TextView
    txn: Transaction
    text: str  # the text HomeBank matches rules against
    pattern: str
    category: int  # 0 = uncategorised
    year: int
    date: date


@dataclass
class Candidate:
    kind: str  # "pattern" or "merged"
    text: str  # rule text as it would go into HomeBank
    category_key: int
    pattern: str = ""
    pattern_count: int = 0
    pattern_share: float = 0.0
    patterns_covered: int = 1
    matched: frozenset[int] = frozenset()  # categorised items matched (view indices)
    correct: int = 0
    would_fill: int = 0  # uncategorised items matched
    first_seen: date | None = None
    last_seen: date | None = None
    examples: list[str] = field(default_factory=list)
    status: str = "suggested"
    id: str = ""
    covered_by: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def matches(self) -> int:
        return len(self.matched)

    @property
    def precision(self) -> float:
        return self.correct / len(self.matched) if self.matched else 0.0

    @property
    def key(self) -> tuple[str, int]:
        return (self.text.casefold(), self.category_key)

    def sim_rule(self, field_: int) -> SimRule:
        return SimRule(id=self.id or self.text, search=self.text, flags=NEW_RULE_FLAGS,
                       match_field=field_, category_key=self.category_key, source="new")


# --- eligible transactions --------------------------------------------------------------------


def build_items(book: Book, params: MiningParams) -> tuple[list[Item], TextView, Counter]:
    """Eligible transactions, the view rules are matched against, and exclusion counts."""
    excluded: Counter = Counter()
    chosen: list[Transaction] = []
    for t in book.transactions:
        if t.is_void:
            excluded["void"] += 1
        elif t.is_split:
            excluded["split"] += 1
        elif t.is_internal_transfer and not params.include_transfers:
            excluded["internal transfer"] += 1
        elif t.date is None:
            excluded["unreadable date"] += 1
        else:
            chosen.append(t)
    view = TextView.build(book, chosen)
    texts = view.payee if params.field == F.RULE_FIELD_PAYEE else view.memo
    items = []
    for i, t in enumerate(chosen):
        text = texts[i]
        if not text.strip():
            excluded["no text in the chosen field"] += 1
            continue
        pattern = normalise(text)
        if not pattern:
            excluded["nothing left after cleaning"] += 1
            continue
        category = t.category_key if t.category_key in book.categories else 0
        items.append(Item(i, t, text, pattern, category, t.date.year, t.date))
    return items, view, excluded


# --- rule texts -------------------------------------------------------------------------------


def _acceptable(tokens: list[str], min_len: int) -> bool:
    """A usable rule text: long enough, has letters, and starts and ends with a non-generic word."""
    text = " ".join(tokens)
    return (len(text) >= min_len and any(c.isalpha() for c in text)
            and not is_generic(tokens[:1]) and not is_generic(tokens[-1:])
            and any(sum(c.isalpha() for c in t) >= 3 for t in tokens))


#: Rule texts found in at least this share of a pattern's descriptions are considered.
MIN_TEXT_COVERAGE = 0.9


def rule_text_options(pattern: str, raw_texts: Sequence[str], min_len: int = 4) -> list[tuple[str, float]]:
    """Runs of whole pattern words that occur literally in the raw texts (ignoring case).

    Returns (text, coverage) pairs, best first: texts found in every raw text before
    texts found in most of them, then longer texts before shorter ones. Texts keep the
    spelling of the descriptions (e.g. "Woolworths" rather than "WOOLWORTHS"). The
    complete pattern is always allowed; shorter runs must not start or end with a
    generic word such as "PAYMENT".
    """
    tokens = pattern.split()
    raws = [r.casefold() for r in raw_texts]
    if not raws:
        return []
    options: dict[str, float] = {}
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens) + 1):
            run = tokens[i:j]
            whole = i == 0 and j == len(tokens)
            if whole:
                text = " ".join(run)
                if len(text) < min_len or is_generic(run) or not any(c.isalpha() for c in text):
                    continue
            elif not _acceptable(run, min_len):
                continue
            text = " ".join(run)
            cf = text.casefold()
            coverage = sum(1 for r in raws if cf in r) / len(raws)
            if coverage >= MIN_TEXT_COVERAGE:
                options[_original_spelling(text, raw_texts)] = coverage
    return sorted(options.items(), key=lambda kv: (kv[1] < 1.0, -len(kv[0]), kv[0]))


def _original_spelling(text: str, raw_texts: Sequence[str]) -> str:
    """The text as written in the first description that contains it."""
    cf = text.casefold()
    for raw in raw_texts:
        folded = raw.casefold()
        pos = folded.find(cf)
        if pos >= 0 and len(folded) == len(raw):  # same length: positions line up
            return raw[pos:pos + len(text)]
    return text


def literal_rule_text(pattern: str, raw_texts: Sequence[str], min_len: int = 4) -> str | None:
    """The longest run of whole pattern words found in every raw text, if any."""
    for text, coverage in rule_text_options(pattern, raw_texts, min_len):
        if coverage == 1.0:
            return text
    return None


# --- mining -----------------------------------------------------------------------------------


@dataclass
class MiningResult:
    suggested: list[Candidate]
    rejected: list[Candidate]
    merged_considered: int = 0


def _measure(cand: Candidate, idxs: list[int], by_idx: dict[int, Item]) -> None:
    labelled = [by_idx[i] for i in idxs if i in by_idx and by_idx[i].category]
    cand.matched = frozenset(it.idx for it in labelled)
    cand.correct = sum(1 for it in labelled if it.category == cand.category_key)
    cand.would_fill = sum(1 for i in idxs if i in by_idx and not by_idx[i].category)
    dates = [by_idx[i].date for i in idxs if i in by_idx]
    cand.first_seen = min(dates) if dates else None
    cand.last_seen = max(dates) if dates else None
    seen, examples = set(), []
    for i in idxs:
        if i in by_idx and by_idx[i].text not in seen:
            seen.add(by_idx[i].text)
            examples.append(by_idx[i].text)
        if len(examples) == 3:
            break
    cand.examples = examples


def mine(items: Sequence[Item], matcher: Matcher, params: MiningParams,
         existing: Sequence[SimRule] = (),
         existing_prediction: Sequence[int] | None = None,
         valid_categories: set[int] | None = None) -> MiningResult:
    """Build candidates from `items` (one period). Matching is restricted to these items.

    `existing` are the file's rules; `existing_prediction` (category per view index
    from those rules) lets candidates the existing rules already handle be set aside.
    """
    by_idx = {it.idx: it for it in items}
    same_text: dict[str, list[SimRule]] = defaultdict(list)
    for r in existing:
        if r.match_field == params.field and not r.flags & F.ASGF_REGEX:
            same_text[r.search.casefold()].append(r)
    existing_texts = set(same_text)

    def duplicate_status(c: Candidate) -> str | None:
        rules = same_text.get(c.text.casefold())
        if not rules:
            return None
        for r in rules:
            works = valid_categories is None or r.category_key in valid_categories
            if r.sets_category and r.category_key == c.category_key and works:
                return f"already an existing rule ({r.id})"
        return f"same text as existing rule {rules[0].id}, which gives another or a missing category: fix that rule"
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        groups[it.pattern].append(it)
    label_counts: dict[str, Counter] = {
        p: Counter(it.category for it in g if it.category) for p, g in groups.items()
    }
    options: dict[str, list[tuple[str, float]]] = {}
    for p, g in groups.items():
        if label_counts[p]:
            options[p] = rule_text_options(p, [it.text for it in g], params.min_text_length)

    def indices(text: str) -> list[int]:
        rule = SimRule(id=text, search=text, flags=NEW_RULE_FLAGS, match_field=params.field)
        return [i for i in matcher.indices(rule) if i in by_idx]

    def already_handled(c: Candidate) -> bool:
        if existing_prediction is None or not c.matched:
            return False
        right = sum(1 for i in c.matched if existing_prediction[i] == by_idx[i].category)
        return right / len(c.matched) >= params.min_share

    suggested: list[Candidate] = []
    rejected: list[Candidate] = []
    for p, counts in label_counts.items():
        n = sum(counts.values())
        if not counts or n < params.min_count:
            continue
        cat, top = counts.most_common(1)[0]
        if top / n < params.min_share:
            continue
        first: Candidate | None = None
        chosen: Candidate | None = None
        for text, coverage in options.get(p, [])[:6]:
            cand = Candidate("pattern", text, cat, pattern=p, pattern_count=n, pattern_share=top / n)
            _measure(cand, indices(text), by_idx)
            if coverage < 1.0:
                cand.notes.append(f"matches {coverage:.0%} of this pattern's descriptions "
                                  "(the others are written differently)")
            first = first or cand
            if cand.matches >= params.min_count and cand.precision >= params.min_share:
                chosen = cand
                break
        if chosen is None:
            cand = first or Candidate("pattern", "", cat, pattern=p, pattern_count=n, pattern_share=top / n)
            cand.status = "rule text too broad" if first else "no usable rule text"
            rejected.append(cand)
        elif duplicate_status(chosen):
            chosen.status = duplicate_status(chosen)
            rejected.append(chosen)
        elif already_handled(chosen):
            chosen.status = "existing rules already handle these"
            rejected.append(chosen)
        else:
            suggested.append(chosen)

    merged_considered = 0
    if params.merge:
        rule_texts = {p: (opts[0][0] if opts else None) for p, opts in options.items()}
        merged, merged_considered = _merge(groups, label_counts, rule_texts, indices, by_idx,
                                           params, existing_texts)
        pattern_texts = {c.text.casefold() for c in suggested}
        merged = [m for m in merged if m.text.casefold() not in pattern_texts]
        for m in merged:
            if already_handled(m):
                m.status = "existing rules already handle these"
                rejected.append(m)
        merged = [m for m in merged if m.status == "suggested"]
        for c in suggested:
            for m in merged:
                if m.category_key == c.category_key and c.matched <= m.matched:
                    c.covered_by = m.text
                    break
        suggested += merged
    return MiningResult(suggested, rejected, merged_considered)


def _merge(groups, label_counts, rule_texts, indices, by_idx, params, existing_texts):
    subruns: dict[str, set[str]] = defaultdict(set)
    for p, text in rule_texts.items():
        if not text:
            continue
        tokens = text.split()
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                run = tokens[i:j]
                if _acceptable(run, params.min_text_length):
                    subruns[" ".join(run)].add(p)
    accepted: list[Candidate] = []
    considered = 0
    for text, patterns in subruns.items():
        if len(patterns) < 2 or text.casefold() in existing_texts:
            continue
        total: Counter = Counter()
        for p in patterns:
            total.update(label_counts[p])
        n = sum(total.values())
        if n < params.min_count:
            continue
        cat, top = total.most_common(1)[0]
        if top / n < params.min_share:
            continue
        considered += 1
        cand = Candidate("merged", text, cat)
        idxs = indices(text)
        _measure(cand, idxs, by_idx)
        covered = {by_idx[i].pattern for i in cand.matched}
        cand.patterns_covered = len(covered)
        cand.pattern_count = cand.matches
        if cand.matches >= params.min_count and cand.precision >= params.min_share and len(covered) >= 2:
            accepted.append(cand)
    # Keep only the most general of nested candidates with the same category; when two
    # match exactly the same transactions, keep the longer (more specific) text.
    accepted.sort(key=lambda c: (-c.matches, -len(c.text), c.text))
    kept: list[Candidate] = []
    for c in accepted:
        if any(k.category_key == c.category_key and c.matched <= k.matched for k in kept):
            continue
        kept.append(c)
    return kept, considered


def evaluation_order(cands: Sequence[Candidate]) -> list[Candidate]:
    """New rules: more specific (longer) texts first, so they win over general ones."""
    return sorted(cands, key=lambda c: (-len(c.text), c.kind != "pattern", c.text))


# --- back-test ----------------------------------------------------------------------------------

SCENARIOS = ("existing rules only", "new rules only", "existing + new rules")


@dataclass
class Outcome:
    correct: int = 0
    wrong: int = 0
    unassigned: int = 0
    would_fill: int = 0  # uncategorised transactions that would get a category

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.unassigned


@dataclass
class Backtest:
    train_years: tuple[int, int]
    test_years: tuple[int, int]
    test_labelled: int
    overall: dict[str, Outcome]
    by_category: dict[int, dict[str, Outcome]]
    per_candidate: dict[tuple[str, int], tuple[int, int]]  # key -> (matches, correct) in test
    train_suggested: int


def _outcomes(sim: Simulation, items: Sequence[Item]) -> tuple[Outcome, dict[int, Outcome]]:
    overall, per_cat = Outcome(), defaultdict(Outcome)
    for it in items:
        predicted = sim.category[it.idx]
        if not it.category:
            if predicted:
                overall.would_fill += 1
            continue
        o = per_cat[it.category]
        if predicted == 0:
            overall.unassigned += 1
            o.unassigned += 1
        elif predicted == it.category:
            overall.correct += 1
            o.correct += 1
        else:
            overall.wrong += 1
            o.wrong += 1
    return overall, per_cat


def backtest(book: Book, items: Sequence[Item], matcher: Matcher, params: MiningParams,
             existing: Sequence[SimRule]) -> Backtest | None:
    years = sorted({it.year for it in items if it.category})
    if len(years) < params.test_years + 1 or params.test_years < 1:
        return None
    first_test = years[-params.test_years]
    train = [it for it in items if it.year < first_test]
    test = [it for it in items if it.year >= first_test]
    existing_sim = simulate(existing, matcher)
    result = mine(train, matcher, params, existing, existing_sim.category, set(book.categories))
    new_rules = [c.sim_rule(params.field) for c in evaluation_order(result.suggested)]
    sims = {
        SCENARIOS[0]: simulate(existing, matcher),
        SCENARIOS[1]: simulate(new_rules, matcher),
        SCENARIOS[2]: simulate(list(existing) + new_rules, matcher),
    }
    overall, by_category = {}, defaultdict(dict)
    for name, sim in sims.items():
        o, per_cat = _outcomes(sim, test)
        overall[name] = o
        for cat, oc in per_cat.items():
            by_category[cat][name] = oc
    test_by_idx = {it.idx: it for it in test if it.category}
    per_candidate = {}
    for c in result.suggested:
        idxs = [i for i in matcher.indices(c.sim_rule(params.field)) if i in test_by_idx]
        per_candidate[c.key] = (len(idxs), sum(1 for i in idxs if test_by_idx[i].category == c.category_key))
    return Backtest(
        train_years=(years[0], first_test - 1),
        test_years=(first_test, years[-1]),
        test_labelled=len(test_by_idx),
        overall=overall,
        by_category=dict(by_category),
        per_candidate=per_candidate,
        train_suggested=len(result.suggested),
    )


# --- existing rules -----------------------------------------------------------------------------


@dataclass
class RuleScore:
    rule: SimRule
    position: int
    matches: int = 0
    matches_recent: int = 0
    decided: int = 0
    decided_correct: int = 0
    matched_correct: int = 0
    matched_labelled: int = 0
    overruled: int = 0
    would_fill: int = 0
    status: str = ""


def score_existing(book: Book, items: Sequence[Item], matcher: Matcher, params: MiningParams,
                   existing: Sequence[SimRule], recent_from: int | None) -> list[RuleScore]:
    sim = simulate(existing, matcher)
    by_idx = {it.idx: it for it in items}
    scores = [RuleScore(r, pos) for pos, r in enumerate(existing, start=1)]
    for idx, rule_ids in enumerate(sim.matched):
        it = by_idx.get(idx)
        if it is None:
            continue
        for ri in rule_ids:
            s = scores[ri]
            s.matches += 1
            if recent_from is not None and it.year >= recent_from:
                s.matches_recent += 1
            if it.category:
                s.matched_labelled += 1
                if it.category == s.rule.category_key:
                    s.matched_correct += 1
                if s.rule.sets_category and sim.decided_by[idx] != ri:
                    s.overruled += 1
        ri = sim.decided_by[idx]
        if ri is not None:
            s = scores[ri]
            if it.category:
                s.decided += 1
                if it.category == s.rule.category_key:
                    s.decided_correct += 1
            else:
                s.would_fill += 1
    for s in scores:
        r = s.rule
        if r.error:
            s.status = f"broken: {r.error}"
        elif r.sets_category and r.category_key and r.category_key not in book.categories:
            s.status = "broken: its category no longer exists"
        elif s.matches == 0:
            s.status = "never matches"
        elif r.sets_category and s.decided == 0 and s.would_fill == 0:
            s.status = "always overruled by another rule"
        elif recent_from is not None and s.matches_recent == 0:
            s.status = "not used recently"
        elif not r.sets_category:
            s.status = "does not set a category"
        elif s.decided and s.decided_correct / s.decided < params.min_share:
            s.status = "weak: often gives the wrong category"
        else:
            s.status = "OK"
    return scores


# --- overlaps -------------------------------------------------------------------------------------


@dataclass
class Overlap:
    first: SimRule  # earlier in evaluation order
    second: SimRule
    shared: int
    winner: SimRule

    @property
    def different_categories(self) -> bool:
        return (self.first.sets_category and self.second.sets_category
                and self.first.category_key != self.second.category_key)


def find_overlaps(rules: Sequence[SimRule], matcher: Matcher, items: Sequence[Item]) -> list[Overlap]:
    sim = simulate(rules, matcher)
    wanted = {it.idx for it in items}
    pairs: Counter = Counter()
    for idx, rule_ids in enumerate(sim.matched):
        if idx not in wanted or len(rule_ids) < 2:
            continue
        for a in range(len(rule_ids)):
            for b in range(a + 1, len(rule_ids)):
                pairs[(rule_ids[a], rule_ids[b])] += 1
    out = []
    for (a, b), n in pairs.items():
        ra, rb = rules[a], rules[b]
        if rb.overwrites_category or not ra.sets_category:
            winner = rb
        else:
            winner = ra
        out.append(Overlap(ra, rb, n, winner))
    out.sort(key=lambda o: (not o.different_categories, -o.shared))
    return out


# --- inconsistent patterns ----------------------------------------------------------------------


@dataclass
class Inconsistency:
    pattern: str
    counts: Counter
    recent_top: int
    first_seen: date
    last_seen: date
    examples: list[str]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def top(self) -> tuple[int, int]:
        return self.counts.most_common(1)[0]


def inconsistent_patterns(items: Sequence[Item], params: MiningParams) -> list[Inconsistency]:
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if it.category:
            groups[it.pattern].append(it)
    out = []
    for p, g in groups.items():
        counts = Counter(it.category for it in g)
        n = len(g)
        if n < params.min_count or counts.most_common(1)[0][1] / n >= params.min_share:
            continue
        last_year = max(it.year for it in g)
        recent = Counter(it.category for it in g if it.year == last_year)
        examples = list(dict.fromkeys(it.text for it in g))[:3]
        out.append(Inconsistency(p, counts, recent.most_common(1)[0][0], min(it.date for it in g),
                                 max(it.date for it in g), examples))
    out.sort(key=lambda x: -x.total)
    return out

