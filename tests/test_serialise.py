import unittest

from hbkit.xhb import serialise as S
from hbkit.xhb.model import Rule
from hbkit.xhb.reader import read_file

from .helpers import EXAMPLE

SERIALISERS = {
    "properties": S.serialise_properties,
    "cur": S.serialise_currency,
    "grp": S.serialise_group,
    "account": S.serialise_account,
    "pay": S.serialise_payee,
    "cat": S.serialise_category,
    "tag": S.serialise_tag,
    "asg": S.serialise_rule,
    "fav": S.serialise_template,
    "ope": S.serialise_transaction,
    "flt": S.serialise_filter,
}


class HomeBankExampleTests(unittest.TestCase):
    """The serialiser must reproduce lines HomeBank itself wrote, byte for byte."""

    def test_every_element_except_templates(self):
        book = read_file(EXAMPLE)
        objects = (
            [book.properties] + list(book.currencies.values()) + list(book.groups.values())
            + list(book.accounts.values()) + list(book.payees.values()) + list(book.categories.values())
            + list(book.tags.values()) + book.rules + book.transactions
        )
        checked = 0
        for obj in objects:
            original = book.raw[obj.src.start:obj.src.end].decode("utf-8")
            self.assertEqual(SERIALISERS[obj.src.name](obj), original)
            checked += 1
        self.assertEqual(checked, 1 + 4 + 3 + 8 + 15 + 23 + 5 + 2 + 755)

    def test_templates_differ_only_by_version(self):
        # HomeBank 5.1 always wrote limit="..." on templates; 5.9 and later write it only
        # when the "limit" recurrence flag is set (recflg did not exist in 5.1).
        book = read_file(EXAMPLE)
        for tpl in book.templates:
            original = book.raw[tpl.src.start:tpl.src.end].decode("utf-8")
            ours = S.serialise_template(tpl)
            self.assertEqual(ours, original.replace(f' limit="{tpl.limit}"', ""))


class EscapeTests(unittest.TestCase):
    def test_g_markup_escape_text(self):
        self.assertEqual(S.escape("A&B <x> \"q\" 'a'"), "A&amp;B &lt;x&gt; &quot;q&quot; &apos;a&apos;")
        self.assertEqual(S.escape("tab\tnew\nline\x01\x7f\x85"), "tab\tnew\nline&#x1;&#x7f;\x85")
        self.assertEqual(S.escape("é ü 漢"), "é ü 漢")

    def test_crlf_variant(self):
        self.assertEqual(S.escape("a\r\nb", crlf=True), "a&#xd;&#xa;b")

    def test_numbers_like_g_ascii_dtostr(self):
        self.assertEqual(S.dtostr(9.72), "9.7200000000000006")
        self.assertEqual(S.dtostr(-101.1), "-101.09999999999999")
        self.assertEqual(S.dtostr(-1053.45), "-1053.45")
        self.assertEqual(S.dtostr(0.0), "0")
        self.assertEqual(S.dtostr(50.0), "50")
        self.assertEqual(S.dtostr(4.2e-05), "4.1999999999999998e-05")

    def test_new_rule_line(self):
        rule = Rule(key=12, flags=4, pos=11, search="WOOLWORTHS & CO", category_key=7)
        self.assertEqual(S.serialise_rule(rule),
                         '<asg key="12" flags="4" pos="11" name="WOOLWORTHS &amp; CO" category="7" amount="0"/>')


if __name__ == "__main__":
    unittest.main()
