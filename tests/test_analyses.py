"""The analysis framework, and privacy checks that apply to every analysis automatically."""

import csv
import unittest

from hbkit.analyses import discover
from hbkit.analyses.base import Analysis, Param
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, run_cli, sample


class DiscoveryTests(unittest.TestCase):
    def test_analyses_are_found_and_well_formed(self):
        found = discover()
        self.assertIn("overview", found)
        for name, analysis in found.items():
            self.assertIsInstance(analysis, Analysis)
            self.assertTrue(analysis.description)
            for p in analysis.params:
                self.assertIsInstance(p, Param)
                self.assertIn(p.type, (int, float, str, bool))
                self.assertTrue(p.name.isidentifier())

    def test_list_command(self):
        code, out, _ = run_cli("list")
        self.assertEqual(code, 0)
        for name in discover():
            self.assertIn(name, out)


class EveryAnalysisTests(TempDirTestCase):
    """Run every analysis with its defaults on a sentinel-marked file."""

    def test_no_file_text_on_screen(self):
        path = self.write_sample(sentinel=True)
        for name in discover():
            with self.subTest(analysis=name):
                out_dir = self.tmp / f"out-{name}"
                code, out, err = run_cli("run", name, "--file", str(path), "--out", str(out_dir))
                self.assertEqual(code, 0, err)
                self.assertNoSentinel(out, err)
                self.assertTrue(any(out_dir.rglob("*")), "the analysis wrote no detailed output")


class OverviewTests(TempDirTestCase):
    def test_totals_match_transactions(self):
        path = self.write_sample()
        code, out, err = run_cli("run", "overview", "--file", str(path), "--out", str(self.tmp))
        self.assertEqual(code, 0, err)
        csv_path = next(self.tmp.rglob("monthly_category_totals.csv"))
        with open(csv_path, encoding="utf-8-sig") as fh:
            total = sum(float(r["total"]) for r in csv.DictReader(fh))
        book = read_bytes(sample().data)
        expected = sum(t.amount for t in book.transactions if not t.is_void and not t.is_internal_transfer)
        self.assertAlmostEqual(total, expected, places=1)
        self.assertIn("Categorised", out)

    def test_boolean_parameter(self):
        path = self.write_sample()
        code, out_with, _ = run_cli("run", "overview", "--file", str(path), "--out", str(self.tmp),
                                    "--include-transfers")
        code2, out_without, _ = run_cli("run", "overview", "--file", str(path), "--out", str(self.tmp))
        self.assertEqual((code, code2), (0, 0))
        self.assertNotEqual(out_with, out_without)


if __name__ == "__main__":
    unittest.main()
