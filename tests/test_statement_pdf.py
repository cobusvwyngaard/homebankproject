"""PDF bank statements: the text reader, the statement table and the conversion (synthetic PDFs only)."""

import unittest
from datetime import date

from hbkit.fixtures.statement_pdf import SAMPLE_ROWS, StatementOptions, StatementRow, statement_pdf
from hbkit.imports.formats import ImportFormatError, read_import_file, with_categories
from hbkit.imports.pdf import Glyph, PdfError, _cmap_unicode, glyph_text, lines, page_glyphs
from hbkit.imports.statement import amount_of

EXPECTED = [  # (date, amount, payment mode, memo, info)
    (date(2026, 3, 2), -14.0, 10, "MONTHLY ACCOUNT FEE", "000417"),
    (date(2026, 3, 6), 35000.0, 0, "MAGTAPE CREDIT ACME CORP SALARY", ""),
    (date(2026, 3, 10), -650.0, 0, "DEBIT ORDER VIRGIN ACTIVE 0012345678", ""),
    (date(2026, 3, 10), -3.5, 10, "DEBIT ORDER FEE", ""),
    (date(2026, 3, 14), -1234.56, 0, "POS PURCHASE WOOLWORTHS SANDTON 4587*1234 14 MAR", ""),  # wrapped
    (date(2026, 3, 18), -2100.0, 0, "IB PAYMENT TO CITY OF JOBURG", ""),
    (date(2026, 3, 18), -5.5, 10, "IB PAYMENT TO CITY OF JOBURG (fee)", ""),  # from the fees column
    (date(2026, 3, 25), 750.0, 0, "TRANSFER FROM SAVINGS", ""),
]


def read(**options):
    return read_import_file("statement.pdf", statement_pdf(SAMPLE_ROWS, StatementOptions(**options)))


class StatementTests(unittest.TestCase):
    def check(self, f):
        self.assertEqual(f.format, "pdf-statement")
        self.assertEqual([(t.date, t.amount, t.paymode, t.memo, t.info) for t in f.txns], EXPECTED)
        self.assertEqual(f.problems, [])

    def test_standard_font(self):
        f = read()
        self.check(f)
        self.assertEqual(f.layout, "Tran list no Date, Description, Fees (R), Debits (R), Credits (R), Balance (R)")
        self.assertIn("62000000001", f.numbers)  # the account number on the summary page

    def test_embedded_font_with_its_own_encoding(self):
        self.check(read(font="embedded", compress=False))

    def test_composite_font_over_several_pages(self):
        f = read(font="composite", rows_per_page=3)
        self.check(f)
        self.assertEqual(len(page_glyphs(statement_pdf(SAMPLE_ROWS, StatementOptions(rows_per_page=3)))), 4)

    def test_balances_must_add_up(self):
        with self.assertRaises(ImportFormatError) as ctx:
            read(wrong_balance_at=2)
        self.assertIn("do not add up", str(ctx.exception))

    def test_refusals(self):
        with self.assertRaises(PdfError):
            read(encrypted=True)
        with self.assertRaises(ImportFormatError):
            read_import_file("x.pdf", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n")  # no text at all
        no_table = statement_pdf([], StatementOptions())
        no_table = no_table.replace(b"Description", b"Particulars")  # header no longer recognised
        with self.assertRaises(ImportFormatError):
            read_import_file("x.pdf", no_table)
        with self.assertRaises(ImportFormatError):
            read_import_file("x.pdf", statement_pdf([], StatementOptions()))  # a table without transactions

    def test_converted_to_homebank_csv(self):
        f = read()
        out = with_categories(f, {1: "Salary", 3: "Bank charges"})
        lines_out = out.decode().split("\r\n")
        self.assertEqual(lines_out[0], "02-03-2026;10;000417;;MONTHLY ACCOUNT FEE;-14.00;;")
        self.assertEqual(lines_out[1], "06-03-2026;0;;;MAGTAPE CREDIT ACME CORP SALARY;35000.00;Salary;")
        again = read_import_file("statement-prepared.csv", out)
        self.assertEqual((again.format, len(again.txns)), ("csv", 8))

    def test_unknown_row_is_reported(self):
        rows = SAMPLE_ROWS + [StatementRow("31/02/2026", "NOT A REAL DATE", debit=1.0)]
        with self.assertRaises(ImportFormatError):  # an unreadable row also breaks the balance chain
            read_import_file("x.pdf", statement_pdf(rows))


class ReaderPartsTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(amount_of("7,281.40"), (7281.40, ""))
        self.assertEqual(amount_of("3.50 *"), (3.5, "*"))
        self.assertEqual(amount_of("12.50-"), (-12.5, "-"))
        self.assertEqual(amount_of("100.00 Cr"), (100.0, "cr"))
        self.assertEqual(amount_of("100.00 Dr"), (-100.0, "dr"))
        self.assertIsNone(amount_of("10000000002 991231"))
        self.assertIsNone(amount_of("02/03/2026"))

    def test_glyph_names(self):
        self.assertEqual(glyph_text("A"), "A")
        self.assertEqual(glyph_text("zero"), "0")
        self.assertEqual(glyph_text("eacute"), "é")
        self.assertEqual(glyph_text("uni20AC"), "€")
        self.assertEqual(glyph_text("comma.alt"), ",")
        self.assertEqual(glyph_text("nonsense_glyph"), "�")

    def test_tounicode_maps(self):
        cmap = (b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                b"2 beginbfchar <0003> <0020> <0024> <00410042> endbfchar\n"
                b"1 beginbfrange <0010> <0012> <0061> endbfrange\n"
                b"1 beginbfrange <0020> <0021> [<0058> <0059>] endbfrange\n")
        mapping, width = _cmap_unicode(cmap)
        self.assertEqual(width, 2)
        self.assertEqual((mapping[3], mapping[0x24], mapping[0x10], mapping[0x12], mapping[0x21]),
                         (" ", "AB", "a", "c", "Y"))

    def test_lines_and_chunks(self):
        g = [Glyph(10, 15, 100, 7, "A"), Glyph(15, 20, 100.4, 7, "B"), Glyph(22, 27, 100, 7, "C"),
             Glyph(60, 65, 100, 7, "D"), Glyph(10, 15, 88, 7, "E")]
        out = lines(g)
        self.assertEqual([[c.text for c in ln.chunks] for ln in out], [["AB C", "D"], ["E"]])
