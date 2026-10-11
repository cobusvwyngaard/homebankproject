"""Reading and preparing import files (QIF and HomeBank CSV). All files are made up."""

import unittest
from datetime import date

from hbkit.imports.formats import (
    ImportFormatError, date_order, parse_amount, parse_date, read_import_file, with_categories,
)

QIF = (
    "!Type:Bank\r\n\r\n"
    "D28/09/2025\r\nT-6150.25\r\nN0\r\nMHOME LOAN INTEREST  \r\n^\r\n"
    "D02/09/2025\r\nT-310.00\r\nN0\r\nMBOND INSURANCE PREMIUM POLICY 4410023  \r\n^\r\n"
    "D15/09/2025\r\nT5000.00\r\nN0\r\nMPAYMENT RECEIVED\r\nL[Cheque account]\r\n^\r\n"
    "D16/09/2025\r\nT-100.00\r\nMALREADY DONE\r\nLGroceries\r\n^\r\n"
    "D17/09/2025\r\nT-80.00\r\nMEMPTY CATEGORY LINE\r\nL\r\n^\r\n"
    "D18/09/2025\r\nT-90.00\r\nMSPLIT ONE\r\nSGroceries\r\n$-60.00\r\nSHousehold\r\n$-30.00\r\n^\r\n"
).encode()

CSV = (
    "26-09-2025;5;;To: Savings;TRANSFER TO SAVINGS;-2000.00;;\n"
    "24-09-2025;;;;POS PURCHASE WOOLWORTHS SANDTON 4587*1234 24 SEP;-512.40;;\n"
    '23-09-2025;6;;"Shop; the best";"QUOTED ""MEMO""";-10.00;;\n'
    "22-09-2025;;;;HAS CATEGORY;-5.00;Groceries;\n"
)


class ValueTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_date("24/04/2026"), date(2026, 4, 24))
        self.assertEqual(parse_date("24-04-2026"), date(2026, 4, 24))
        self.assertEqual(parse_date("2026-04-24"), date(2026, 4, 24))
        self.assertEqual(parse_date("24/04'26"), date(2026, 4, 24))
        self.assertEqual(parse_date("4/24/2026", "MDY"), date(2026, 4, 24))
        self.assertIsNone(parse_date("31/02/2026"))
        self.assertIsNone(parse_date("soon"))

    def test_date_order_from_the_whole_file(self):
        self.assertEqual(date_order(["01/02/2026", "24/02/2026"]), "DMY")
        self.assertEqual(date_order(["02/01/2026", "02/24/2026"]), "MDY")
        self.assertEqual(date_order(["01/02/2026"]), "DMY")  # ambiguous: South African order
        self.assertEqual(date_order(["2026/02/24"]), "YMD")

    def test_amounts(self):
        self.assertEqual(parse_amount("-1,876.40"), -1876.40)
        self.assertEqual(parse_amount("1 234,56"), 1234.56)
        self.assertEqual(parse_amount("1.234,56"), 1234.56)
        self.assertEqual(parse_amount("69"), 69.0)
        self.assertIsNone(parse_amount(""))


class QifTests(unittest.TestCase):
    def setUp(self):
        self.f = read_import_file("loan.qif", QIF)

    def test_read(self):
        f = self.f
        self.assertEqual((f.format, f.qif_type, f.encoding, f.date_order), ("qif", "Bank", "utf-8", "DMY"))
        self.assertEqual(len(f.txns), 6)
        t = f.txns[0]
        self.assertEqual((t.date, t.amount, t.memo, t.info), (date(2025, 9, 28), -6150.25, "HOME LOAN INTEREST", "0"))
        self.assertTrue(f.txns[2].transfer)
        self.assertEqual(f.txns[3].category, "Groceries")
        self.assertTrue(f.txns[5].split)
        self.assertEqual([t.can_take_category for t in f.txns], [True, True, False, False, True, False])
        self.assertEqual((f.first_date, f.last_date), (date(2025, 9, 2), date(2025, 9, 28)))
        self.assertEqual(f.problems, [])

    def test_unchanged_without_categories(self):
        self.assertEqual(with_categories(self.f, {}), QIF)

    def test_categories_added_and_nothing_else(self):
        out = with_categories(self.f, {0: "Property A:Bond interest", 4: "Household"})
        self.assertEqual(out.replace(b"LProperty A:Bond interest\r\n", b"", 1).replace(b"LHousehold", b"L"), QIF)
        self.assertIn(b"MHOME LOAN INTEREST  \r\nLProperty A:Bond interest\r\n^", out)
        again = read_import_file("loan.qif", out)
        self.assertEqual(again.txns[0].category, "Property A:Bond interest")
        self.assertEqual(again.txns[4].category, "Household")

    def test_refusals(self):
        for index in (2, 3, 5, 99):  # transfer, has a category, split, does not exist
            with self.assertRaises(ImportFormatError):
                with_categories(self.f, {index: "Groceries"})
        for bad in ("", " Groceries", "[Savings]", "Line\nbreak"):
            with self.assertRaises(ImportFormatError):
                with_categories(self.f, {0: bad})

    def test_lf_endings_and_missing_final_caret(self):
        raw = b"!Type:CCard\nD01/10/2025\nT-1.00\nPSHOP\n^\nD02/10/2025\nT-2.00\nPSHOP"
        f = read_import_file("card.qif", raw)
        self.assertEqual((f.qif_type, len(f.txns), f.problems), ("CCard", 1, [6]))
        self.assertEqual(with_categories(f, {0: "Groceries"}),
                         b"!Type:CCard\nD01/10/2025\nT-1.00\nPSHOP\nLGroceries\n^\nD02/10/2025\nT-2.00\nPSHOP")

    def test_windows_1252_file_keeps_its_encoding(self):
        raw = "!Type:Bank\r\nD01/10/2025\r\nT-1.00\r\nMCAFÉ\r\n^\r\n".encode("cp1252")
        f = read_import_file("cafe.qif", raw)
        self.assertEqual((f.encoding, f.txns[0].memo), ("cp1252", "CAFÉ"))
        out = with_categories(f, {0: "Uitgawes:Koffie é"})
        self.assertIn("LUitgawes:Koffie é\r\n".encode("cp1252"), out)
        with self.assertRaises(ImportFormatError):
            with_categories(f, {0: "Food ✓"})  # not possible in Windows-1252

    def test_utf8_category_in_ascii_file(self):
        out = with_categories(self.f, {0: "Eiendom:Rente ê"})
        self.assertEqual(read_import_file("x.qif", out).txns[0].category, "Eiendom:Rente ê")

    def test_not_transactions(self):
        with self.assertRaises(ImportFormatError):
            read_import_file("cats.qif", b"!Type:Cat\nNGroceries\n^\n")


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.raw = CSV.encode()
        self.f = read_import_file("transact.csv", self.raw)

    def test_read(self):
        f = self.f
        self.assertEqual((f.format, f.separator, f.date_order, len(f.txns)), ("csv", ";", "DMY", 4))
        self.assertTrue(f.txns[0].transfer)  # payment mode 5
        t = f.txns[1]
        self.assertEqual((t.date, t.amount, t.payee), (date(2025, 9, 24), -512.40, ""))
        self.assertEqual((f.txns[2].payee, f.txns[2].memo), ("Shop; the best", 'QUOTED "MEMO"'))
        self.assertEqual(f.txns[3].category, "Groceries")
        self.assertEqual([t.can_take_category for t in f.txns], [False, True, True, False])

    def test_category_column_filled(self):
        out = with_categories(self.f, {1: "Groceries", 2: "Odd; name"})
        lines = out.decode().splitlines()
        self.assertEqual(lines[1], "24-09-2025;;;;POS PURCHASE WOOLWORTHS SANDTON 4587*1234 24 SEP;-512.40;Groceries;")
        self.assertEqual(lines[2], '23-09-2025;6;;"Shop; the best";"QUOTED ""MEMO""";-10.00;"Odd; name";')
        self.assertEqual([lines[0], lines[3]], CSV.splitlines()[0:4:3])
        self.assertEqual(read_import_file("t.csv", out).txns[2].category, "Odd; name")

    def test_header_and_other_lines_are_left_alone(self):
        raw = b"date;paymode;info;payee;memo;amount;category;tags\r\n" + self.raw.replace(b"\n", b"\r\n")
        f = read_import_file("t.csv", raw)
        self.assertEqual((len(f.txns), f.problems), (4, [1]))
        out = with_categories(f, {1: "Groceries"})
        self.assertTrue(out.startswith(b"date;paymode;info;payee;memo;amount;category;tags\r\n"))
        self.assertEqual(out.count(b"\r\n"), raw.count(b"\r\n"))

    def test_comma_separated(self):
        f = read_import_file("t.csv", b"24-09-2025,,,,SHOP,-1.00,,\n")
        self.assertEqual((f.separator, len(f.txns)), (",", 1))

    def test_other_files_are_refused(self):
        for raw in (b"Date,Description,Amount\n2025-09-24,SHOP,-1.00\n", b"", b"hello"):
            with self.assertRaises(ImportFormatError):
                read_import_file("bank.csv", raw)


if __name__ == "__main__":
    unittest.main()
