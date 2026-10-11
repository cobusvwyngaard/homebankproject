import unittest

from hbkit.xhb.reader import read_bytes, read_file
from hbkit.xhb.structure import structure_report

from .helpers import EXAMPLE, TempDirTestCase, run_cli, sample


class StructureReportTests(unittest.TestCase):
    def test_example_file(self):
        text = "\n".join(structure_report(read_file(EXAMPLE)))
        self.assertIn("File version (v)      1.3", text)
        self.assertIn("written by HomeBank 5.1.0", text)
        self.assertIn("as HomeBank saves it", text)
        self.assertNotIn("freebox", text)  # a rule text in the example file

    def test_unknown_names_are_marked(self):
        text = "\n".join(structure_report(read_bytes(sample(unknown_extras=True).data)))
        self.assertIn("futureattr *", text)
        self.assertIn("zzfuture", text)
        self.assertIn("not known to this toolkit", text)

    def test_windows_file(self):
        text = "\n".join(structure_report(read_bytes(sample(newline="\r\n").data)))
        self.assertIn("CRLF (Windows style)", text)

    def test_fill_rates_for_memo_and_payee(self):
        lines = structure_report(read_bytes(sample().data))
        wording = [line for line in lines if line.strip().startswith("wording")]
        self.assertTrue(wording)


class StructureCommandTests(TempDirTestCase):
    def test_command(self):
        path = self.write_sample()
        code, out, err = run_cli("structure", "--file", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("HomeBank file structure report", out)

    def test_missing_file(self):
        code, out, err = run_cli("structure", "--file", str(self.tmp / "missing.xhb"))
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
