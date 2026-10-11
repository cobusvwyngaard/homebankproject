import difflib
import os

from hbkit.errors import WriteRefusedError
from hbkit.xhb import serialise as S
from hbkit.xhb.model import Rule
from hbkit.xhb.reader import read_bytes, read_file
from hbkit.xhb.writer import XhbEditor

from .helpers import EXAMPLE, TempDirTestCase, sample

VARIANTS = [
    {},
    {"newline": "\r\n"},
    {"unknown_extras": True},
    {"control_chars": True},
    {"file_version": "1.4", "data_version": 50402},
]


def line_diff(a: bytes, b: bytes) -> tuple[list[str], list[str]]:
    """Lines removed from a and added in b (with their line endings)."""
    al, bl = a.decode("utf-8").splitlines(True), b.decode("utf-8").splitlines(True)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if op != "equal":
            removed += al[i1:i2]
            added += bl[j1:j2]
    return removed, added


class RoundTripTests(TempDirTestCase):
    """Reading and writing an unchanged file must reproduce it byte for byte."""

    def check_unchanged(self, data: bytes, name: str):
        src = self.tmp / f"{name}.xhb"
        src.write_bytes(data)
        editor = XhbEditor(read_file(src))
        self.assertFalse(editor.has_changes)
        self.assertEqual(editor.render(), data)
        out = self.tmp / f"{name}-copy.xhb"
        editor.save(out, input_path=src)
        self.assertEqual(out.read_bytes(), data)

    def test_homebank_example(self):
        self.check_unchanged(EXAMPLE.read_bytes(), "example")

    def test_generated_variants(self):
        for i, opts in enumerate(VARIANTS):
            with self.subTest(**opts):
                self.check_unchanged(sample(**opts).data, f"v{i}")


class AddRuleTests(TempDirTestCase):
    def add_and_save(self, data: bytes, *rules: Rule):
        self.runs = getattr(self, "runs", 0) + 1
        src = self.tmp / f"in{self.runs}.xhb"
        src.write_bytes(data)
        before = src.read_bytes()
        mtime = src.stat().st_mtime_ns
        book = read_file(src)
        editor = XhbEditor(book)
        for r in rules:
            editor.add_rule(r)
        out = self.tmp / f"out{self.runs}.xhb"
        result = editor.save(out, input_path=src)
        # the input is untouched
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(src.stat().st_mtime_ns, mtime)
        self.assertFalse(list(self.tmp.glob("*.tmp")))
        return book, out.read_bytes(), result

    def test_one_rule_changes_exactly_one_line(self):
        for opts in VARIANTS:
            with self.subTest(**opts):
                data = sample(**opts).data
                rule = Rule(flags=4, search="WOOLWORTHS", category_key=1)
                book, new, _ = self.add_and_save(data, rule)
                removed, added = line_diff(data, new)
                nl = "\r\n" if opts.get("newline") == "\r\n" else "\n"
                self.assertEqual(removed, [])
                self.assertEqual(added, [S.serialise_rule(rule) + nl])
                self.assertEqual((rule.key, rule.pos), (11, 11))
                # placed directly after the last existing rule
                lines = new.decode().splitlines()
                i = lines.index(S.serialise_rule(rule))
                self.assertTrue(lines[i - 1].startswith("<asg "))
                self.assertTrue(lines[i + 1].startswith("<fav "))

    def test_new_file_reads_back(self):
        data = sample().data
        _, new, result = self.add_and_save(
            data,
            Rule(flags=4, search="WOOLWORTHS", category_key=1),
            Rule(flags=4, search="ENGEN & SHELL <x>", category_key=6),
        )
        self.assertEqual(result.inserted, 2)
        old, again = read_bytes(data), read_bytes(new)
        self.assertEqual(len(again.rules), len(old.rules) + 2)
        added = again.rules[-2:]
        self.assertEqual([(r.key, r.pos, r.search, r.category_key) for r in added],
                         [(11, 11, "WOOLWORTHS", 1), (12, 12, "ENGEN & SHELL <x>", 6)])
        self.assertEqual([t.memo for t in again.transactions], [t.memo for t in old.transactions])

    def test_homebank_example(self):
        data = EXAMPLE.read_bytes()
        rule = Rule(flags=4, search="supermarket", category_key=129)
        _, new, _ = self.add_and_save(data, rule)
        removed, added = line_diff(data, new)
        self.assertEqual((removed, added), ([], [S.serialise_rule(rule) + "\n"]))

    def test_file_without_rules(self):
        data = (b'<?xml version="1.0"?>\n<homebank v="1.6" d="051003">\n<cat key="1" name="Food"/>\n'
                b'<tag key="1" name="x"/>\n<ope date="738000" amount="-1" account="1"/>\n</homebank>\n')
        _, new, _ = self.add_and_save(data, Rule(flags=4, search="shop", category_key=1))
        self.assertEqual(new.decode().splitlines()[4], '<asg key="1" flags="4" pos="1" name="shop" category="1" amount="0"/>')


class OtherEditTests(TempDirTestCase):
    def test_replace_and_remove(self):
        data = sample().data
        book = read_bytes(data)
        editor = XhbEditor(book)
        editor.replace(book.rules[1], {"category": "8"})
        editor.remove(book.rules[4])
        out = self.tmp / "out.xhb"
        editor.save(out)
        removed, added = line_diff(data, out.read_bytes())
        orig = data.decode().splitlines(True)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(added), 1)
        self.assertIn('category="8"', added[0])
        again = read_bytes(out.read_bytes())
        self.assertEqual(len(again.rules), 9)
        self.assertEqual(again.rules[1].category_key, 8)
        self.assertEqual(len(orig) - 1, len(out.read_bytes().decode().splitlines(True)))

    def test_replace_keeps_unknown_attributes_and_order(self):
        book = read_bytes(sample(unknown_extras=True).data)
        acc = book.accounts[1]
        editor = XhbEditor(book)
        editor.replace(acc, {"name": "Renamed"})
        text = editor.render().decode()
        line = next(l for l in text.splitlines() if l.startswith('<account key="1"'))
        self.assertIn('name="Renamed"', line)
        self.assertTrue(line.endswith('futureattr="1"/>'))


class RefusalTests(TempDirTestCase):
    def test_never_overwrites_input(self):
        src = self.write_sample()
        editor = XhbEditor(read_file(src))
        editor.add_rule(Rule(flags=4, search="x", category_key=1))
        with self.assertRaises(WriteRefusedError):
            editor.save(src, input_path=src)
        with self.assertRaises(WriteRefusedError):
            editor.save(self.tmp / "." / src.name, input_path=src)
        with self.assertRaises(WriteRefusedError):
            editor.save(src, input_path=src, overwrite=True)
        self.assertEqual(src.read_bytes(), sample().data)

    def test_existing_output_needs_overwrite(self):
        src = self.write_sample()
        out = self.tmp / "out.xhb"
        out.write_text("keep me")
        editor = XhbEditor(read_file(src))
        with self.assertRaises(WriteRefusedError):
            editor.save(out, input_path=src)
        self.assertEqual(out.read_text(), "keep me")
        editor.save(out, input_path=src, overwrite=True)
        self.assertEqual(out.read_bytes(), src.read_bytes())

    def test_invalid_references(self):
        editor = XhbEditor(read_bytes(sample().data))
        with self.assertRaises(WriteRefusedError):
            editor.add_rule(Rule(flags=4, search="x", category_key=9999))
        with self.assertRaises(WriteRefusedError):
            editor.add_rule(Rule(flags=4, search="", category_key=1))
        with self.assertRaises(WriteRefusedError):
            editor.add_rule(Rule(key=1, flags=4, search="dup key", category_key=1))

    def test_non_utf8_input(self):
        data = sample().data.replace(b"Groceries", b"Groc\xe9ries")
        with self.assertRaises(WriteRefusedError):
            XhbEditor(read_bytes(data))

    def test_verification_catches_a_bad_render(self):
        editor = XhbEditor(read_bytes(sample().data))
        editor.add_rule(Rule(flags=4, search="x", category_key=1))
        editor.render = lambda: sample().data  # simulate a bug: the edit went missing
        with self.assertRaises(WriteRefusedError):
            editor.save(self.tmp / "out.xhb")
        self.assertFalse((self.tmp / "out.xhb").exists())

    def test_symlinked_output_to_input(self):
        src = self.write_sample()
        link = self.tmp / "link.xhb"
        try:
            os.symlink(src, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        editor = XhbEditor(read_file(src))
        with self.assertRaises(WriteRefusedError):
            editor.save(link, input_path=src, overwrite=True)
