"""The rule-mining analysis and the apply-rules command, end to end."""

import csv
import difflib
import json

from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, run_cli, sample

EXPECTED_FILES = {
    "README.txt", "review.csv", "rejected_candidates.csv", "backtest.csv", "backtest_by_category.csv",
    "existing_rules.csv", "overlaps.csv", "inconsistent_patterns.csv", "summary.json",
}


class RuleMiningCommandTests(TempDirTestCase):
    def mine(self, *extra, **sample_opts):
        path = self.write_sample(**sample_opts)
        out = self.tmp / "out"
        code, stdout, stderr = run_cli("run", "rule-mining", "--file", str(path), "--out", str(out), *extra)
        self.assertEqual(code, 0, stderr)
        folder = next((out / "rule-mining").iterdir())
        return path, folder, stdout, stderr

    def read_csv(self, path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_outputs(self):
        _, folder, stdout, _ = self.mine()
        self.assertEqual({p.name for p in folder.iterdir()}, EXPECTED_FILES)
        review = self.read_csv(folder / "review.csv")
        self.assertEqual(next(iter(review[0])), "approve")
        self.assertTrue(all(r["approve"] == "" for r in review))
        self.assertTrue(all(r["rule_id"].startswith("N") for r in review))
        summary = json.loads((folder / "summary.json").read_text())
        self.assertEqual(summary["suggested"], len(review))
        self.assertEqual(summary["backtest"]["test_years"], [2024, 2025])
        self.assertIn("Back-test", stdout)
        self.assertIn("existing + new rules: correct", stdout)
        self.assertNotIn("Groceries", stdout)  # no names on screen by default

    def test_show_categories_is_explicit(self):
        _, _, stdout, _ = self.mine("--show-categories")
        self.assertIn("Groceries", stdout)

    def test_min_share_as_percentage(self):
        _, folder, _, _ = self.mine("--min-share", "95", "--no-merge")
        self.assertEqual(json.loads((folder / "summary.json").read_text())["parameters"]["min_share"], 0.95)
        kinds = {r["kind"] for r in self.read_csv(folder / "review.csv")}
        self.assertEqual(kinds, {"pattern"})

    def test_bad_parameter(self):
        path = self.write_sample()
        code, _, err = run_cli("run", "rule-mining", "--file", str(path), "--out", str(self.tmp), "--min-share", "0")
        self.assertEqual(code, 2)
        self.assertIn("--min-share", err)

    def test_sentinel_file(self):
        _, _, stdout, stderr = self.mine("--show-categories", sentinel=True)
        # category names were explicitly requested, but no memo or rule text may appear
        for line in (stdout + stderr).splitlines():
            if "ZQXSENT" in line:
                self.assertIn("ZQXSENTCAT_", line)


class ApplyRulesTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_sample("finances.xhb")
        self.out = self.tmp / "out"
        code, _, err = run_cli("run", "rule-mining", "--file", str(self.src), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.folder = next((self.out / "rule-mining").iterdir())
        with open(self.folder / "review.csv", encoding="utf-8-sig", newline="") as fh:
            self.rows = list(csv.DictReader(fh))

    def write_review(self, rows, name="review.csv", encoding="utf-8-sig", delimiter=","):
        path = self.folder / name
        with open(path, "w", encoding=encoding, newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=delimiter)
            w.writeheader()
            w.writerows(rows)
        return path

    def apply(self, review, *extra):
        return run_cli("apply-rules", "--review", str(review), "--file", str(self.src), "--out", str(self.out), *extra)

    def added_lines(self, output):
        a = self.src.read_text(encoding="utf-8").splitlines()
        b = output.read_text(encoding="utf-8").splitlines()
        diff = [d for d in difflib.ndiff(a, b) if d[:1] in "+-"]
        self.assertTrue(all(d.startswith("+ ") for d in diff), "lines were removed or changed")
        return [d[2:] for d in diff]

    def test_apply_approved_rules(self):
        before = self.src.read_bytes()
        rows = [dict(r) for r in self.rows]
        rows[0]["approve"] = "y"
        rows[1]["approve"] = "Yes"
        rows[2]["approve"] = "ja"
        self.write_review(rows)
        code, stdout, err = self.apply(self.folder)  # the folder works as well as the file
        self.assertEqual(code, 0, err)
        self.assertEqual(self.src.read_bytes(), before)  # the original is untouched
        output = next(self.out.glob("with-rules_*.xhb"))
        added = self.added_lines(output)
        self.assertEqual(len(added), 3)
        self.assertTrue(all(line.startswith('<asg key="') and 'flags="4"' in line for line in added))
        book = read_bytes(output.read_bytes())
        texts = {r.search: book.category_path(r.category_key) for r in book.rules}
        for r in rows[:3]:
            self.assertEqual(texts[r["rule_text"]], r["category"])
        # specific texts are placed before general ones
        new = sorted(book.rules, key=lambda r: r.pos)[-3:]
        self.assertEqual([len(r.search) for r in new], sorted((len(r.search) for r in new), reverse=True))
        self.assertIn("Rules added", stdout)

    def test_excel_saved_review(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["approve"] = "x"
        rows[1]["approve"] = "X"
        review = self.write_review(rows, "excel.csv", encoding="cp1252", delimiter=";")
        code, _, err = self.apply(review)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.added_lines(next(self.out.glob("with-rules_*.xhb")))), 2)

    def test_edits_and_skips(self):
        rows = [dict(r) for r in self.rows]
        rows[0].update(approve="y", rule_text="MY EDITED TEXT")
        rows[1].update(approve="y", category="No such category")
        rows[2].update(approve="y", rule_text="VODACOM", category="Communication:Cellphone")  # exists already
        rows[3].update(approve="y", rule_text=rows[4]["rule_text"], category=rows[4]["category"])
        rows[4].update(approve="y")  # same as row 3
        review = self.write_review(rows)
        code, stdout, err = self.apply(review)
        self.assertEqual(code, 0, err)
        self.assertIn("Skipped: category not found in the file", stdout)
        self.assertIn("Skipped: already a rule in the file", stdout)
        self.assertIn("Skipped: approved twice", stdout)
        added = self.added_lines(next(self.out.glob("with-rules_*.xhb")))
        self.assertEqual(len(added), 2)
        self.assertTrue(any('name="MY EDITED TEXT"' in line for line in added))

    def test_nothing_approved(self):
        code, stdout, _ = self.apply(self.folder)
        self.assertEqual(code, 0)
        self.assertIn("no file was written", stdout)
        self.assertFalse(list(self.out.glob("with-rules_*.xhb")))

    def test_never_overwrites_the_input(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["approve"] = "y"
        self.write_review(rows)
        before = self.src.read_bytes()
        code, _, err = self.apply(self.folder, "--output", str(self.src))
        self.assertEqual(code, 2)
        self.assertIn("must not be the input", err)
        self.assertEqual(self.src.read_bytes(), before)

    def test_changed_file_is_noted(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["approve"] = "y"
        self.write_review(rows)
        self.src.write_bytes(sample(seed=2).data)
        code, stdout, err = self.apply(self.folder)
        self.assertEqual(code, 0, err)
        self.assertIn("has changed since this review was made", stdout)

    def test_missing_columns(self):
        path = self.folder / "bad.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        code, _, err = self.apply(path)
        self.assertEqual(code, 2)
        self.assertIn("no approve, rule_text, category column", err)

    def test_privacy(self):
        src = self.write_sample("s.xhb", sentinel=True)
        out = self.tmp / "out2"
        code, stdout, err = run_cli("run", "rule-mining", "--file", str(src), "--out", str(out))
        self.assertEqual(code, 0, err)
        folder = next((out / "rule-mining").iterdir())
        with open(folder / "review.csv", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows[:3]:
            r["approve"] = "y"
        self.write_review(rows, "s.csv")
        code, stdout2, err2 = run_cli("apply-rules", "--review", str(self.folder / "s.csv"), "--file", str(src),
                                      "--out", str(out))
        self.assertEqual(code, 0, err2)
        self.assertNoSentinel(stdout, err, stdout2, err2)
