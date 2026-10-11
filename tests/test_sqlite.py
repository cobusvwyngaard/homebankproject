import re
import sqlite3
from pathlib import Path

from hbkit.export.sqlite import SCHEMA_VERSION, TABLES, VIEWS, build_database, export_sqlite
from hbkit.xhb.reader import read_bytes, read_file

from .helpers import EXAMPLE, TempDirTestCase, run_cli, sample

DOC = Path(__file__).resolve().parent.parent / "docs" / "sqlite-schema.md"


def memory_db(data: bytes) -> tuple[sqlite3.Connection, dict]:
    conn = sqlite3.connect(":memory:")
    counts = build_database(read_bytes(data), conn)
    return conn, counts


class ExportTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = sample().data
        cls.book = read_bytes(cls.data)
        cls.conn, cls.counts = memory_db(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def test_row_counts_match_the_file(self):
        b = self.book
        self.assertEqual(self.counts["transactions"], len(b.transactions))
        self.assertEqual(self.counts["splits"], sum(len(t.splits) for t in b.transactions))
        self.assertEqual(self.counts["rules"], len(b.rules))
        self.assertEqual(self.counts["categories"], len(b.categories))
        self.assertEqual(self.q("PRAGMA user_version")[0][0], SCHEMA_VERSION)

    def test_amount_totals_match(self):
        total = round(sum(t.amount for t in self.book.transactions), 2)
        self.assertAlmostEqual(self.q("SELECT SUM(amount) FROM transactions")[0][0], total, places=2)
        lines = self.q("SELECT SUM(amount) FROM v_transaction_lines")[0][0]
        self.assertAlmostEqual(lines, total, places=2)

    def test_split_lines_expand(self):
        n_plain = self.q("SELECT COUNT(*) FROM transactions WHERE is_split = 0")[0][0]
        n_split_lines = self.q("SELECT COUNT(*) FROM splits")[0][0]
        self.assertEqual(self.q("SELECT COUNT(*) FROM v_transaction_lines")[0][0], n_plain + n_split_lines)
        rows = self.q("SELECT DISTINCT category FROM v_transaction_lines WHERE is_split_line = 1")
        self.assertEqual({r[0] for r in rows}, {"Groceries", "Household:Cleaning"})

    def test_transfers_net_to_zero(self):
        self.assertAlmostEqual(self.q("SELECT SUM(amount) FROM v_transactions WHERE type='transfer'")[0][0], 0.0)

    def test_category_paths_and_totals(self):
        self.assertEqual(self.q("SELECT full_name FROM categories WHERE key = 6")[0][0], "Transport:Fuel")
        rows = self.q("SELECT top_level, sub_name FROM v_categories WHERE key = 6")
        self.assertEqual(rows, [("Transport", "Fuel")])
        monthly = self.q("SELECT SUM(total) FROM v_monthly_category_totals")[0][0]
        expected = self.q("SELECT SUM(amount) FROM v_transaction_lines WHERE is_void=0 AND type<>'transfer'")[0][0]
        self.assertAlmostEqual(monthly, expected, places=1)

    def test_dates_are_iso(self):
        dates = [r[0] for r in self.q("SELECT date FROM transactions")]
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates))

    def test_rules_view_in_evaluation_order(self):
        rows = self.q("SELECT key, category FROM v_rules")
        self.assertEqual([r[0] for r in rows], list(range(1, 11)))
        self.assertEqual(rows[0][1], "Entertainment:Streaming")
        self.assertIsNone(rows[6][1])  # rule 7 points to a missing category

    def test_unknown_attributes_kept(self):
        conn, _ = memory_db(sample(unknown_extras=True).data)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT extra_json FROM accounts WHERE key=1").fetchone()[0],
                         '{"futureattr": "1"}')
        self.assertEqual(conn.execute("SELECT element FROM unknown_elements").fetchall(), [("zzfuture",)])

    def test_homebank_example(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        counts = build_database(read_file(EXAMPLE), conn)
        self.assertEqual(counts["transactions"], 755)

    def test_export_replaces_previous(self):
        path = self.tmp / "db.sqlite"
        export_sqlite(self.book, path)
        export_sqlite(self.book, path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], len(self.book.transactions))
        conn.close()
        self.assertFalse(list(self.tmp.glob("*.tmp")))


class SchemaDocumentationTests(TempDirTestCase):
    """Every table, view and column must be described in docs/sqlite-schema.md."""

    def test_documented(self):
        doc = DOC.read_text(encoding="utf-8")
        conn, _ = memory_db(sample().data)
        self.addCleanup(conn.close)
        for name, _ in TABLES + VIEWS:
            self.assertIn(f"### {name}\n", doc, f"{name} is not documented")
            section = doc.split(f"### {name}\n", 1)[1].split("\n### ", 1)[0]
            for col in [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]:
                self.assertIn(f"`{col}`", section, f"{name}.{col} is not documented")


class CommandTests(TempDirTestCase):
    def test_command_and_privacy(self):
        path = self.write_sample(sentinel=True)
        out = self.tmp / "x.sqlite"
        code, stdout, stderr = run_cli("export-sqlite", "--file", str(path), "--output", str(out))
        self.assertEqual(code, 0, stderr)
        self.assertTrue(out.exists())
        self.assertIn("transactions", stdout)
        self.assertNoSentinel(stdout, stderr)
