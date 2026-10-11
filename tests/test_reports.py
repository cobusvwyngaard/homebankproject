"""The HTML reports, run on synthetic files only. Skipped when Plotly is not installed."""

import html
import importlib.util
import re
import unittest
from datetime import date

from hbkit.fixtures.generator import write_cpi
from hbkit.reports import discover
from hbkit.reports.base import run_report
from hbkit.reports.data import build_report_data, period_of
from hbkit.reports.html import money
from hbkit.reports.settings import Group, Settings
from hbkit.xhb.reader import read_bytes, read_file

from .helpers import TempDirTestCase, run_cli, sample
from .test_report_data import MINI, MINI_SETTINGS

HAS_PLOTLY = importlib.util.find_spec("plotly") is not None
needs_plotly = unittest.skipUnless(HAS_PLOTLY, "Plotly is not installed (see requirements.txt)")

SETTINGS_TOML = """
[period]
year_mode = "tax"
[accounts]
exclude = ["Business account", "Old cheque account"]
[groups."tax: rental – property A"]
categories = ["Rental A"]
tags = ["propA"]
[groups."tax: medical"]
categories = ["Medical"]
tags = ["medical"]
[groups.essential]
categories = ["Groceries", "Utilities", "Transport:Fuel"]
[groups.discretionary]
categories = ["Eating out", "Entertainment", "Clothing"]
[inflation]
cpi_file = "cpi.csv"
"""


def text_of(page: str) -> str:
    """The visible text of an HTML page, roughly (for assertions)."""
    page = re.sub(r"<(script|style).*?</\1>", " ", page, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


class ReportTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_sample("rep.xhb", reporting=True)
        write_cpi(self.tmp / "cpi.csv")
        self.settings_path = self.tmp / "settings.toml"
        self.settings_path.write_text(SETTINGS_TOML, encoding="utf-8")
        self.out = self.tmp / "out"

    def cli(self, name, *extra, sentinel=False):
        src = self.write_sample("s.xhb", reporting=True, sentinel=True) if sentinel else self.src
        before = set((self.out / "reports").glob("*.html")) if (self.out / "reports").exists() else set()
        code, out, err = run_cli("report", "run", name, "--file", str(src), "--settings", str(self.settings_path),
                                 "--out", str(self.out), *extra)
        self.assertEqual(code, 0, err)
        new = set((self.out / "reports").glob(f"{name}_*.html")) - before
        self.assertEqual(len(new), 1, "each run writes exactly one new page")
        return new.pop().read_text(encoding="utf-8"), out, err

    def run_mini(self, name, settings=MINI_SETTINGS, **kwargs):
        report = discover()[name]
        path = self.tmp / "mini.xhb"
        path.write_bytes(MINI)
        summary, page = run_report(report, read_file(path), path, settings, self.out, report_defaults(report),
                                   **kwargs)
        return page.read_text(encoding="utf-8"), summary


def report_defaults(report):
    return {p.name: p.default for p in report.params}


@needs_plotly
class CommonTests(ReportTestCase):
    def test_runs_never_overwrite_each_other(self):
        for _ in range(3):
            self.cli("annual-overview", "--year", "2025")
        self.assertEqual(len(list((self.out / "reports").glob("annual-overview_2024-25_*.html"))), 3)

    def test_every_report_is_self_contained_and_private(self):
        for name in discover():
            with self.subTest(report=name):
                page, out, err = self.cli(name, sentinel=True)
                self.assertNoSentinel(out, err)  # the terminal never shows file text
                self.assertNotRegex(page, r"<script[^>]+src=")  # nothing loaded from elsewhere
                self.assertNotRegex(page, r"<link[^>]+href=\"?https?:")
                self.assertIn("Plotly", page) if discover()[name].period_kind == "range" else None
                text = text_of(page)
                for label in ("Report", "Year mode", "Data range", "Generated", "Settings", "HomeBank file"):
                    self.assertIn(label, text)

    def test_list(self):
        code, out, _ = run_cli("report", "list")
        self.assertEqual(code, 0)
        for name in discover():
            self.assertIn(name, out)

    def test_no_plotly_message(self):
        from hbkit.reports import html as rhtml

        self.assertIn("--no-index --find-links vendor", rhtml.INSTALL_HINT)


@needs_plotly
class AnnualOverviewTests(ReportTestCase):
    def test_hand_checked_mini_file(self):
        page, summary = self.run_mini("annual-overview")
        text = text_of(page)
        # 2024/25: income 1000, expenses 350, net 650; 2025/26: income 5, expenses 27, net -22
        for amount in ("R 1 000.00", "R 350.00", "R 650.00", "R 5.00", "R 27.00", "-R 22.00"):
            self.assertIn(amount, text)
        self.assertIn("2024/25", text)
        self.assertIn("Business", text)  # listed as excluded

    def test_totals_match_the_views(self):
        page, _, _ = self.cli("annual-overview")
        text = text_of(page)
        book = read_bytes(sample(reporting=True).data)
        settings = Settings(year_mode="tax", exclude_accounts=["Business account", "Old cheque account"])
        data = build_report_data(book, settings)
        for period, income, expenses, net in data.conn.execute(
                "SELECT period, income, expenses, net_savings FROM r_period_totals"):
            for value in (income, expenses, net):
                self.assertIn(money(value), text, period)
        data.close()

    def test_partial_year_is_compared_with_the_same_months(self):
        page, _, _ = self.cli("annual-overview")
        text = text_of(page)
        self.assertIn("vs the same months of 2024/25", text)
        self.assertIn("compared with 2024/25 up to 30 Sep 2024", text)
        # independent calculation of 2024/25 income up to 30 Sep 2024
        book = read_bytes(sample(reporting=True).data)
        business = {a.key for a in book.accounts.values() if a.name in ("Business account", "Old cheque account")}
        income = 0.0
        for t in book.transactions:
            if (t.is_void or t.account_key in business or t.is_internal_transfer
                    or not date(2024, 3, 1) <= t.date <= date(2024, 9, 30)):
                continue
            for key, amount in [(s.category_key, s.amount) for s in t.splits] or [(t.category_key, t.amount)]:
                cat = book.categories.get(key)
                if (cat.is_income if cat else amount > 0):
                    income += amount
        current = float(re.search(r"Income\s+R ([\d ]+)", text).group(1).replace(" ", ""))
        diff = float(re.search(r"Income\s+R [\d ]+\s+([+-]?)R ([\d ]+)", text).group(2).replace(" ", ""))
        self.assertAlmostEqual(current - diff, income, delta=1.0)  # rounded to whole rand

    def test_year_options(self):
        page, _, _ = self.cli("annual-overview", "--year", "2025")
        self.assertIn("Tax year 2024/25", text_of(page))
        page, _, _ = self.cli("annual-overview", "--from", "2022", "--to", "2024")
        self.assertIn("2021/22 to 2023/24", text_of(page))
        code, _, err = run_cli("report", "run", "annual-overview", "--file", str(self.src), "--settings",
                               str(self.settings_path), "--out", str(self.out), "--year", "2010")
        self.assertEqual(code, 2)
        self.assertIn("no transactions in that", err)

    def test_balances_table(self):
        page, _, _ = self.cli("annual-overview")
        text = text_of(page)
        self.assertIn("Balance at the end of each year", text)
        self.assertNotIn("Business account  R", text)  # excluded accounts are not in the balances
        book = read_bytes(sample(reporting=True).data)
        cheque = next(a for a in book.accounts.values() if a.name == "Cheque account")
        final = cheque.initial + sum(t.amount for t in book.transactions
                                     if t.account_key == cheque.key and not t.is_void)
        self.assertIn(money(final, "R", 0), text)

    def test_calendar_mode(self):
        page, _ = self.run_mini("annual-overview", Settings(year_mode="calendar", exclude_accounts=["Business"]))
        text = text_of(page)
        self.assertIn("R 1 005.00", text)  # 2025 income
        self.assertIn("R 377.00", text)  # 2025 expenses


if __name__ == "__main__":
    unittest.main()
