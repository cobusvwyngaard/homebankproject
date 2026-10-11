import unittest
import xml.etree.ElementTree as ET

from hbkit.errors import XhbFormatError
from hbkit.xhb.scanner import scan, unescape

from .helpers import EXAMPLE, sample

SECRET = "MY SECRET MEMO"


def doc(*children: str, newline: str = "\n") -> bytes:
    lines = ['<?xml version="1.0"?>', '<homebank v="1.6" d="051003">', *children, "</homebank>"]
    return (newline.join(lines) + newline).encode("utf-8")


class SpanTests(unittest.TestCase):
    def test_spans_cover_exact_bytes(self):
        data = doc('<ope date="738000" amount="-1" wording="A &amp; B"/>', '<tag key="1" name="x"/>')
        sr = scan(data)
        for el in sr.elements[1:]:
            text = data[el.start:el.end].decode()
            self.assertTrue(text.startswith(f"<{el.name}") and text.endswith("/>"))
        self.assertEqual(sr.elements[1].attrs["wording"], "A & B")
        self.assertEqual(sr.elements[1].line, 3)
        self.assertEqual(data[sr.root_close[0]:sr.root_close[1]], b"</homebank>")

    def test_attribute_order_is_kept(self):
        sr = scan(doc('<ope b="1" a="2" c="3"/>'))
        self.assertEqual(list(sr.elements[1].attrs), ["b", "a", "c"])


class GMarkupBehaviourTests(unittest.TestCase):
    def test_control_character_references_are_accepted(self):
        # HomeBank writes &#x1; for control characters; strict XML parsers reject it.
        data = doc('<ope wording="A&#x1;B"/>')
        with self.assertRaises(ET.ParseError):
            ET.fromstring(data)
        self.assertEqual(scan(data).elements[1].attrs["wording"], "A\x01B")

    def test_whitespace_in_values_is_not_normalised(self):
        data = doc('<account notes="line1\nline2\tend"/>')
        self.assertEqual(scan(data).elements[1].attrs["notes"], "line1\nline2\tend")

    def test_entities(self):
        self.assertEqual(unescape("&lt;&gt;&amp;&quot;&apos;&#65;&#x42;"), "<>&\"'AB")

    def test_unknown_entity_is_kept_and_counted(self):
        sr = scan(doc('<ope wording="fish &chips"/>'))
        self.assertEqual(sr.elements[1].attrs["wording"], "fish &chips")
        self.assertEqual(sum(sr.issues.values()), 1)


class FormatDetectionTests(unittest.TestCase):
    def test_newlines(self):
        self.assertEqual(scan(doc("<tag/>")).newline, "\n")
        self.assertEqual(scan(doc("<tag/>", newline="\r\n")).newline, "\r\n")

    def test_bom_and_encoding(self):
        sr = scan(b"\xef\xbb\xbf" + doc("<tag/>"))
        self.assertTrue(sr.has_bom)
        self.assertTrue(sr.utf8_valid)
        sr = scan(doc('<pay name="Caf\xe9"/>').replace("Café".encode(), b"Caf\xe9"))
        self.assertFalse(sr.utf8_valid)

    def test_comments_and_nesting_are_reported(self):
        sr = scan(doc("<!-- note -->", '<ope><x a="1"/></ope>'))
        self.assertEqual(sr.issues["comments"], 1)
        self.assertEqual(sr.issues["nested elements below the top level"], 1)


class ErrorTests(unittest.TestCase):
    def assertSafe(self, data: bytes):
        with self.assertRaises(XhbFormatError) as cm:
            scan(data)
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertIn("line", str(cm.exception))
        return cm.exception

    def test_unterminated_value(self):
        err = self.assertSafe(doc(f'<ope wording="{SECRET}/>'))
        self.assertIn("unterminated attribute value in <ope>", str(err))

    def test_unquoted_value(self):
        self.assertSafe(doc(f"<ope wording={SECRET}/>"))

    def test_unclosed_root(self):
        self.assertSafe(f'<homebank v="1.6">\n<ope wording="{SECRET}"/>\n'.encode())

    def test_unknown_names_are_not_echoed(self):
        err = self.assertSafe(doc(f'<{SECRET.replace(" ", "")} x/>'))
        self.assertNotIn(SECRET.replace(" ", ""), str(err))

    def test_not_xml(self):
        with self.assertRaises(XhbFormatError):
            scan(b"Date,Amount,Memo\n2024-01-01,-10,shop\n")


class CrossCheckTests(unittest.TestCase):
    """On well-formed files the scanner must agree with xml.etree."""

    def check(self, data: bytes):
        ours = [(e.name, e.attrs) for e in scan(data).elements]
        theirs = [(e.tag, dict(e.attrib)) for e in ET.fromstring(data).iter()]
        self.assertEqual(ours, theirs)

    def test_homebank_example(self):
        self.check(EXAMPLE.read_bytes())

    def test_generated_file(self):
        self.check(sample().data)


if __name__ == "__main__":
    unittest.main()
