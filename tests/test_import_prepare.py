"""Predicting categories for import files, remembering file names, and preparing files.

Uses the synthetic file with two home loans (it ends in September 2025) and made-up
import files for October 2025.
"""

import json

from hbkit.errors import HbkitError
from hbkit.fixtures.generator import ACC_CHEQUE, ACC_LOAN_A, ACC_LOAN_B, ACC_OLD, ACC_SAVINGS
from hbkit.imports.formats import read_import_file
from hbkit.imports.memory import ImportMemory, check_month, name_key
from hbkit.imports.predict import History, clear_winner, guess_accounts, predict
from hbkit.imports.session import (
    add_file, file_info, month_accounts, month_files, prepare, prepared_name, preview, safe_name, save_prepared,
)
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample

LOAN_A = (
    "!Type:Bank\r\n"
    "D02/09/2025\r\nT-310.00\r\nN0\r\nMBOND INSURANCE PREMIUM POLICY 4410023  \r\n^\r\n"  # also in September's file
    "D02/10/2025\r\nT-310.00\r\nN0\r\nMBOND INSURANCE PREMIUM POLICY 4410023  \r\n^\r\n"
    "D28/10/2025\r\nT-6150.25\r\nN0\r\nMHOME LOAN INTEREST  \r\n^\r\n"
    "D29/10/2025\r\nT-12.00\r\nN0\r\nMSOMETHING NEW  \r\n^\r\n"
).encode()

CHEQUE = (
    "03/10/2025;;;;NETFLIX.COM LOS GATOS 4587*1234 03 OCT;-229.00;;\n"
    "05/10/2025;;;;POS PURCHASE WOOLWORTHS SANDTON 4587*1234 05 OCT;-812.40;;\n"
    "25/10/2025;9;;;MAGTAPE CREDIT ACME CORP SALARY REF 123456;35000.00;;\n"
    "26/10/2025;5;;To: Savings;TRANSFER TO SAVINGS;-2000.00;;\n"
    "01/10/2025;11;;;DEBIT ORDER VIRGIN ACTIVE 0012345678;-650.00;;\n"
)


class PredictionTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = read_bytes(sample(home_loans=True).data)
        cls.history = History(cls.book)
        cls.loan = read_import_file("Loan_A.qif", LOAN_A)
        cls.cheque = read_import_file("transact.csv", CHEQUE.encode())

    def path(self, key):
        return self.book.category_path(key) if key else ""

    def test_categories_follow_the_account(self):
        a = predict(self.history, ACC_LOAN_A, self.loan)
        b = predict(self.history, ACC_LOAN_B, self.loan)
        self.assertEqual(self.path(a[2].category), "Property A:Bond interest")
        self.assertEqual(self.path(b[2].category), "Property B:Bond interest")  # same text, other account
        self.assertEqual((a[2].source, a[2].evidence), ("this account", "10 of the last 10 in this account"))
        self.assertEqual(self.path(a[1].category), "Property A:Insurance")
        self.assertEqual((a[3].category, a[3].source), (0, ""))  # never seen

    def test_duplicates_use_the_exact_text(self):
        a = predict(self.history, ACC_LOAN_A, self.loan)
        b = predict(self.history, ACC_LOAN_B, self.loan)
        self.assertEqual([p.duplicate for p in a], [True, False, False, False])
        self.assertFalse(any(p.duplicate for p in b))  # loan B's policy number differs

    def test_guess_the_account_from_the_contents(self):
        guesses = guess_accounts(self.history, self.loan)
        self.assertEqual([g.account for g in guesses[:2]], [ACC_LOAN_A, ACC_LOAN_B])
        best = clear_winner(guesses)
        self.assertEqual(best.account, ACC_LOAN_A)
        self.assertEqual((best.duplicates, best.exact, best.close), (1, 3, 3))
        self.assertTrue(any("already in this account" in r for r in best.reasons))
        self.assertEqual(clear_winner(guess_accounts(self.history, self.cheque)).account, ACC_CHEQUE)

    def test_identical_texts_are_told_apart_by_amount(self):
        # Interest only, same text in both loans: the amount close to the latest one decides.
        raw = "!Type:Bank\nD28/10/2025\nT-4080.10\nMHOME LOAN INTEREST\n^\n".encode()
        guesses = guess_accounts(self.history, read_import_file("x.qif", raw))
        best = clear_winner(guesses)
        self.assertEqual((best.account, best.close), (ACC_LOAN_B, 1))
        self.assertAlmostEqual(guesses[0].score, 1.5)  # exact text shared (0.5) + the close amount (1)

    def test_all_accounts_and_homebank_rules(self):
        own = predict(self.history, ACC_CHEQUE, self.cheque)
        other = predict(self.history, ACC_SAVINGS, self.cheque)
        self.assertEqual((self.path(own[1].category), own[1].source), ("Groceries", "this account"))
        self.assertEqual((self.path(other[1].category), other[1].source), ("Groceries", "all accounts"))
        self.assertEqual((own[0].homebank_by, self.path(own[0].homebank_category)), ("rule E1", "Entertainment:Streaming"))
        self.assertEqual((own[2].overwritten_by, self.path(own[2].overwrite_category)), ("E9", "Salary"))
        self.assertEqual(own[3].skip, "transfer")

    def test_recent_habit_wins(self):
        # The gym debit order moved from Personal care to Sport & fitness three years in.
        p = predict(self.history, ACC_CHEQUE, self.cheque)[4]
        self.assertEqual((self.path(p.category), p.source), ("Sport & fitness", "this account"))

    def test_preview_rows(self):
        rows = preview(self.book, self.history, self.loan, ACC_LOAN_A)
        self.assertEqual([r["suggested"] for r in rows],
                         ["Property A:Insurance", "Property A:Insurance", "Property A:Bond interest", ""])
        self.assertEqual(rows[2]["memo"], "HOME LOAN INTEREST")
        with self.assertRaises(HbkitError):
            preview(self.book, self.history, self.loan, 999)

    def test_prepare_uses_homebanks_spelling(self):
        data, n = prepare(self.book, self.loan, {2: "property a:bond INTEREST", 3: ""})
        self.assertEqual(n, 1)
        self.assertIn(b"MHOME LOAN INTEREST  \r\nLProperty A:Bond interest\r\n^", data)
        with self.assertRaises(HbkitError):
            prepare(self.book, self.loan, {3: "No such category"})

    def test_month_accounts(self):
        memory = ImportMemory(self.tmp / "m.json")
        rows = {r["key"]: r for r in month_accounts(self.book, self.history, memory, "2025-10")}
        self.assertTrue(rows[ACC_LOAN_A]["expected"] and rows[ACC_CHEQUE]["expected"])
        self.assertFalse(rows[ACC_OLD]["expected"])  # closed
        self.assertEqual(rows[ACC_LOAN_A]["last_date"], "2025-09-28")
        self.assertEqual(rows[ACC_LOAN_A]["in_month"], 0)
        self.assertGreater(month_accounts(self.book, self.history, memory, "2025-09")[0]["in_month"], 0)
        memory.expected_accounts = [ACC_LOAN_B]
        rows = {r["key"]: r for r in month_accounts(self.book, self.history, memory, "2025-10")}
        self.assertEqual([k for k, r in rows.items() if r["expected"]], [ACC_LOAN_B])
        self.assertFalse(month_accounts(self.book, self.history, memory, "2030-01")[0]["active"])


class MemoryTests(TempDirTestCase):
    def test_name_keys(self):
        same = ["transact.csv", "transact (1).csv", "Transact_20260531.csv", "transact - Copy.csv",
                "transact 2026-05.csv", "transact_20260531_153012.csv", "transact May 2026.csv"]
        self.assertEqual({name_key(n) for n in same}, {"transact.csv"})
        self.assertEqual(name_key("Flat_12.qif"), "flat_12.qif")  # other numbers are kept
        self.assertNotEqual(name_key("Flat_12.qif"), name_key("Flat_13.qif"))
        self.assertNotEqual(name_key("transact.csv"), name_key("transact.qif"))
        self.assertEqual(name_key("Statement 31-05-2026.qif"), "statement.qif")
        self.assertEqual(name_key("2026-05.qif"), "file.qif")

    def test_links_and_predictions(self):
        path = self.tmp / "data" / "import-accounts.json"
        m = ImportMemory(path)
        self.assertIsNone(m.predict("Loan_A.qif"))
        m.link("2025-09", "Loan_A.qif", 7, "Home loan A", transactions=3)
        m.link("2025-09", "transact.csv", 1, "Cheque account")
        m.link("2025-08", "transact.csv", 3, "Savings")
        m.save()
        m = ImportMemory(path)
        self.assertEqual(m.predict("Loan_A.qif", "2025-10"), (7, "same file name as in 2025-09"))
        self.assertEqual(m.predict("transact (1).csv", "2025-10"),
                         (1, "similar file name (transact.csv) in 2025-09"))  # the latest link wins
        self.assertIsNone(m.predict("Loan_A.qif", "2025-09"))  # not from the month itself
        m.link("2025-09", "Loan_A.qif", 8, "Home loan B")  # relinking replaces
        self.assertEqual([x["account"] for x in m.for_month("2025-09")], [1, 8])
        m.unlink("2025-09", "transact.csv")
        self.assertIsNone(m.get("2025-09", "transact.csv"))
        self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_bad_input(self):
        for month in ("2025-13", "2025-1", "May", ""):
            with self.assertRaises(HbkitError):
                check_month(month)
        path = self.tmp / "broken.json"
        path.write_text("{not json")
        with self.assertRaises(HbkitError):
            ImportMemory(path)


class FileTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = read_bytes(sample(home_loans=True).data)
        cls.history = History(cls.book)

    def test_names(self):
        self.assertEqual(safe_name("..\\..\\Loan A.qif"), "Loan A.qif")
        self.assertEqual(safe_name("../x/../y:z?.csv"), "y_z_.csv")
        self.assertEqual(prepared_name("Loan_A.qif"), "Loan_A-prepared.qif")
        with self.assertRaises(HbkitError):
            safe_name("..")

    def test_add_files(self):
        out = self.tmp / "out"
        p1 = add_file(out, "2025-10", "Loan_A.qif", LOAN_A)
        self.assertEqual(p1, out / "imports" / "2025-10" / "originals" / "Loan_A.qif")
        self.assertEqual(add_file(out, "2025-10", "Loan_A.qif", LOAN_A), p1)  # same content: kept once
        p2 = add_file(out, "2025-10", "Loan_A.qif", LOAN_A + b"\r\n")
        self.assertEqual(p2.name, "Loan_A (2).qif")
        self.assertEqual([p.name for p in month_files(out, "2025-10")], ["Loan_A (2).qif", "Loan_A.qif"])
        with self.assertRaises(HbkitError):
            add_file(out, "2025-10", "notes.csv", b"just some text\n")

    def test_file_info_and_saving(self):
        out = self.tmp / "out"
        memory = ImportMemory(self.tmp / "m.json")
        path = add_file(out, "2025-10", "Loan_A.qif", LOAN_A)
        info = file_info(self.book, self.history, memory, "2025-10", path)
        self.assertEqual((info.account, info.suggested), (None, ACC_LOAN_A))
        self.assertTrue(info.reason.startswith("from its contents"))
        self.assertEqual((info.transactions, info.format, info.prepared), (4, "qif", ""))

        memory.link("2025-09", "Loan_A.qif", ACC_LOAN_B, "Home loan B")  # remembered, but wrong
        info = file_info(self.book, self.history, memory, "2025-10", path)
        self.assertEqual((info.suggested, info.reason), (ACC_LOAN_B, "same file name as in 2025-09"))
        self.assertIn("look like", info.warning)  # its contents point to loan A

        memory.link("2025-10", "Loan_A.qif", ACC_LOAN_A, "Home loan A")
        data, _ = prepare(self.book, read_import_file(path.name, path.read_bytes()), {2: "Property A:Bond interest"})
        target = save_prepared(path, data)
        self.assertEqual(target.name, "Loan_A-prepared.qif")
        self.assertEqual(path.read_bytes(), LOAN_A)  # the original is untouched
        info = file_info(self.book, self.history, memory, "2025-10", path)
        self.assertEqual((info.account, info.reason, info.warning), (ACC_LOAN_A, "linked by you", ""))
        self.assertEqual(info.prepared, str(target))


class PrepareCommandTests(TempDirTestCase):
    def run_prepare(self, src, bank_file, *extra):
        from .helpers import run_cli

        return run_cli("prepare-import", "--file", str(src), "--input", str(bank_file), "--out", str(self.tmp / "out"),
                       "--data", str(self.tmp / "data"), *extra)

    def test_prepare_and_remember(self):
        src = self.write_sample("loans.xhb", home_loans=True)
        bank = self.tmp / "Loan_A.qif"
        bank.write_bytes(LOAN_A)
        code, out, err = self.run_prepare(src, bank)
        self.assertEqual(code, 0, err)
        self.assertIn("recognised from its contents", out)
        prepared = self.tmp / "out" / "imports" / "2025-10" / "prepared" / "Loan_A-prepared.qif"
        self.assertIn(b"LProperty A:Bond interest", prepared.read_bytes())
        self.assertEqual(bank.read_bytes(), LOAN_A)
        memory = ImportMemory(self.tmp / "data" / "import-accounts.json")
        self.assertEqual(memory.get("2025-10", "Loan_A.qif")["account"], ACC_LOAN_A)

        nov = self.tmp / "nov" / "Loan_A.qif"
        nov.parent.mkdir()
        nov.write_bytes(LOAN_A.replace(b"/10/2025", b"/11/2025").replace(b"MHOME LOAN INTEREST", b"MSOMETHING ELSE"))
        code, out, err = self.run_prepare(src, nov)
        self.assertEqual(code, 0, err)
        self.assertIn("remembered for this file name", out)

        code, out, err = self.run_prepare(src, bank, "--account", "home loan b", "--month", "2025-12")
        self.assertEqual(code, 0, err)
        self.assertIn("given with --account", out)
        self.assertEqual(ImportMemory(self.tmp / "data" / "import-accounts.json").get("2025-12", "Loan_A.qif")["account"],
                         ACC_LOAN_B)

    def test_errors(self):
        src = self.write_sample("loans.xhb", home_loans=True)
        bank = self.tmp / "x.qif"
        bank.write_bytes(LOAN_A)
        self.assertEqual(self.run_prepare(src, bank, "--account", "No such account")[0], 2)
        self.assertEqual(self.run_prepare(src, self.tmp / "missing.qif")[0], 2)
        unknown = self.tmp / "unknown.qif"
        unknown.write_bytes(b"!Type:Bank\nD01/10/2025\nT-1.00\nMNEVER SEEN BEFORE\n^\n")
        code, _, err = self.run_prepare(src, unknown)
        self.assertEqual(code, 2)
        self.assertIn("give it with --account", err)

    def test_privacy(self):
        from hbkit.fixtures.generator import SENTINEL

        src = self.write_sample("s.xhb", home_loans=True, sentinel=True)
        bank = self.tmp / "bank.qif"
        bank.write_bytes(LOAN_A.replace(b"MHOME LOAN INTEREST", f"M{SENTINEL}MEMO_HOME LOAN INTEREST".encode()))
        code, out, err = self.run_prepare(src, bank, "--account", f"{SENTINEL}ACC_Home loan A")
        self.assertEqual(code, 0, err)
        self.assertNoSentinel(out, err)
        prepared = next((self.tmp / "out").rglob("bank-prepared.qif")).read_bytes()
        self.assertIn(f"L{SENTINEL}CAT_Property A:{SENTINEL}CAT_Bond interest".encode(), prepared)


class BankCsvPredictionTests(TempDirTestCase):
    def test_categories_for_a_bank_csv(self):
        from .test_import_formats import BANK_CSV

        book = read_bytes(sample().data)
        history = History(book)
        f = read_import_file("cheque.csv", BANK_CSV.encode())
        self.assertEqual(clear_winner(guess_accounts(history, f)).account, ACC_CHEQUE)
        rows = preview(book, history, f, ACC_CHEQUE)
        self.assertEqual(rows[0]["suggested"], "Groceries")
        self.assertEqual(rows[1]["suggested"], "Sport & fitness")  # the recent habit
        data, n = prepare(book, f, {r["index"]: r["suggested"] for r in rows if r["suggested"]})
        again = read_import_file("cheque-prepared.csv", data)
        self.assertEqual(again.format, "csv")
        self.assertEqual(sum(1 for t in again.txns if t.category), n)
