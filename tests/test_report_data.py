"""The reporting layer: settings, the shared r_* views, periods and balances.

The core tests use a tiny hand-written HomeBank file whose expected totals were
worked out by hand, so they do not merely repeat the code's own logic.
"""

import sqlite3
import unittest
from datetime import date
from pathlib import Path

from hbkit.errors import HbkitError
from hbkit.export.sqlite import export_sqlite
from hbkit.fixtures.generator import write_cpi
from hbkit.reports.data import (
    REPORT_TABLES, REPORT_VIEWS, build_report_data, month_end_balances, period_bounds, period_label,
    period_of, period_title,
)
from hbkit.reports.settings import Group, Settings, load_settings, read_cpi
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample

PROJECT = Path(__file__).resolve().parent.parent


def j(y, m, d):
    return date(y, m, d).toordinal()


# Accounts: 1 Cheque (opening 1000), 2 Savings, 3 Business (excluded in the tests).
# Categories: 1 Salary (income), 2 Groceries, 3 Household, 4 Household:Cleaning.
MINI = f"""<?xml version="1.0"?>
<homebank v="1.6" d="051003">
<properties title="Mini" curr="1" auto_smode="0" earnbyh="0"/>
<cur key="1" flags="0" iso="ZAR" name="Rand" symb="R" syprf="1" dchar="." gchar=" " frac="2" rate="0" mdate="0"/>
<account key="1" pos="1" type="6" curr="1" name="Cheque" initial="1000" minimum="0" maximum="0"/>
<account key="2" pos="2" type="7" curr="1" name="Savings" initial="0" minimum="0" maximum="0"/>
<account key="3" pos="3" type="6" curr="1" name="Business" initial="0" minimum="0" maximum="0"/>
<pay key="1" name="Shop"/>
<cat key="1" flags="2" name="Salary"/>
<cat key="2" name="Groceries"/>
<cat key="3" name="Household"/>
<cat key="4" parent="3" flags="1" name="Cleaning"/>
<tag key="1" name="propA"/>
<ope date="{j(2025, 1, 10)}" amount="1000" account="1" flags="2" category="1" wording="a salary"/>
<ope date="{j(2025, 1, 12)}" amount="-200" account="1" payee="1" category="2" wording="b groceries"/>
<ope date="{j(2025, 1, 15)}" amount="-300" account="1" dst_account="2" flags="8" wording="c to savings" kxfer="1"/>
<ope date="{j(2025, 1, 20)}" amount="-100" account="1" dst_account="3" flags="8" wording="d to business" kxfer="2"/>
<ope date="{j(2025, 2, 28)}" amount="-150" account="1" flags="256" wording="e split" scat="2||4" samt="-100||-50" smem="||soap"/>
<ope date="{j(2025, 3, 1)}" amount="20" account="1" flags="2" category="2" wording="f refund"/>
<ope date="{j(2025, 3, 2)}" amount="-999" account="1" st="3" category="2" wording="g void"/>
<ope date="{j(2025, 3, 5)}" amount="5" account="1" flags="2" wording="h uncategorised in"/>
<ope date="{j(2025, 3, 6)}" amount="-7" account="1" wording="i uncategorised out"/>
<ope date="{j(2025, 3, 8)}" amount="-40" account="1" category="3" tags="propA" wording="k household tagged"/>
<ope date="{j(2025, 1, 15)}" amount="300" account="2" dst_account="1" flags="10" wording="c from cheque" kxfer="1"/>
<ope date="{j(2025, 1, 20)}" amount="100" account="3" dst_account="1" flags="10" wording="d from cheque" kxfer="2"/>
<ope date="{j(2025, 3, 7)}" amount="5000" account="3" flags="2" category="1" wording="j business income"/>
</homebank>
""".encode()

MINI_SETTINGS = Settings(
    year_mode="tax",
    exclude_accounts=["business"],
    groups=[Group("home", categories=["Household"]), Group("prop", tags=["PROPA"]),
            Group("groceries", categories=["Groceries"])],
)


def q(data, sql, *args):
    return data.conn.execute(sql, args).fetchall()


class HandCheckedTotalsTests(unittest.TestCase):
    """Every number below was worked out by hand from MINI."""

    def setUp(self):
        self.book = read_bytes(MINI)
        self.data = build_report_data(self.book, MINI_SETTINGS)

    def tearDown(self):
        self.data.close()

    def test_tax_year_totals(self):
        rows = q(self.data, "SELECT period, period_label, income, expenses, net_savings FROM r_period_totals ORDER BY period")
        # 2024/25: income a 1000; expenses b 200 + split e 150 = 350.
        # 2025/26: income h 5; expenses -20 (refund f) + 7 (i) + 40 (k) = 27; net 5 - 27 = -22.
        self.assertEqual(rows, [(2025, "2024/25", 1000.0, 350.0, 650.0), (2026, "2025/26", 5.0, 27.0, -22.0)])

    def test_calendar_year_totals(self):
        data = build_report_data(self.book, Settings(year_mode="calendar", exclude_accounts=["Business"]))
        rows = q(data, "SELECT period, period_label, income, expenses, net_savings, savings_rate FROM r_period_totals")
        self.assertEqual(rows, [(2025, "2025", 1005.0, 377.0, 628.0, round(628 / 1005, 4))])
        data.close()

    def test_transfers_are_flagged_and_left_out_of_totals(self):
        flows = dict(q(self.data, "SELECT memo, flow FROM r_lines"))
        self.assertEqual(flows["c to savings"], "transfer")
        self.assertEqual(flows["c from cheque"], "transfer")  # both legs
        self.assertEqual(flows["d to business"], "excluded")  # other side is an excluded account
        self.assertNotIn("d from cheque", flows)  # the excluded account itself is left out
        self.assertNotIn("j business income", flows)
        self.assertNotIn("g void", flows)
        flow_memos = {r[0] for r in q(self.data, "SELECT memo FROM r_flows")}
        self.assertFalse({"c to savings", "c from cheque", "d to business"} & flow_memos)
        self.assertEqual(q(self.data, "SELECT SUM(amount) FROM r_lines WHERE flow = 'transfer'"), [(0.0,)])

    def test_transfers_do_not_change_totals(self):
        # Remove the transfers from the file: income and expense totals must be identical.
        without = b"\n".join(line for line in MINI.split(b"\n") if b'kxfer="' not in line)
        other = build_report_data(read_bytes(without), MINI_SETTINGS)
        sql = "SELECT period, income, expenses FROM r_period_totals ORDER BY period"
        self.assertEqual(q(self.data, sql), q(other, sql))
        other.close()

    def test_splits_are_expanded(self):
        rows = q(self.data, "SELECT line_no, category, amount, memo FROM r_lines WHERE transaction_id = "
                            "(SELECT transaction_id FROM r_lines WHERE memo = 'soap') ORDER BY line_no")
        self.assertEqual(rows, [(1, "Groceries", -100.0, "e split"), (2, "Household:Cleaning", -50.0, "soap")])

    def test_tax_year_boundary(self):
        periods = dict(q(self.data, "SELECT memo, period_label FROM r_lines"))
        self.assertEqual(periods["e split"], "2024/25")  # 28 February 2025
        self.assertEqual(periods["f refund"], "2025/26")  # 1 March 2025

    def test_flow_classification(self):
        flows = dict(q(self.data, "SELECT memo, flow FROM r_flows"))
        self.assertEqual(flows["f refund"], "expense")  # a refund reduces spending
        self.assertEqual(flows["h uncategorised in"], "income")
        self.assertEqual(flows["i uncategorised out"], "expense")
        self.assertEqual(flows["a salary"], "income")

    def test_groups(self):
        rows = q(self.data, "SELECT group_name, COUNT(*), SUM(amount) FROM r_group_lines GROUP BY group_name ORDER BY 1")
        # groceries: b -200, split line -100, refund +20; home: split line Cleaning -50 and k -40
        # (a parent includes its children); prop: k by tag (case-insensitive).
        self.assertEqual(rows, [("groceries", 3, -280.0), ("home", 2, -90.0), ("prop", 1, -40.0)])

    def test_a_line_matched_twice_counts_once(self):
        data = build_report_data(self.book, Settings(
            year_mode="tax", exclude_accounts=["Business"],
            groups=[Group("both", categories=["Household"], tags=["propA"])]))
        self.assertEqual(q(data, "SELECT COUNT(*), SUM(amount) FROM r_group_lines"), [(2, -90.0)])
        data.close()

    def test_category_totals(self):
        rows = q(self.data, "SELECT period, category, total, lines FROM r_category_period "
                            "WHERE flow = 'expense' ORDER BY period, category")
        self.assertEqual(rows, [
            (2025, "Groceries", -300.0, 2), (2025, "Household:Cleaning", -50.0, 1),
            (2026, "(uncategorised)", -7.0, 1), (2026, "Groceries", 20.0, 1), (2026, "Household", -40.0, 1),
        ])

    def test_balances(self):
        balances = month_end_balances(self.book, ["Business"])
        self.assertEqual(set(balances), {"Cheque", "Savings"})
        # Cheque: 1000 + 1000 - 200 - 300 - 100 - 150 + 20 + 5 - 7 - 40 (void left out) = 1228
        self.assertEqual(balances["Cheque"], [("2025-01", 1400.0), ("2025-02", 1250.0), ("2025-03", 1228.0)])
        self.assertEqual(balances["Savings"], [("2025-01", 300.0), ("2025-02", 300.0), ("2025-03", 300.0)])

    def test_unknown_names_are_counted(self):
        data = build_report_data(self.book, Settings(
            exclude_accounts=["Nope"], groups=[Group("g", categories=["Missing"], tags=["nosuchtag"])]))
        self.assertEqual(dict(data.problems), {
            "excluded account names not found in the file": 1,
            "group category names not found in the file": 1,
            "group tags not found in the file": 1,
        })
        data.close()


class PeriodTests(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(period_of(date(2025, 2, 28), "tax"), 2025)
        self.assertEqual(period_of(date(2025, 3, 1), "tax"), 2026)
        self.assertEqual(period_of(date(2024, 2, 29), "tax"), 2024)
        self.assertEqual(period_of(date(2025, 3, 1), "calendar"), 2025)
        self.assertEqual(period_label(2026, "tax"), "2025/26")
        self.assertEqual(period_label(2000, "tax"), "1999/00")
        self.assertEqual(period_bounds(2024, "tax"), (date(2023, 3, 1), date(2024, 2, 29)))
        self.assertEqual(period_bounds(2025, "calendar"), (date(2025, 1, 1), date(2025, 12, 31)))
        self.assertIn("1 March 2025 – 28 February 2026", period_title(2026, "tax"))

    def test_sql_agrees_with_python_on_generated_data(self):
        book = read_bytes(sample(reporting=True).data)
        data = build_report_data(book, Settings(year_mode="tax"))
        for d, period in q(data, "SELECT DISTINCT date, period FROM r_lines"):
            self.assertEqual(period, period_of(date.fromisoformat(d), "tax"), d)
        data.close()


class GeneratedFileTests(unittest.TestCase):
    """The same rules hold on the larger synthetic file (computed independently in Python)."""

    def test_totals_match_a_python_calculation(self):
        book = read_bytes(sample(reporting=True).data)
        settings = Settings(year_mode="tax", exclude_accounts=["Business account"])
        data = build_report_data(book, settings)
        business = next(a.key for a in book.accounts.values() if a.name == "Business account")
        income, expenses = {}, {}
        for t in book.transactions:
            if t.is_void or t.account_key == business or t.is_internal_transfer:
                continue
            period = period_of(t.date, "tax")
            for key, amount in [(s.category_key, s.amount) for s in t.splits] or [(t.category_key, t.amount)]:
                cat = book.categories.get(key)
                is_income = cat.is_income if cat else amount > 0
                target = income if is_income else expenses
                target[period] = target.get(period, 0.0) + (amount if is_income else -amount)
        for period, inc, exp in q(data, "SELECT period, income, expenses FROM r_period_totals"):
            self.assertAlmostEqual(inc, income.get(period, 0.0), places=2)
            self.assertAlmostEqual(exp, expenses.get(period, 0.0), places=2)
        data.close()

    def test_split_lines_add_up(self):
        book = read_bytes(sample(reporting=True).data)
        data = build_report_data(book, Settings())
        rows = q(data, "SELECT t.amount, SUM(l.amount) FROM r_lines l JOIN transactions t ON t.id = l.transaction_id "
                       "WHERE t.is_split = 1 GROUP BY t.id")
        self.assertTrue(rows)
        for total, lines in rows:
            self.assertAlmostEqual(total, lines, places=2)
        data.close()


class SettingsTests(TempDirTestCase):
    def test_example_file_loads(self):
        s = load_settings(PROJECT / "settings.example.toml")
        self.assertEqual(s.year_mode, "tax")
        self.assertIn("tax: medical", [g.name for g in s.groups])
        self.assertIsNone(s.cpi_file)

    def test_defaults_without_file(self):
        self.assertEqual(load_settings(None).year_mode in ("calendar", "tax"), True)

    def test_errors(self):
        bad = self.tmp / "bad.toml"
        bad.write_text('[period]\nyear_mode = "weekly"\n')
        with self.assertRaises(HbkitError):
            load_settings(bad)
        bad.write_text("[period\n")
        with self.assertRaises(HbkitError) as cm:
            load_settings(bad)
        self.assertIn("line", str(cm.exception))
        bad.write_text('[groups.x]\ncategories = 5\n')
        with self.assertRaises(HbkitError):
            load_settings(bad)
        with self.assertRaises(HbkitError):
            load_settings(self.tmp / "missing.toml")

    def test_cpi_path_next_to_settings(self):
        write_cpi(self.tmp / "cpi.csv")
        path = self.tmp / "s.toml"
        path.write_text('[inflation]\ncpi_file = "cpi.csv"\n')
        self.assertEqual(load_settings(path).cpi_file, self.tmp / "cpi.csv")

    def test_cpi_formats(self):
        path = self.tmp / "cpi.csv"
        path.write_text("Month;Index\n2024/01;100,5\n202402;101\n2024-03-01;102.5\nrubbish;x\n", encoding="utf-8")
        table, bad = read_cpi(path)
        self.assertEqual(table, {"2024-01": 100.5, "2024-02": 101.0, "2024-03": 102.5})
        self.assertEqual(bad, 1)
        path.write_text("nothing useful\n")
        with self.assertRaises(HbkitError):
            read_cpi(path)

    def test_cpi_factor(self):
        cpi = self.tmp / "cpi.csv"
        cpi.write_text("month,index\n2025-01,100\n2025-02,110\n")
        settings = Settings(year_mode="calendar", exclude_accounts=["Business"], cpi_file=cpi)
        data = build_report_data(read_bytes(MINI), settings)
        factors = dict(q(data, "SELECT memo, cpi_factor FROM r_lines"))
        self.assertAlmostEqual(factors["b groceries"], 1.1)  # January prices in February's terms
        self.assertAlmostEqual(factors["e split"], 1.0)
        self.assertEqual(factors["f refund"], 1.0)  # after the last CPI month: treated as current
        data.close()


class ExportAndDocsTests(TempDirTestCase):
    def test_export_with_settings_adds_views(self):
        path = self.tmp / "db.sqlite"
        export_sqlite(read_bytes(MINI), path, MINI_SETTINGS)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM r_period_totals").fetchone()[0], 2)
        conn.close()

    def test_report_tables_and_views_are_documented(self):
        doc = (PROJECT / "docs" / "sqlite-schema.md").read_text(encoding="utf-8")
        data = build_report_data(read_bytes(MINI), MINI_SETTINGS)
        self.addCleanup(data.close)
        for name, _ in REPORT_TABLES + REPORT_VIEWS:
            self.assertIn(f"### {name}\n", doc, f"{name} is not documented")
            section = doc.split(f"### {name}\n", 1)[1].split("\n### ", 1)[0]
            for col in [r[1] for r in data.conn.execute(f"PRAGMA table_info({name})")]:
                self.assertIn(f"`{col}`", section, f"{name}.{col} is not documented")


if __name__ == "__main__":
    unittest.main()


class PeriodChoiceTests(unittest.TestCase):
    def test_range(self):
        from hbkit.reports.base import choose_periods

        ps, last = [2024, 2025, 2026], date(2025, 9, 30)
        self.assertEqual(choose_periods("range", ps, last, "tax", None, None, None), ps)
        self.assertEqual(choose_periods("range", ps, last, "tax", 2025, None, None), [2025])
        self.assertEqual(choose_periods("range", ps, last, "tax", None, 2025, None), [2025, 2026])
        with self.assertRaises(HbkitError):
            choose_periods("range", ps, last, "tax", 2025, 2024, None)
        with self.assertRaises(HbkitError):
            choose_periods("range", ps, last, "tax", None, 2030, 2031)

    def test_single_defaults_to_the_latest_finished_year(self):
        from hbkit.reports.base import choose_periods

        ps, last = [2024, 2025, 2026], date(2025, 9, 30)
        self.assertEqual(choose_periods("single", ps, last, "tax", None, None, None), [2025])  # ended Feb 2025
        self.assertEqual(choose_periods("single", [2024, 2025], last, "calendar", None, None, None), [2024])
        self.assertEqual(choose_periods("single", ps, last, "tax", 2026, None, None), [2026])
        with self.assertRaises(HbkitError):
            choose_periods("single", ps, last, "tax", 2019, None, None)
        with self.assertRaises(HbkitError):
            choose_periods("single", ps, last, "tax", None, 2024, 2025)
