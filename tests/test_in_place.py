"""Changing the HomeBank file itself (the owner's option) instead of writing a new copy (synthetic files only)."""

from unittest import mock

from hbkit.errors import WriteRefusedError
from hbkit.rules.fill import fill_categories, plan_fill
from hbkit.xhb import writer as W
from hbkit.xhb.reader import read_bytes
from hbkit.xhb.writer import XhbEditor, homebank_running

from .helpers import TempDirTestCase, run_cli


class WriterTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_sample("finances.xhb")
        self.original = self.src.read_bytes()
        self.book = read_bytes(self.original)
        self.editor = XhbEditor(self.book)
        self.editor.replace(self.book.transactions[0], {"info": "CHANGED"})
        self.backups = self.tmp / "out" / "backups"

    def test_changes_the_file_and_keeps_a_backup(self):
        res = self.editor.save_in_place(self.src, self.backups)
        self.assertTrue(res.in_place)
        self.assertEqual(res.path, self.src)
        self.assertEqual(self.src.read_bytes(), self.editor.render())
        self.assertEqual(res.backup.read_bytes(), self.original)
        self.assertTrue(res.backup.name.startswith("finances_before_") and res.backup.suffix == ".xhb")
        self.assertEqual(read_bytes(self.src.read_bytes()).transactions[0].info, "CHANGED")
        self.assertEqual(list(self.src.parent.glob("*.hbkit-tmp")), [])

    def test_refused_when_the_file_changed_on_disk(self):
        self.src.write_bytes(self.original.replace(b"</homebank>", b"\n</homebank>"))  # saved elsewhere meanwhile
        changed = self.src.read_bytes()
        with self.assertRaises(WriteRefusedError) as ctx:
            self.editor.save_in_place(self.src, self.backups)
        self.assertIn("has changed", str(ctx.exception))
        self.assertEqual(self.src.read_bytes(), changed)
        self.assertFalse(self.backups.exists())

    def test_refused_while_homebank_runs(self):
        with mock.patch.object(W, "homebank_running", return_value=True):
            with self.assertRaises(WriteRefusedError) as ctx:
                self.editor.save_in_place(self.src, self.backups)
        self.assertIn("HomeBank is running", str(ctx.exception))
        self.assertEqual(self.src.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())

    def test_save_to(self):
        with self.assertRaises(WriteRefusedError):
            self.editor.save_to(None, self.src)  # no backup folder
        copy = self.editor.save_to(self.tmp / "copy.xhb", self.src, self.backups)
        self.assertFalse(copy.in_place)
        self.assertEqual(self.src.read_bytes(), self.original)
        self.assertIn(homebank_running(), (True, False, None))


class StepTests(TempDirTestCase):
    def test_two_steps_on_the_same_file(self):
        src = self.write_sample("finances.xhb", uncategorised=0.3)
        backups = self.tmp / "out" / "backups"
        book = read_bytes(src.read_bytes())
        summary, path = fill_categories(book, ["E1"], src, None, plan_fill(book).source_sha256, backups)
        self.assertEqual(path, src)
        rows = {r["label"]: r for r in summary.to_dict()["rows"]}
        self.assertIn("Changed file", rows)
        self.assertIn("Backup of the file as it was", rows)
        first = src.read_bytes()
        self.assertNotEqual(first, book.raw)

        book2 = read_bytes(first)  # the next step works on the result
        _, path = fill_categories(book2, ["E4"], src, None, plan_fill(book2).source_sha256, backups)
        self.assertEqual(path, src)
        self.assertEqual(sorted(p.read_bytes() for p in backups.iterdir()), sorted([book.raw, first]))
        with self.assertRaises(Exception):  # the first book is out of date now
            fill_categories(book, ["E4"], src, None, None, backups)


class CommandTests(TempDirTestCase):
    def test_fill_categories_in_place(self):
        import csv

        src = self.write_sample("finances.xhb", uncategorised=0.3)
        out = self.tmp / "out"
        self.assertEqual(run_cli("fill-categories", "--file", str(src), "--out", str(out))[0], 0)
        folder = next((out / "fill-categories").iterdir())
        with open(folder / "rules.csv", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            r["approve"] = "y" if r["rule_id"] == "E1" else ""
        with open(folder / "rules.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        before = src.read_bytes()
        code, _, err = run_cli("fill-categories", "--file", str(src), "--out", str(out), "--review", str(folder),
                               "--in-place", "--output", str(self.tmp / "x.xhb"))
        self.assertEqual(code, 2)
        self.assertIn("not both", err)
        code, stdout, err = run_cli("fill-categories", "--file", str(src), "--out", str(out), "--review", str(folder),
                                    "--in-place")
        self.assertEqual(code, 0, err)
        self.assertIn("Changed file", stdout)
        self.assertNotEqual(src.read_bytes(), before)
        self.assertEqual([p.read_bytes() for p in (out / "backups").iterdir()], [before])
        self.assertEqual(list(out.glob("with-categories_*.xhb")), [])
