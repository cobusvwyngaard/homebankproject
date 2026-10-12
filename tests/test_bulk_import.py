"""Importing bank files into the HomeBank file in one go (synthetic files only)."""

from datetime import date

from hbkit.errors import HbkitError
from hbkit.imports.bulk import (
    CLOSED, IN_HOMEBANK, NEW, SPLIT, TWICE, Source, default_output, import_transactions, plan_import,
)
from hbkit.imports.formats import read_import_file
from hbkit.xhb import format as F
from hbkit.xhb import serialise as S
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample


def j(day):
    return date(2025, 1, day).toordinal()


# Account 2 is closed. Payee 1 "Acme" has default category 2 and payment mode 6.
# Rules: 1 set category "SHOP" -> 1; 3 overwrite category "FEE" -> 3;
# 6 set payee (1) and payment mode (3) for "TEA"; 7 set tags for payee "Acme".
BOOK = f"""<?xml version="1.0"?>
<homebank v="1.6" d="051003">
<properties title="T" curr="1"/>
<cur key="1" flags="0" iso="ZAR" name="Rand" symb="R" syprf="1" dchar="." gchar=" " frac="2" rate="0" mdate="0"/>
<account key="1" pos="1" type="6" curr="1" name="Cheque" initial="0" minimum="0" maximum="0"/>
<account key="2" flags="2" pos="2" type="6" curr="1" name="Old" initial="0" minimum="0" maximum="0"/>
<account key="3" pos="3" type="7" curr="1" name="Savings" initial="0" minimum="0" maximum="0"/>
<pay key="1" name="Acme" category="2" paymode="6"/>
<cat key="1" name="Groceries"/>
<cat key="2" name="Household"/>
<cat key="3" name="Bank"/>
<tag key="1" name="work"/>
<asg key="1" flags="4" pos="1" name="SHOP" category="1"/>
<asg key="3" flags="1028" pos="2" name="FEE" category="3"/>
<asg key="6" flags="10" pos="3" name="TEA" payee="1" paymode="3"/>
<asg key="7" flags="16" pos="4" field="1" name="acme" tags="work"/>
<ope date="{j(1)}" amount="-10" account="1" wording="SHOP ONE"/>
<ope date="{j(2)}" amount="-20" account="1" category="2" wording="SHOP ALREADY"/>
<ope date="{j(7)}" amount="-60" account="1" wording="NOTHING MATCHES"/>
<ope date="{j(8)}" amount="-70" account="2" wording="IN CLOSED ACCOUNT"/>
<ope date="{j(6)}" amount="50" account="3" wording="INTEREST"/>
</homebank>
""".encode()

CHEQUE = (
    "01-01-2025;0;;;SHOP ONE;-10.00;;\n"  # already in HomeBank
    "01-01-2025;0;;;SHOP ONE AGAIN;-10.00;;\n"  # same day and amount: the existing one only stands for one
    "10-01-2025;6;;Acme ;TEA AT ACME;-3.50;;\n"
    "11-01-2025;0;REF1;New Shop;SHOP NEW;-15.00;Groceries;work holiday\n"
    "12-01-2025;0;;new shop;MONTHLY FEE;-7.00;Household;\n"
    "13-01-2025;5;;;TO SAVINGS;-100.00;;\n"
    "14-01-2025;0;;;TEA;-2.00;;\n"
    "15-01-2025;0;;;SALARY;1000.00;;\n"
)
SAVINGS = "!Type:Bank\r\nD13/01/2025\r\nT100.00\r\nN0\r\nMFROM CHEQUE  \r\n^\r\n" \
          "D14/01/2025\r\nT-5.00\r\nMSPLIT ONE\r\nSGroceries\r\n$-5.00\r\n^\r\n"


class PlanTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.book = read_bytes(BOOK)
        self.cheque = Source("cheque.csv", 1, read_import_file("cheque.csv", CHEQUE.encode()))
        self.savings = Source("savings.qif", 3, read_import_file("savings.qif", SAVINGS.encode()))

    def plan(self, *sources, **kw):
        return plan_import(self.book, list(sources) or [self.cheque, self.savings], **kw)

    def by_memo(self, plan):
        return {c.txn.memo: c for c in plan.candidates}

    def test_statuses(self):
        plan = self.plan()
        c = self.by_memo(plan)
        self.assertEqual(c["SHOP ONE"].status, IN_HOMEBANK)
        self.assertEqual(c["SHOP ONE"].match.memo, "SHOP ONE")
        self.assertEqual(c["SHOP ONE AGAIN"].status, NEW)
        self.assertEqual(c["SPLIT ONE"].status, SPLIT)
        self.assertEqual(plan.counts(), {NEW: 8, IN_HOMEBANK: 1, SPLIT: 1})
        self.assertEqual(plan.new_payees, ["New Shop"])  # once, whatever the case

        closed = Source("old.csv", 2, read_import_file("old.csv", CHEQUE.encode()))
        self.assertEqual(self.plan(closed).counts(), {CLOSED: 8})
        again = Source("cheque (2).csv", 1, read_import_file("cheque (2).csv", CHEQUE.encode()))
        plan = self.plan(self.cheque, again)
        self.assertEqual([x.status for x in plan.candidates if x.source == 1], [TWICE] * 8)
        self.assertIn("also in cheque.csv", plan.candidates[-1].notes)

    def test_date_tolerance(self):
        moved = Source("c.csv", 1, read_import_file("c.csv", b"03-01-2025;0;;;LATE;-20.00;;\n"))
        self.assertEqual(self.plan(moved).candidates[0].status, NEW)
        self.assertEqual(self.plan(moved, days=1).candidates[0].status, IN_HOMEBANK)
        with self.assertRaises(HbkitError):
            self.plan(moved, days=11)

    def test_homebanks_automation(self):
        c = self.by_memo(self.plan(run_rules=False))
        self.assertEqual((c["TEA"].payee_key, c["TEA"].paymode, c["TEA"].category_key), (0, 0, 0))
        self.assertEqual(c["SHOP NEW"].category_key, 1)  # from the file
        self.assertEqual(c["MONTHLY FEE"].category_key, 2)
        self.assertEqual(c["TO SAVINGS"].paymode, F.PAYMODE_XFER)  # CSV 5 becomes 4
        self.assertIn("a transfer in the file: link it on the Transfers page", c["TO SAVINGS"].notes)
        self.assertEqual(c["SHOP NEW"].tags, ["work"])
        self.assertIn("tags that are not in HomeBank were left out", c["SHOP NEW"].notes)

        c = self.by_memo(self.plan())
        self.assertEqual((c["TEA"].payee_key, c["TEA"].paymode), (1, 3))  # set payee and mode if empty
        self.assertEqual(c["TEA AT ACME"].paymode, 6)  # not empty: kept
        self.assertEqual(c["TEA AT ACME"].category_key, 0)  # payee defaults are off
        self.assertEqual(c["TEA AT ACME"].tags, ["work"])  # payee rule on "Acme"
        self.assertEqual((c["SHOP ONE AGAIN"].category_key, c["SHOP ONE AGAIN"].category_from), (1, "rule"))
        self.assertEqual(c["SHOP NEW"].category_key, 1)  # "set" does not replace the file's category
        self.assertEqual((c["MONTHLY FEE"].category_key, c["MONTHLY FEE"].category_from), (3, "rule"))  # overwrite

        c = self.by_memo(self.plan(payee_defaults=True))
        self.assertEqual((c["TEA AT ACME"].category_key, c["TEA AT ACME"].category_from), (2, "payee default"))

    def test_suggestions_fill_only_empty_categories(self):
        src = Source("cheque.csv", 1, self.cheque.file, suggestions={2: "Household", 3: "Bank", 7: "No such"})
        c = self.by_memo(self.plan(src, run_rules=False))
        self.assertEqual((c["TEA AT ACME"].category_key, c["TEA AT ACME"].category_from), (2, "suggested"))
        self.assertEqual(c["SHOP NEW"].category_key, 1)  # the file's own category wins
        self.assertIn("its category is not in HomeBank: left empty", c["SALARY"].notes)


class ImportTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.tmp / "finances.xhb"
        self.src.write_bytes(BOOK)
        self.book = read_bytes(BOOK)
        self.sources = [Source("cheque.csv", 1, read_import_file("cheque.csv", CHEQUE.encode())),
                        Source("savings.qif", 3, read_import_file("savings.qif", SAVINGS.encode()))]
        self.plan = plan_import(self.book, self.sources)

    def test_new_copy(self):
        out = default_output(self.tmp / "out")
        summary, path = import_transactions(self.book, self.plan, None, self.src, out, self.plan.source_sha256)
        self.assertEqual(path, out)
        self.assertEqual(self.src.read_bytes(), BOOK)
        rows = {r["label"]: r["text"] for r in summary.to_dict()["rows"]}
        self.assertEqual((rows["Transactions imported"], rows["New payees added"]), ("8", "1"))
        new = read_bytes(path.read_bytes())
        self.assertEqual(len(new.transactions), 5 + 8)
        self.assertEqual(new.payees[2].name, "New Shop")
        t = {x.memo: x for x in new.transactions}
        self.assertEqual((t["SHOP NEW"].payee_key, t["MONTHLY FEE"].payee_key), (2, 2))
        self.assertEqual((t["SHOP NEW"].info, t["SHOP NEW"].tags, t["SHOP NEW"].category_key), ("REF1", ["work"], 1))
        self.assertEqual(t["SALARY"].flags, F.OF_INCOME)
        self.assertEqual(t["SHOP ONE AGAIN"].flags, 0)
        self.assertEqual((t["TEA"].payee_key, t["TEA"].paymode), (1, 3))
        qif = t["FROM CHEQUE  "]  # HomeBank keeps a QIF memo's spaces
        self.assertEqual((qif.account_key, qif.info, qif.amount, qif.date_julian), (3, "0", 100.0, j(13)))
        data = path.read_bytes()
        for x in new.transactions[-8:] + new.transactions[:5]:
            self.assertEqual(data[x.src.start:x.src.end].decode(), S.serialise_transaction(x))
        # Placed after the account's latest earlier transaction, in date order.
        order = [(x.account_key, x.memo) for x in new.transactions]
        self.assertEqual(order[:4], [(1, "SHOP ONE"), (1, "SHOP ONE AGAIN"), (1, "SHOP ALREADY"), (1, "NOTHING MATCHES")])
        self.assertEqual([m for a, m in order if a == 1][-6:],
                         ["TEA AT ACME", "SHOP NEW", "MONTHLY FEE", "TO SAVINGS", "TEA", "SALARY"])
        self.assertEqual(order[-1], (3, "FROM CHEQUE  "))

    def test_choose_and_refuse(self):
        dup = next(c.id for c in self.plan.candidates if c.status == IN_HOMEBANK)
        split = next(c.id for c in self.plan.candidates if c.status == SPLIT)
        summary, path = import_transactions(self.book, self.plan, {dup, split}, self.src, self.tmp / "o.xhb")
        rows = {r["label"]: r["text"] for r in summary.to_dict()["rows"]}
        self.assertEqual((rows["Transactions imported"], rows["Chosen but not importable (skipped)"]), ("1", "1"))
        self.assertEqual(len(read_bytes(path.read_bytes()).transactions), 6)
        self.assertIsNone(import_transactions(self.book, self.plan, set(), self.src, self.tmp / "n.xhb")[1])
        with self.assertRaises(HbkitError):
            import_transactions(self.book, self.plan, None, self.src, self.tmp / "x.xhb", "0" * 64)
        with self.assertRaises(HbkitError):
            import_transactions(read_bytes(BOOK.replace(b'title="T"', b'title="U"')), self.plan, None, self.src,
                                self.tmp / "y.xhb")

    def test_into_the_file_itself(self):
        backups = self.tmp / "out" / "backups"
        summary, path = import_transactions(self.book, self.plan, None, self.src, None, None, backups)
        self.assertEqual(path, self.src)
        self.assertEqual(len(read_bytes(self.src.read_bytes()).transactions), 13)
        self.assertEqual([p.read_bytes() for p in backups.iterdir()], [BOOK])
        again = read_bytes(self.src.read_bytes())
        self.assertEqual(plan_import(again, self.sources).counts(), {IN_HOMEBANK: 9, SPLIT: 1})  # nothing new now


class SyntheticFileTests(TempDirTestCase):
    def test_generated_file_with_bank_files(self):
        from hbkit.fixtures.generator import ACC_CHEQUE, ACC_LOAN_A

        from .test_import_prepare import CHEQUE as CHEQUE_OCT
        from .test_import_prepare import LOAN_A

        src = self.write_sample("finances.xhb", home_loans=True)
        book = read_bytes(src.read_bytes())
        sources = [Source("Loan_A.qif", ACC_LOAN_A, read_import_file("Loan_A.qif", LOAN_A)),
                   Source("transact.csv", ACC_CHEQUE, read_import_file("transact.csv", CHEQUE_OCT.encode()))]
        plan = plan_import(book, sources)
        self.assertEqual(plan.counts()[IN_HOMEBANK], 1)  # September's bond insurance premium
        summary, path = import_transactions(book, plan, None, src, self.tmp / "with.xhb")
        new = read_bytes(path.read_bytes())
        self.assertEqual(len(new.transactions), len(book.transactions) + plan.counts()[NEW])
        self.assertEqual(new.warnings, book.warnings)


class CommandTests(TempDirTestCase):
    def run_cli(self, *args):
        from .helpers import run_cli

        return run_cli(*args, "--out", str(self.tmp / "out"), "--data", str(self.tmp / "data"))

    def test_prepare_then_import(self):
        from hbkit.fixtures.generator import SENTINEL

        from .test_import_prepare import LOAN_A

        src = self.write_sample("s.xhb", home_loans=True, sentinel=True)
        bank = self.tmp / "Loan_A.qif"
        bank.write_bytes(LOAN_A.replace(b"MSOMETHING NEW", f"M{SENTINEL}MEMO_SOMETHING NEW".encode()))
        code, _, err = self.run_cli("prepare-import", "--file", str(src), "--input", str(bank),
                                    "--account", f"{SENTINEL}ACC_Home loan A")
        self.assertEqual(code, 0, err)
        before = src.read_bytes()
        code, out, err = self.run_cli("import-files", "--file", str(src), "--month", "2025-10")
        self.assertEqual(code, 0, err)
        self.assertNoSentinel(out, err)
        self.assertIn("Transactions imported", out)
        self.assertEqual(src.read_bytes(), before)
        copy = next((self.tmp / "out").glob("with-imports_*.xhb"))
        new = read_bytes(copy.read_bytes())
        self.assertEqual(len(new.transactions), len(read_bytes(before).transactions) + 3)  # one was already there
        self.assertTrue(any(SENTINEL in t.memo for t in new.transactions))
        listing = next((self.tmp / "out" / "import-files").iterdir()) / "transactions.csv"
        self.assertIn("in HomeBank", listing.read_text(encoding="utf-8-sig"))

        code, out, err = self.run_cli("import-files", "--file", str(src), "--month", "2025-10", "--in-place")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(read_bytes(src.read_bytes()).transactions), len(new.transactions))
        self.assertEqual(len(list((self.tmp / "out" / "backups").iterdir())), 1)

        self.assertEqual(self.run_cli("import-files", "--file", str(src), "--month", "2025-11")[0], 2)  # nothing linked
