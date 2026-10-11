"""Descriptions whose category depends on the account (e.g. interest on two home loans)."""

import unittest
from datetime import date

from hbkit.fixtures.generator import ACC_LOAN_A, ACC_LOAN_B
from hbkit.rules.engine import Matcher, existing_rules, simulate
from hbkit.rules.mining import (
    Candidate, MiningParams, build_items, evaluation_order, mine, raw_text_options,
)
from hbkit.xhb.reader import read_bytes

from .helpers import sample


def analyse(book, params=None):
    params = params or MiningParams()
    items, view, _ = build_items(book, params)
    matcher = Matcher(view)
    existing = existing_rules(book)
    result = mine(items, matcher, params, existing, simulate(existing, matcher).category,
                  set(book.categories))
    return items, matcher, result


class HomeLoanTests(unittest.TestCase):
    """The synthetic file's two home loans: insurance texts differ by a policy number,
    interest texts are identical in both accounts."""

    @classmethod
    def setUpClass(cls):
        cls.book = read_bytes(sample(home_loans=True).data)
        cls.items, cls.matcher, cls.result = analyse(cls.book)
        cls.dependent = {d.pattern: d for d in cls.result.account_dependent}

    def test_account_dependent_patterns_found(self):
        self.assertEqual(set(self.dependent), {"HOME LOAN INTEREST", "BOND INSURANCE PREMIUM POLICY"})
        for d in self.dependent.values():
            self.assertEqual({g.account_key for g in d.groups}, {ACC_LOAN_A, ACC_LOAN_B})
            self.assertTrue(all(g.consistent for g in d.groups))

    def test_policy_numbers_give_account_rules(self):
        rules = {c.account_key: c for c in self.result.suggested if c.kind == "account"}
        self.assertEqual(set(rules), {ACC_LOAN_A, ACC_LOAN_B})
        self.assertEqual(rules[ACC_LOAN_A].text, "BOND INSURANCE PREMIUM POLICY 4410023")
        self.assertEqual(self.book.category_path(rules[ACC_LOAN_A].category_key), "Property A:Insurance")
        self.assertEqual(self.book.category_path(rules[ACC_LOAN_B].category_key), "Property B:Insurance")
        for c in rules.values():
            self.assertEqual(c.precision, 1.0)
            self.assertEqual(c.account_conflicts, [])

    def test_identical_texts_are_reported_not_suggested(self):
        d = self.dependent["HOME LOAN INTEREST"]
        for g in d.groups:
            self.assertIsNone(g.rule)
            self.assertEqual(g.status, "no rule text tells this account's transactions apart")
        self.assertFalse(any("INTEREST" in c.text.upper() and "HOME" in c.text.upper()
                             for c in self.result.suggested))

    def test_simulated_rules_categorise_each_account_correctly(self):
        rules = [c.sim_rule(0) for c in evaluation_order(self.result.suggested)]
        sim = simulate(rules, self.matcher)
        insurance = [it for it in self.items if it.pattern == "BOND INSURANCE PREMIUM POLICY"]
        self.assertEqual(len(insurance), 138)
        self.assertTrue(all(sim.category[it.idx] == it.category for it in insurance))

    def test_standard_sample_has_no_account_dependent_patterns(self):
        _, _, result = analyse(read_bytes(sample().data))
        self.assertEqual(result.account_dependent, [])
        self.assertFalse(any(c.kind == "account" or c.account_conflicts for c in result.suggested))


def j(day: int) -> int:
    return date(2024, 1, 1).toordinal() + day


def minority_file(personal: int = 100, business: int = 5) -> bytes:
    """The same Uber text on two cards; the business card's trips are business travel."""
    lines = [
        '<?xml version="1.0"?>', '<homebank v="1.6" d="051003">',
        '<properties title="T" curr="1"/>',
        '<cur key="1" flags="0" iso="ZAR" name="Rand" symb="R" syprf="1" dchar="." gchar=" " frac="2" rate="0" mdate="0"/>',
        '<account key="1" pos="1" type="4" curr="1" name="Personal card" initial="0" minimum="0" maximum="0"/>',
        '<account key="2" pos="2" type="4" curr="1" name="Business card" initial="0" minimum="0" maximum="0"/>',
        '<cat key="1" name="Ride hailing"/>', '<cat key="2" name="Business travel"/>',
    ]
    lines += [f'<ope date="{j(i)}" amount="-80" account="1" category="1" wording="UBER TRIP HELP.UBER.COM"/>'
              for i in range(personal)]
    lines += [f'<ope date="{j(i)}" amount="-90" account="2" category="2" wording="UBER TRIP HELP.UBER.COM"/>'
              for i in range(business)]
    lines.append("</homebank>")
    return ("\n".join(lines) + "\n").encode()


class MinorityAccountTests(unittest.TestCase):
    def test_general_rule_flags_the_other_account(self):
        book = read_bytes(minority_file())
        _, _, result = analyse(book)
        self.assertEqual(len(result.suggested), 1)  # 100 of 105 = 95%: still a general rule
        rule = result.suggested[0]
        self.assertEqual(rule.kind, "pattern")
        self.assertEqual(rule.account_conflicts, [(2, 2, 5)])
        (d,) = result.account_dependent
        statuses = {g.account_key: g.status for g in d.groups}
        self.assertEqual(statuses[1], "the general rule for this description gives this category")
        self.assertEqual(statuses[2], "no rule text tells this account's transactions apart")

    def test_too_few_in_the_other_account(self):
        book = read_bytes(minority_file(business=4))
        _, _, result = analyse(book)
        self.assertEqual(result.account_dependent, [])
        self.assertEqual(result.suggested[0].account_conflicts, [])

    def test_same_category_everywhere_is_not_account_dependent(self):
        data = minority_file(business=20).replace(b'account="2" category="2"', b'account="2" category="1"')
        _, _, result = analyse(read_bytes(data))
        self.assertEqual(result.account_dependent, [])


class RawTextOptionTests(unittest.TestCase):
    def test_numbers_are_kept(self):
        options = dict(raw_text_options(["BOND PREMIUM POLICY 4410023"] * 3))
        self.assertIn("BOND PREMIUM POLICY 4410023", options)
        self.assertIn("4410023", options)  # a long number is a usable text on its own
        self.assertNotIn("23", options)

    def test_best_first_and_coverage(self):
        texts = ["INTEREST ACC 1234567 MAR"] * 9 + ["INTEREST ACC 1234567"]
        options = raw_text_options(texts)
        self.assertEqual(options[0], ("INTEREST ACC 1234567", 1.0))
        self.assertIn(("INTEREST ACC 1234567 MAR", 0.9), options)

    def test_generic_edges_and_spelling(self):
        options = dict(raw_text_options(["Debit Order Sanlam Homeowners 4410023"] * 2))
        self.assertIn("Sanlam Homeowners 4410023", options)
        self.assertNotIn("Order Sanlam Homeowners 4410023", options)  # starts with a generic word
        self.assertNotIn("Debit Order", options)

    def test_account_rules_come_first(self):
        cands = [Candidate("pattern", "A LONG GENERAL TEXT", 1), Candidate("merged", "SHORT", 1),
                 Candidate("account", "X 4410023", 2)]
        self.assertEqual([c.kind for c in evaluation_order(cands)], ["account", "pattern", "merged"])


if __name__ == "__main__":
    unittest.main()
