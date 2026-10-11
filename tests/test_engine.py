"""The HomeBank rule simulator must behave like transaction_auto_assign() in hb-assign.c."""

import unittest

from hbkit.rules.engine import Matcher, SimRule, TextView, simulate
from hbkit.xhb import format as F
from hbkit.xhb.model import Book, Payee, Transaction
from hbkit.xhb.scanner import scan

DOCAT, OVWCAT = F.ASGF_DOCAT, F.ASGF_OVWCAT


def view(*memos, payees=None, amounts=None):
    return TextView(memo=list(memos), payee=payees or [""] * len(memos),
                    amount=amounts or [-1.0] * len(memos))


def run(rules, v, start=None):
    return simulate(rules, Matcher(v), start)


class OrderTests(unittest.TestCase):
    def test_first_set_if_empty_rule_wins(self):
        rules = [SimRule("A", "SHOP", DOCAT, category_key=1), SimRule("B", "SHOP X", DOCAT, category_key=2)]
        sim = run(rules, view("SHOP X 123"))
        self.assertEqual((sim.category, sim.decided_by), ([1], [0]))

    def test_last_overwrite_rule_wins(self):
        rules = [
            SimRule("A", "SHOP", DOCAT, category_key=1),
            SimRule("B", "SHOP", DOCAT | OVWCAT, category_key=2),
            SimRule("C", "X", DOCAT | OVWCAT, category_key=3),
            SimRule("D", "SHOP", DOCAT, category_key=4),
        ]
        self.assertEqual(run(rules, view("SHOP X")).category, [3])

    def test_existing_category_is_kept_unless_overwritten(self):
        rules = [SimRule("A", "SHOP", DOCAT, category_key=1)]
        self.assertEqual(run(rules, view("SHOP"), start=[9]).category, [9])
        rules = [SimRule("A", "SHOP", OVWCAT, category_key=1)]
        self.assertEqual(run(rules, view("SHOP"), start=[9]).category, [1])

    def test_rule_without_category_action(self):
        rules = [SimRule("A", "SHOP", F.ASGF_DOPAY, category_key=1)]
        self.assertEqual(run(rules, view("SHOP")).category, [0])


class MatchingTests(unittest.TestCase):
    def test_contains_ignoring_case(self):
        rules = [SimRule("A", "woolworths", DOCAT, category_key=1)]
        self.assertEqual(run(rules, view("POS WOOLWORTHS SANDTON", "WOOLWORTH")).category, [1, 0])

    def test_case_sensitive(self):
        rules = [SimRule("A", "Shop", DOCAT | F.ASGF_EXACT, category_key=1)]
        self.assertEqual(run(rules, view("Shop", "SHOP")).category, [1, 0])

    def test_unicode_case_folding(self):
        rules = [SimRule("A", "STRASSE", DOCAT, category_key=1)]
        self.assertEqual(run(rules, view("Hauptstraße 1")).category, [1])

    def test_regex(self):
        rules = [SimRule("A", r"^DEBIT ORDER \w+ LIFE", DOCAT | F.ASGF_REGEX, category_key=1)]
        self.assertEqual(run(rules, view("debit order discovery life", "x DEBIT ORDER A LIFE")).category, [1, 0])

    def test_regex_needs_a_non_empty_match(self):
        rules = [SimRule("A", "x*", DOCAT | F.ASGF_REGEX, category_key=1)]
        self.assertEqual(run(rules, view("abc", "abxc")).category, [0, 1])

    def test_invalid_regex_never_matches(self):
        rule = SimRule("A", "[unclosed", DOCAT | F.ASGF_REGEX, category_key=1)
        self.assertEqual(rule.error, "invalid regular expression")
        self.assertEqual(run([rule], view("[unclosed")).category, [0])

    def test_amount_condition(self):
        rules = [SimRule("A", "FEE", DOCAT | F.ASGF_AMOUNT, category_key=1, amount=-69.0)]
        self.assertEqual(run(rules, view("FEE", "FEE", amounts=[-69.0, -70.0])).category, [1, 0])


class PayeeFieldTests(unittest.TestCase):
    def book(self):
        b = Book(raw=b"", scan=scan(b"<homebank/>"))
        b.payees[3] = Payee(key=3, name="Engen Garage")
        return b

    def test_payee_texts(self):
        txns = [
            Transaction(memo="ENGEN N1 CITY", payee_key=0),  # no payee: empty text
            Transaction(memo="something", payee_key=3),  # payee name
            Transaction(memo="ENGEN dangling", payee_key=99),  # dangling key: memo
        ]
        v = TextView.build(self.book(), txns)
        self.assertEqual(v.payee, ["", "Engen Garage", "ENGEN dangling"])
        rules = [SimRule("A", "engen", DOCAT, match_field=F.RULE_FIELD_PAYEE, category_key=1)]
        self.assertEqual(run(rules, v).category, [0, 1, 1])


if __name__ == "__main__":
    unittest.main()
