"""Filling empty categories with assignment rules (synthetic files only)."""

import difflib
from datetime import date

from hbkit.errors import HbkitError
from hbkit.rules.fill import default_output, fill_categories, plan_fill
from hbkit.xhb import serialise as S
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample


def j(day):
    return date(2025, 1, day).toordinal()


# Account 2 is closed. Rules: 1 set "SHOP" -> Groceries; 2 set "SHOP" -> Household (shadowed by 1);
# 3 overwrite "FEE" -> Bank; 4 set "FEE" -> Household (overwritten by 3, which comes earlier but overwrites).
MINI = f"""<?xml version="1.0"?>
<homebank v="1.6" d="051003">
<properties title="T" curr="1"/>
<cur key="1" flags="0" iso="ZAR" name="Rand" symb="R" syprf="1" dchar="." gchar=" " frac="2" rate="0" mdate="0"/>
<account key="1" pos="1" type="6" curr="1" name="Cheque" initial="0" minimum="0" maximum="0"/>
<account key="2" flags="2" pos="2" type="6" curr="1" name="Old" initial="0" minimum="0" maximum="0"/>
<account key="3" pos="3" type="7" curr="1" name="Savings" initial="0" minimum="0" maximum="0"/>
<cat key="1" name="Groceries"/>
<cat key="2" name="Household"/>
<cat key="3" name="Bank"/>
<asg key="1" flags="4" pos="1" name="SHOP" category="1"/>
<asg key="2" flags="4" pos="2" name="SHOP" category="2"/>
<asg key="3" flags="1028" pos="3" name="FEE" category="3"/>
<asg key="4" flags="4" pos="4" name="FEE" category="2"/>
<asg key="5" flags="4" pos="5" name="GONE" category="99"/>
<ope date="{j(1)}" amount="-10" account="1" wording="SHOP ONE"/>
<ope date="{j(2)}" amount="-20" account="1" category="2" wording="SHOP ALREADY"/>
<ope date="{j(3)}" amount="-5" account="1" wording="MONTHLY FEE"/>
<ope date="{j(4)}" amount="-30" account="1" st="3" wording="SHOP VOID"/>
<ope date="{j(5)}" amount="-40" account="1" flags="256" wording="SHOP SPLIT" scat="1||2" samt="-20||-20" smem="||"/>
<ope date="{j(6)}" amount="-50" account="1" dst_account="3" flags="8" wording="SHOP TRANSFER" kxfer="1"/>
<ope date="{j(7)}" amount="-60" account="1" wording="NOTHING MATCHES"/>
<ope date="{j(8)}" amount="-70" account="2" wording="SHOP IN CLOSED ACCOUNT"/>
<ope date="{j(6)}" amount="50" account="3" dst_account="1" flags="10" wording="SHOP TRANSFER" kxfer="1"/>
</homebank>
""".encode()


class PlanTests(TempDirTestCase):
    def test_which_transactions_and_rules(self):
        book = read_bytes(MINI)
        plan = plan_fill(book)
        self.assertEqual([t.memo for t in plan.txns], ["SHOP ONE", "MONTHLY FEE", "NOTHING MATCHES"])
        self.assertEqual(plan.closed_skipped, 1)
        self.assertEqual([r.id for r in plan.rules], ["E1", "E2", "E3", "E4"])
        self.assertEqual(dict(plan.left_out_rules), {"its category no longer exists": 1})
        self.assertEqual(plan.matches(), {"E1": 1, "E2": 1, "E3": 1, "E4": 1})

    def test_homebanks_order(self):
        plan = plan_fill(read_bytes(MINI))
        cat = lambda d: {plan.txns[i].memo: (r.id, c) for i, (r, c) in d.items()}  # noqa: E731
        self.assertEqual(cat(plan.decide({"E1", "E2", "E3", "E4"})),
                         {"SHOP ONE": ("E1", 1), "MONTHLY FEE": ("E3", 3)})  # first set wins; overwrite wins
        self.assertEqual(cat(plan.decide({"E2", "E4"})), {"SHOP ONE": ("E2", 2), "MONTHLY FEE": ("E4", 2)})
        self.assertEqual(plan.decide(set()), {})

    def test_synthetic_rules(self):
        book = read_bytes(sample(uncategorised=0.3).data)
        plan = plan_fill(book)
        ids = {r.id for r in plan.rules}
        self.assertNotIn("E7", ids)  # its category is missing
        self.assertNotIn("E10", ids)  # invalid regular expression
        everything = plan.decide(ids)
        self.assertNotIn("E3", {r.id for r, _ in everything.values()})  # UBER TRIP is shadowed by UBER
        self.assertIn("E3", {r.id for r, _ in plan.decide(ids - {"E2"}).values()})
        for i, (rule, _) in plan.decide({"E8"}).items():  # a payee rule
            self.assertEqual(book.payee_name(plan.txns[i].payee_key), "Engen")


class FillTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_sample("finances.xhb", uncategorised=0.3)
        self.book = read_bytes(self.src.read_bytes())
        self.plan = plan_fill(self.book)
        self.out = self.tmp / "out"

    def fill(self, rules, **kw):
        return fill_categories(self.book, rules, self.src, default_output(self.out), **kw)

    def test_only_empty_categories_change(self):
        rules = [r.id for r in self.plan.rules]
        decided = self.plan.decide(set(rules))
        before = self.src.read_bytes()
        summary, path = self.fill(rules, expected_sha256=self.plan.source_sha256)
        self.assertEqual(self.src.read_bytes(), before)
        rows = {r["label"]: r for r in summary.to_dict()["rows"]}
        self.assertEqual(rows["Categories filled"]["text"], str(len(decided)))
        diff = [d for d in difflib.ndiff(before.decode().splitlines(), path.read_text().splitlines()) if d[:1] in "+-"]
        self.assertEqual(len(diff), 2 * len(decided))
        new = read_bytes(path.read_bytes())
        for i, (rule, cat) in decided.items():
            t = new.transactions[self.plan.txns[i].index]
            self.assertEqual(t.category_key, cat)
            self.assertEqual(path.read_bytes()[t.src.start:t.src.end].decode(), S.serialise_transaction(t))
        for old, t in zip(self.book.transactions, new.transactions):
            if old.category_key:
                self.assertEqual(t.category_key, old.category_key)
            self.assertEqual((t.amount, t.memo, t.payee_key, t.paymode, t.flags), (old.amount, old.memo, old.payee_key,
                                                                                   old.paymode, old.flags))

    def test_skips_and_refusals(self):
        summary, path = self.fill(["E1", "E7", "E99"])
        labels = [r["label"] for r in summary.to_dict()["rows"]]
        self.assertIn("Skipped: rules not found or not able to set a category", labels)
        self.assertIsNotNone(path)
        self.assertIsNone(self.fill([])[1])
        self.assertIsNone(self.fill(["E5"])[1])  # matches nothing
        with self.assertRaises(HbkitError):
            self.fill(["E1"], expected_sha256="0" * 64)
        with self.assertRaises(HbkitError):
            fill_categories(self.book, ["E1"], self.src, self.src)
