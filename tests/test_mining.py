"""Rule mining checked against the synthetic file's ground truth."""

import unittest

from hbkit.fixtures.generator import MERCHANTS
from hbkit.rules.engine import Matcher, existing_rules, simulate
from hbkit.rules.mining import (
    SCENARIOS, MiningParams, backtest, build_items, find_overlaps, inconsistent_patterns, mine,
    score_existing,
)
from hbkit.xhb import format as F
from hbkit.xhb.reader import read_bytes

from .helpers import sample


def merchant_of(text: str) -> str | None:
    """The generator merchant whose code appears in a description (longest code wins)."""
    codes = [m.code for m in MERCHANTS if m.code in text.upper()]
    aliases = {"MRP": "MR PRICE", "SUNNYDALE": "SUNNYDALE", "ELECTRICITY": "PREPAID ELECTRICITY",
               "CREDIT INTEREST": "INTEREST", "ACME CORP SALARY": "SALARY"}
    codes += [code for alias, code in aliases.items() if alias in text.upper()]
    return max(codes, key=len) if codes else None


class MiningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = sample()
        cls.book = read_bytes(cls.gen.data)
        cls.params = MiningParams()
        cls.items, view, cls.excluded = build_items(cls.book, cls.params)
        cls.matcher = Matcher(view)
        cls.existing = existing_rules(cls.book)
        cls.existing_sim = simulate(cls.existing, cls.matcher)
        cls.result = mine(cls.items, cls.matcher, cls.params, cls.existing, cls.existing_sim.category,
                          set(cls.book.categories))
        cls.texts = {c.text: c for c in cls.result.suggested}

    def test_excluded_transactions(self):
        self.assertGreater(self.excluded["split"], 0)
        self.assertGreater(self.excluded["internal transfer"], 0)
        self.assertGreater(self.excluded["void"], 0)
        self.assertFalse(any(it.txn.is_split or it.txn.is_internal_transfer or it.txn.is_void
                             for it in self.items))

    def test_suggestions_meet_thresholds(self):
        self.assertGreater(len(self.result.suggested), 10)
        for c in self.result.suggested:
            self.assertGreaterEqual(c.matches, self.params.min_count, c.text)
            self.assertGreaterEqual(c.precision, self.params.min_share, c.text)

    def test_suggestions_agree_with_ground_truth(self):
        checked = 0
        for c in self.result.suggested:
            code = merchant_of(c.examples[0])
            if code is None or code in self.gen.inconsistent or code == "VIRGIN ACTIVE":
                continue
            self.assertEqual(self.book.category_path(c.category_key), self.gen.truth[code], c.text)
            checked += 1
        self.assertGreater(checked, 10)

    def test_expected_rules(self):
        self.assertIn("DISCOVERY LIFE", self.texts)
        self.assertIn("WOOLWORTHS SANDTON", self.texts)
        self.assertEqual(self.texts["WOOLWORTHS"].kind, "merged")  # one rule for all branches
        self.assertGreaterEqual(self.texts["WOOLWORTHS"].patterns_covered, 3)
        self.assertEqual(self.texts["WOOLWORTHS SANDTON"].covered_by, "WOOLWORTHS")

    def test_ambiguous_texts_are_not_suggested(self):
        for text in self.texts:
            self.assertNotIn(text.upper(), {"UBER", "HELP.UBER.COM", "CRESTA", "SANDTON"})
            self.assertNotIn("CLICKS", text.upper())
            self.assertNotIn("TAKEALOT", text.upper())
            self.assertNotIn("VIRGIN", text.upper())

    def test_too_broad_text_falls_back_to_longer_one(self):
        # "CRESTA" also matches the clothing shop MRP CRESTA, so the Pick n Pay rule must be longer.
        self.assertIn("PNP CRESTA", self.texts)

    def test_existing_rules_are_not_repeated(self):
        statuses = {c.text: c.status for c in self.result.rejected}
        self.assertTrue(statuses["VODACOM"].startswith("already an existing rule (E4)"))
        # an existing rule with the same text but a missing category must be fixed, not duplicated
        self.assertTrue(statuses["AFRIHOST"].startswith("same text as existing rule E7"))
        self.assertNotIn("VODACOM", self.texts)

    def test_merge_can_be_switched_off(self):
        result = mine(self.items, self.matcher, MiningParams(merge=False), self.existing,
                      self.existing_sim.category, set(self.book.categories))
        self.assertFalse([c for c in result.suggested if c.kind == "merged"])
        self.assertNotIn("WOOLWORTHS", {c.text for c in result.suggested})

    def test_thresholds_are_configurable(self):
        strict = mine(self.items, self.matcher, MiningParams(min_count=1000), self.existing)
        self.assertEqual(strict.suggested, [])
        loose = mine(self.items, self.matcher, MiningParams(min_share=0.5), self.existing)
        self.assertGreater(len(loose.suggested), len(self.result.suggested))


class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = read_bytes(sample().data)
        cls.params = MiningParams()
        items, view, _ = build_items(cls.book, cls.params)
        cls.items = items
        cls.matcher = Matcher(view)
        cls.existing = existing_rules(cls.book)
        cls.bt = backtest(cls.book, items, cls.matcher, cls.params, cls.existing)

    def test_years(self):
        self.assertEqual((self.bt.train_years, self.bt.test_years), ((2020, 2023), (2024, 2025)))
        self.assertEqual(self.bt.test_labelled, sum(1 for it in self.items if it.category and it.year >= 2024))

    def test_outcomes(self):
        existing, new, both = (self.bt.overall[s] for s in SCENARIOS)
        for o in (existing, new, both):
            self.assertEqual(o.total, self.bt.test_labelled)
        self.assertEqual(new.wrong, 0)
        self.assertGreater(both.correct, existing.correct)
        self.assertGreater(new.correct / new.total, 0.6)
        # the planted bad rules (UBER, the DISCOVERY regex) cause the wrong answers
        self.assertGreater(existing.wrong, 0)
        self.assertEqual(both.wrong, existing.wrong)

    def test_per_category_totals(self):
        total = sum(next(iter(per.values())).total for per in self.bt.by_category.values())
        self.assertEqual(total, self.bt.test_labelled)

    def test_too_few_years(self):
        book = read_bytes(sample(years=2).data)
        items, view, _ = build_items(book, self.params)
        self.assertIsNone(backtest(book, items, Matcher(view), self.params, existing_rules(book)))


class ExistingRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        book = read_bytes(sample().data)
        params = MiningParams()
        items, view, _ = build_items(book, params)
        matcher = Matcher(view)
        existing = existing_rules(book)
        cls.scores = {s.rule.id: s for s in score_existing(book, items, matcher, params, existing, 2024)}
        cls.overlaps = find_overlaps(existing, matcher, items)
        cls.inconsistent = inconsistent_patterns(items, params)
        cls.book = book

    def test_statuses(self):
        status = {k: s.status for k, s in self.scores.items()}
        self.assertEqual(status["E1"], "OK")
        self.assertTrue(status["E2"].startswith("weak"))  # "UBER" catches Uber trips too
        self.assertEqual(status["E3"], "always overruled by another rule")  # "UBER TRIP" after "UBER"
        self.assertEqual(status["E4"], "OK")
        self.assertEqual(status["E5"], "never matches")
        self.assertTrue(status["E6"].startswith("weak"))  # regex catches Discovery Life too
        self.assertEqual(status["E7"], "broken: its category no longer exists")
        self.assertEqual(status["E8"], "OK")  # payee-field rule
        self.assertEqual(status["E9"], "OK")  # overwrite rule
        self.assertEqual(status["E10"], "broken: invalid regular expression")

    def test_overlap_and_winner(self):
        pair = next(o for o in self.overlaps if (o.first.id, o.second.id) == ("E2", "E3"))
        self.assertTrue(pair.different_categories)
        self.assertEqual(pair.winner.id, "E2")
        self.assertGreater(pair.shared, 50)

    def test_inconsistent_patterns(self):
        patterns = " ".join(x.pattern for x in self.inconsistent)
        for code in ("CLICKS", "TAKEALOT", "VIRGIN ACTIVE"):
            self.assertIn(code, patterns)
        virgin = next(x for x in self.inconsistent if "VIRGIN" in x.pattern)
        self.assertNotEqual(virgin.recent_top, virgin.top[0] if virgin.top[0] != virgin.recent_top else -1)
        self.assertEqual(self.book.category_path(virgin.recent_top), "Sport & fitness")


class PayeeFieldTests(unittest.TestCase):
    def test_mining_payee_names(self):
        book = read_bytes(sample().data)
        params = MiningParams(field=F.RULE_FIELD_PAYEE)
        items, view, excluded = build_items(book, params)
        self.assertGreater(excluded["no text in the chosen field"], 0)
        result = mine(items, Matcher(view), params, existing_rules(book))
        texts = {c.text for c in result.suggested}
        self.assertIn("Woolworths", texts)


if __name__ == "__main__":
    unittest.main()
