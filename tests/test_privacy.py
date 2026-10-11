"""Privacy guarantees: no text from the HomeBank file may reach the terminal."""

import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from hbkit.privacy import REDACTED, Summary, Terminal, new_run_dir

from .helpers import TempDirTestCase, run_cli

PROJECT = Path(__file__).resolve().parent.parent


class SummaryTests(unittest.TestCase):
    def test_only_numbers_accepted(self):
        s = Summary("t")
        with self.assertRaises(TypeError):
            s.count("x", "a memo")
        with self.assertRaises(TypeError):
            s.number("x", "12")
        with self.assertRaises(TypeError):
            s.percent("x", "1", 2)
        with self.assertRaises(TypeError):
            s.count("x", True)

    def test_render(self):
        s = Summary("Title")
        s.count("Transactions", 12345)
        s.percent("Correct", 3, 4)
        lines = s.render()
        self.assertIn("12 345", "\n".join(lines))
        self.assertIn("75.0%", "\n".join(lines))


class TerminalTests(unittest.TestCase):
    def test_lines_with_file_text_are_hidden(self):
        buf = io.StringIO()
        term = Terminal({"WOOLWORTHS SANDTON"}, stream=buf)
        term.line("Rules: 5")
        term.line("Rule WOOLWORTHS SANDTON is good")
        self.assertEqual(buf.getvalue().splitlines(), ["Rules: 5", REDACTED])
        self.assertEqual(term.hidden, 1)


class CommandSentinelTests(TempDirTestCase):
    """Run commands on a file whose free text is all marked, and check the terminal."""

    def test_structure(self):
        path = self.write_sample(sentinel=True, unknown_extras=True)
        code, out, err = run_cli("structure", "--file", str(path))
        self.assertEqual(code, 0)
        self.assertIn("<ope>", out)
        self.assertNoSentinel(out, err)

    def test_format_error_message(self):
        path = self.tmp / "broken.xhb"
        path.write_bytes(b'<?xml version="1.0"?>\n<homebank v="1.6">\n<ope wording="ZQXSENT secret/>\n')
        code, out, err = run_cli("structure", "--file", str(path))
        self.assertEqual(code, 2)
        self.assertIn("line 3", err)
        self.assertNoSentinel(out, err)

    def test_unexpected_errors_go_to_a_log_file(self):
        path = self.write_sample(sentinel=True)
        from hbkit.xhb import structure

        original = structure.structure_report

        def boom(book):
            raise RuntimeError(book.transactions[0].memo)

        structure.structure_report = boom
        try:
            # --out is not an option of "structure", so the log goes to the default out folder;
            # point it at the temporary folder through the module constant instead.
            import hbkit.cli as cli

            saved = cli.OUT_DIR
            cli.OUT_DIR = self.tmp / "out"
            try:
                code, out, err = run_cli("structure", "--file", str(path))
            finally:
                cli.OUT_DIR = saved
        finally:
            structure.structure_report = original
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err)
        self.assertNoSentinel(out, err)
        logs = list((self.tmp / "out" / "errors").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("ZQXSENT", logs[0].read_text())

    def test_run_dirs_are_unique(self):
        a = new_run_dir(self.tmp, "x")
        b = new_run_dir(self.tmp, "x")
        self.assertNotEqual(a, b)


class RepositoryGuardTests(TempDirTestCase):
    """The git and Claude Code guards around the private data folder stay in place."""

    def test_deny_rules(self):
        settings = json.loads((PROJECT / ".claude" / "settings.json").read_text())
        deny = settings["permissions"]["deny"]
        for rule in ("Read(/data)", "Read(/data/**)", "Edit(/data/**)", "Bash(*data/*)"):
            self.assertIn(rule, deny)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignore_keeps_private_files_out(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        shutil.copy(PROJECT / ".gitignore", repo / ".gitignore")
        files = {
            "data/.gitkeep": False,
            "data/out/.gitkeep": False,
            "data/real.xhb": True,
            "data/notes.txt": True,
            "data/out/review.csv": True,
            "data/out/rule-mining/2026/review.csv": True,
            "copy-of-real.xhb": True,
            "finances.sqlite": True,
            "tests/fixtures/homebank_example/example.xhb": False,
            "hbkit/cli.py": False,
        }
        for rel in files:
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        for rel, ignored in files.items():
            result = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", rel])
            self.assertEqual(result.returncode == 0, ignored, rel)


if __name__ == "__main__":
    unittest.main()
