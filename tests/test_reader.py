import unittest
from datetime import date

from hbkit.errors import HbkitError, XhbFormatError
from hbkit.xhb import format as F
from hbkit.xhb.model import date_to_julian, julian_to_date
from hbkit.xhb.reader import parse_version, read_bytes, read_file

from .helpers import EXAMPLE, TempDirTestCase, sample


def doc(*children: str, v: str = "1.6", d: str = "051003") -> bytes:
    lines = ['<?xml version="1.0"?>', f'<homebank v="{v}" d="{d}">', *children, "</homebank>"]
    return ("\n".join(lines) + "\n").encode("utf-8")


class DateTests(unittest.TestCase):
    def test_glib_julian_days(self):
        # HomeBank's own limits from homebank.h
        self.assertEqual(julian_to_date(F.HB_MINDATE), date(1900, 1, 1))
        self.assertEqual(julian_to_date(F.HB_MAXDATE), date(2200, 12, 31))
        self.assertEqual(julian_to_date(1), date(1, 1, 1))
        self.assertEqual(date_to_julian(date(2024, 2, 29)), 738945)
        self.assertIsNone(julian_to_date(0))


class HomeBankExampleTests(unittest.TestCase):
    """HomeBank's bundled example file (written by HomeBank 5.1)."""

    @classmethod
    def setUpClass(cls):
        cls.book = read_file(EXAMPLE)

    def test_versions(self):
        self.assertEqual(self.book.version, "1.3")
        self.assertEqual(self.book.data_version, 50100)
        self.assertEqual(self.book.file_version, 1.3)

    def test_counts(self):
        b = self.book
        self.assertEqual(
            (len(b.accounts), len(b.payees), len(b.categories), len(b.tags), len(b.rules),
             len(b.templates), len(b.transactions), len(b.currencies), len(b.groups)),
            (8, 15, 23, 5, 2, 18, 755, 4, 3),
        )
        self.assertEqual(dict(b.warnings), {})

    def test_category_paths(self):
        self.assertEqual(self.book.category_path(129), "Alimony:Food")
        self.assertEqual(self.book.category_path(1), "Alimony")
        self.assertIs(self.book.category_by_path(" alimony:FOOD "), self.book.categories[129])

    def test_transfers_and_splits(self):
        b = self.book
        xfers = [t for t in b.transactions if t.is_internal_transfer]
        self.assertTrue(xfers)
        self.assertTrue(all(t.dst_account_key for t in xfers))
        split = next(t for t in b.transactions if t.is_split)
        self.assertEqual([s.category_key for s in split.splits], [129, 130])
        self.assertAlmostEqual(sum(s.amount for s in split.splits), split.amount)

    def test_rules(self):
        r = self.book.rules[0]
        self.assertEqual((r.key, r.pos, r.search, r.category_key, r.payee_key), (1, 1, "freebox", 21, 9))
        self.assertTrue(r.sets_category and r.sets_payee)

    def test_null_text(self):
        # HomeBank keeps the literal "(null)" for currency attributes.
        self.assertEqual(self.book.currencies[4].grouping_char, "(null)")


class GeneratedFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = sample()
        cls.book = read_bytes(cls.gen.data)

    def test_counts(self):
        b = self.book
        self.assertEqual(len(b.transactions), self.gen.transactions)
        self.assertEqual(len(b.accounts), 5)
        self.assertEqual(len(b.rules), 10)
        self.assertEqual(dict(b.warnings), {})

    def test_dates_within_range(self):
        dates = [t.date for t in self.book.transactions]
        self.assertEqual(min(dates).year, 2020)
        self.assertEqual((max(dates).year, max(dates).month), (2025, 9))

    def test_splits_add_up(self):
        for t in self.book.transactions:
            if t.is_split:
                self.assertAlmostEqual(sum(s.amount for s in t.splits), t.amount, places=2)
                self.assertEqual(t.category_key, 0)

    def test_transfers_are_paired(self):
        pairs = {}
        for t in self.book.transactions:
            if t.is_internal_transfer:
                pairs.setdefault(t.kxfer, []).append(t)
        self.assertTrue(pairs)
        for legs in pairs.values():
            self.assertEqual(len(legs), 2)
            a, b = legs
            self.assertAlmostEqual(a.amount, -b.amount)
            self.assertEqual((a.account_key, a.dst_account_key), (b.dst_account_key, b.account_key))

    def test_closed_account_and_notes(self):
        acc = self.book.accounts[5]
        self.assertTrue(acc.is_closed)
        self.assertIn("\n", acc.notes)  # written as &#xa; by the CR/LF-escaping variant

    def test_rule_flags(self):
        rules = {r.key: r for r in self.book.rules}
        self.assertTrue(rules[6].is_regex)
        self.assertEqual(rules[8].match_field, F.RULE_FIELD_PAYEE)
        self.assertTrue(rules[9].flags & F.ASGF_OVWCAT)
        self.assertEqual([r.key for r in self.book.rules_in_evaluation_order()], list(range(1, 11)))


class ToleranceTests(unittest.TestCase):
    def test_unknown_attributes_and_elements_are_kept(self):
        book = read_bytes(sample(unknown_extras=True).data)
        self.assertEqual(book.accounts[1].extra, {"futureattr": "1"})
        self.assertTrue(any(t.extra == {"futureflag": "1"} for t in book.transactions))
        self.assertEqual([e.name for e in book.unknown_elements], ["zzfuture"])
        self.assertGreater(book.warnings["unknown elements kept"], 0)

    def test_windows_line_endings_read_the_same(self):
        lf = read_bytes(sample().data)
        crlf = read_bytes(sample(newline="\r\n").data)
        self.assertEqual(crlf.scan.newline, "\r\n")
        self.assertEqual([t.memo for t in lf.transactions], [t.memo for t in crlf.transactions])

    def test_control_characters(self):
        book = read_bytes(sample(control_chars=True).data)
        self.assertTrue(any("\x01" in t.memo for t in book.transactions))

    def test_pre_5_9_status_is_normalised(self):
        book = read_bytes(doc('<ope date="738000" amount="-5" st="4"/>', '<ope date="738000" amount="-5" st="3"/>',
                              v="1.4", d="050402"))
        void, remind = book.transactions
        self.assertTrue(void.is_void)
        self.assertFalse(remind.is_void)
        self.assertTrue(remind.is_remind)
        old = read_bytes(sample(file_version="1.4", data_version=50402).data)
        new = read_bytes(sample().data)
        self.assertEqual(sum(t.is_void for t in old.transactions), sum(t.is_void for t in new.transactions))

    def test_very_old_rule_exact_attribute(self):
        book = read_bytes(doc('<asg key="1" name="x" exact="1" category="2"/>', v="0.7", d=""))
        self.assertEqual(book.rules[0].flags, F.ASGF_DOCAT | F.ASGF_DOPAY | F.ASGF_EXACT)

    def test_lenient_numbers_without_leaking_values(self):
        book = read_bytes(doc('<ope date="738000" amount="SECRETVALUE" account="2x"/>'))
        t = book.transactions[0]
        self.assertEqual((t.amount, t.account_key), (0.0, 2))
        text = " ".join(book.warnings)
        self.assertIn("<ope amount>", text)
        self.assertNotIn("SECRETVALUE", text)

    def test_null_text_where_homebank_ignores_it(self):
        book = read_bytes(doc('<ope date="738000" amount="-1" wording="(null)" info="(null)"/>'))
        self.assertEqual(book.transactions[0].memo, "")

    def test_bad_splits_are_dropped_with_warning(self):
        book = read_bytes(doc('<ope date="738000" amount="-3" scat="1||2" samt="-1" smem="||"/>'))
        self.assertEqual(book.transactions[0].splits, [])
        self.assertEqual(sum(book.warnings.values()), 1)

    def test_version_parsing(self):
        self.assertEqual(parse_version("1.6"), 1.6)
        self.assertEqual(parse_version(""), 0.1)
        self.assertEqual(parse_version("5.0"), 1.0)

    def test_newer_version_is_flagged(self):
        book = read_bytes(doc(v="9.9", d="099999"))
        self.assertIn("file structure version is newer than this toolkit knows", book.warnings)

    def test_wrong_root(self):
        with self.assertRaises(XhbFormatError):
            read_bytes(b'<?xml version="1.0"?>\n<gnucash/>\n')


class FileErrorTests(TempDirTestCase):
    def test_missing_file(self):
        with self.assertRaises(HbkitError) as cm:
            read_file(self.tmp / "nope.xhb")
        self.assertNotIn("nope", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
