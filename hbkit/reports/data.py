"""The reporting layer on top of the SQLite export: settings tables and shared views.

Views (documented in docs/sqlite-schema.md):

* r_lines            every transaction line (splits expanded), void transactions and
                     excluded accounts left out, with period and flow
* r_flows            r_lines without transfers: the source of every income/expense total
* r_group_lines      r_flows lines in each configured category group
* r_category_period  totals per period and category, nominal and inflation-adjusted
* r_period_totals    income, expenses, net savings and savings rate per period

flow is 'transfer' (between two included accounts), 'excluded' (a transfer to or
from an excluded account, left out entirely), 'income' (a line in an income
category, or an uncategorised positive line) or 'expense' (everything else, so a
refund in an expense category reduces spending).
"""

from __future__ import annotations

import calendar
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from ..export.sqlite import build_database
from ..xhb.model import Book
from .settings import Settings, read_cpi

REPORT_TABLES = [
    ("report_settings", "CREATE TABLE report_settings (key TEXT PRIMARY KEY, value TEXT)"),
    ("excluded_accounts", "CREATE TABLE excluded_accounts (account_key INTEGER PRIMARY KEY)"),
    ("group_members", """CREATE TABLE group_members (
            group_name   TEXT,
            category_key INTEGER,
            tag_name     TEXT
        )"""),
    ("cpi", "CREATE TABLE cpi (month TEXT PRIMARY KEY, idx REAL)"),
]

REPORT_VIEWS = [
    ("r_lines", """
        CREATE VIEW r_lines AS
        WITH mode AS (SELECT COALESCE((SELECT value FROM report_settings WHERE key = 'year_mode'), 'calendar') AS m),
        latest AS (SELECT idx FROM cpi ORDER BY month DESC LIMIT 1),
        last_cpi AS (SELECT MAX(month) AS month FROM cpi),
        lines AS (
            SELECT t.id AS transaction_id, 1 AS line_no, t.amount, t.category_key, t.memo AS memo
            FROM transactions t WHERE t.is_split = 0
            UNION ALL
            SELECT s.transaction_id, s.line_no, s.amount, s.category_key,
                   CASE WHEN s.memo <> '' THEN s.memo ELSE t.memo END
            FROM splits s JOIN transactions t ON t.id = s.transaction_id
        ),
        base AS (
            SELECT l.transaction_id, l.line_no, t.date, t.year, t.month,
                   CASE WHEN (SELECT m FROM mode) = 'tax' AND t.month >= 3 THEN t.year + 1 ELSE t.year END AS period,
                   t.account_key, a.name AS account, p.name AS payee, l.memo, t.tags, l.amount,
                   l.category_key, c.full_name AS category, vc.top_level AS category_top,
                   CASE
                       WHEN t.is_transfer = 1 AND t.transfer_account_key IN (SELECT account_key FROM excluded_accounts) THEN 'excluded'
                       WHEN t.is_transfer = 1 THEN 'transfer'
                       WHEN c.is_income = 1 THEN 'income'
                       WHEN c.key IS NULL AND l.amount > 0 THEN 'income'
                       ELSE 'expense'
                   END AS flow,
                   t.transfer_account_key,
                   CASE
                       WHEN NOT EXISTS (SELECT 1 FROM cpi) THEN NULL
                       WHEN substr(t.date, 1, 7) > (SELECT month FROM last_cpi) THEN 1.0
                       ELSE (SELECT idx FROM latest) / (SELECT idx FROM cpi WHERE cpi.month = substr(t.date, 1, 7))
                   END AS cpi_factor
            FROM lines l
            JOIN transactions t ON t.id = l.transaction_id
            LEFT JOIN accounts a ON a.key = t.account_key
            LEFT JOIN payees p ON p.key = t.payee_key
            LEFT JOIN categories c ON c.key = l.category_key AND l.category_key <> 0
            LEFT JOIN v_categories vc ON vc.key = c.key
            WHERE t.is_void = 0
              AND t.account_key NOT IN (SELECT account_key FROM excluded_accounts)
        )
        SELECT *,
               CASE WHEN (SELECT m FROM mode) = 'tax'
                    THEN (period - 1) || '/' || substr(CAST(period AS TEXT), 3, 2)
                    ELSE CAST(period AS TEXT) END AS period_label
        FROM base"""),
    ("r_flows", """
        CREATE VIEW r_flows AS
        SELECT * FROM r_lines WHERE flow IN ('income', 'expense')"""),
    ("r_group_lines", """
        CREATE VIEW r_group_lines AS
        SELECT gm.group_name, f.*
        FROM r_flows f
        JOIN (SELECT DISTINCT group_name, category_key FROM group_members WHERE category_key IS NOT NULL) gm
          ON gm.category_key = f.category_key
        UNION
        SELECT gm.group_name, f.*
        FROM r_flows f
        JOIN transaction_tags tt ON tt.transaction_id = f.transaction_id
        JOIN group_members gm ON gm.tag_name IS NOT NULL AND lower(gm.tag_name) = lower(tt.tag_name)"""),
    ("r_category_period", """
        CREATE VIEW r_category_period AS
        SELECT period, period_label, flow,
               category_key, COALESCE(category, '(uncategorised)') AS category,
               COALESCE(category_top, '(uncategorised)') AS category_top,
               ROUND(SUM(amount), 2) AS total,
               ROUND(SUM(amount * cpi_factor), 2) AS real_total,
               SUM(CASE WHEN cpi_factor IS NULL THEN 1 ELSE 0 END) AS lines_without_cpi,
               COUNT(*) AS lines
        FROM r_flows
        GROUP BY period, period_label, flow, category_key"""),
    ("r_period_totals", """
        CREATE VIEW r_period_totals AS
        SELECT period, period_label,
               ROUND(SUM(CASE WHEN flow = 'income' THEN amount ELSE 0 END), 2) AS income,
               ROUND(-SUM(CASE WHEN flow = 'expense' THEN amount ELSE 0 END), 2) AS expenses,
               ROUND(SUM(amount), 2) AS net_savings,
               CASE WHEN SUM(CASE WHEN flow = 'income' THEN amount ELSE 0 END) > 0
                    THEN ROUND(SUM(amount) / SUM(CASE WHEN flow = 'income' THEN amount ELSE 0 END), 4)
                    END AS savings_rate
        FROM r_flows
        GROUP BY period, period_label"""),
]


@dataclass
class ReportData:
    """An in-memory database with the reporting layer, plus what was resolved from settings."""

    conn: sqlite3.Connection
    settings: Settings
    excluded_accounts: list[str]
    groups: dict[str, set[int]]  # group -> category keys (tags matched in SQL)
    problems: Counter = field(default_factory=Counter)  # value-free descriptions, for the terminal
    problem_details: list[str] = field(default_factory=list)  # with names, for the report itself
    cpi_months: int = 0

    def close(self) -> None:
        self.conn.close()


def resolve_categories(book: Book, names: list[str]) -> tuple[set[int], list[str]]:
    """Category keys for names (full path or top-level name; a parent includes its children)."""
    paths = {k: (book.category_path(k) or "").casefold() for k in book.categories}
    keys, missing = set(), []
    for name in names:
        want = name.strip().casefold()
        found = {k for k, p in paths.items() if p == want}
        found |= {k for k, c in book.categories.items() if c.parent_key in found}
        if not found:
            missing.append(name)
        keys |= found
    return keys, missing


def add_report_layer(conn: sqlite3.Connection, book: Book, settings: Settings) -> ReportData:
    cur = conn.cursor()
    for _, ddl in REPORT_TABLES:
        cur.execute(ddl)
    for _, ddl in REPORT_VIEWS:
        cur.execute(ddl)
    data = ReportData(conn, settings, [], {})
    cur.execute("INSERT INTO report_settings VALUES ('year_mode', ?)", (settings.year_mode,))

    by_name = {a.name.casefold(): a for a in book.accounts.values()}
    for name in settings.exclude_accounts:
        acc = by_name.get(name.strip().casefold())
        if acc is None:
            data.problems["excluded account names not found in the file"] += 1
            data.problem_details.append(f"Excluded account not found: {name}")
            continue
        cur.execute("INSERT OR IGNORE INTO excluded_accounts VALUES (?)", (acc.key,))
        data.excluded_accounts.append(acc.name)

    tag_names = {t.name.casefold() for t in book.tags.values()}
    for group in settings.groups:
        keys, missing = resolve_categories(book, group.categories)
        data.groups[group.name] = keys
        for name in missing:
            data.problems["group category names not found in the file"] += 1
            data.problem_details.append(f"Group '{group.name}': category not found: {name}")
        cur.executemany("INSERT INTO group_members VALUES (?, ?, NULL)", [(group.name, k) for k in sorted(keys)])
        for tag in group.tags:
            if tag.strip().casefold() not in tag_names:
                data.problems["group tags not found in the file"] += 1
                data.problem_details.append(f"Group '{group.name}': tag not found: {tag}")
            cur.execute("INSERT INTO group_members VALUES (?, NULL, ?)", (group.name, tag.strip()))

    if settings.cpi_file:
        table, bad = read_cpi(settings.cpi_file)
        cur.executemany("INSERT INTO cpi VALUES (?, ?)", sorted(table.items()))
        data.cpi_months = len(table)
        if bad:
            data.problems["CPI rows that could not be read"] += bad
            data.problem_details.append(f"{bad} CPI row(s) could not be read and were ignored.")
    conn.commit()
    return data


def build_report_data(book: Book, settings: Settings) -> ReportData:
    conn = sqlite3.connect(":memory:")
    build_database(book, conn)
    return add_report_layer(conn, book, settings)


# --- periods ------------------------------------------------------------------------------------


def period_of(d: date, mode: str) -> int:
    return d.year + 1 if mode == "tax" and d.month >= 3 else d.year


def period_label(period: int, mode: str) -> str:
    return f"{period - 1}/{str(period)[-2:]}" if mode == "tax" else str(period)


def period_bounds(period: int, mode: str) -> tuple[date, date]:
    if mode == "tax":
        return date(period - 1, 3, 1), date(period, 2, calendar.monthrange(period, 2)[1])
    return date(period, 1, 1), date(period, 12, 31)


def period_title(period: int, mode: str) -> str:
    start, end = period_bounds(period, mode)
    if mode == "tax":
        return (f"Tax year {period_label(period, mode)} (year of assessment {period}: "
                f"{start.day} {start:%B %Y} – {end.day} {end:%B %Y})")
    return f"Calendar year {period}"


# --- balances ------------------------------------------------------------------------------------


def month_end_balances(book: Book, excluded: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Month-end balance per included account, like HomeBank's running balance.

    Opening balance plus every non-void transaction (transfers included). Every
    account gets a value for every month from the first to the last month in the
    file (before its first transaction that is its opening balance), so the
    accounts can be added up month by month. Accounts with no transactions and no
    opening balance are left out.
    """
    skip = {n.casefold() for n in excluded}
    txns = [t for t in book.transactions if not t.is_void and t.date is not None]
    if not txns:
        return {}
    first, last = min(t.date for t in txns), max(t.date for t in txns)
    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    per_account: dict[int, dict[tuple[int, int], float]] = {}
    for t in txns:
        per = per_account.setdefault(t.account_key, {})
        per[(t.date.year, t.date.month)] = per.get((t.date.year, t.date.month), 0.0) + t.amount
    out: dict[str, list[tuple[str, float]]] = {}
    for acc in sorted(book.accounts.values(), key=lambda a: (a.pos, a.key)):
        if acc.name.casefold() in skip or (acc.key not in per_account and not acc.initial):
            continue
        per = per_account.get(acc.key, {})
        balance, series = acc.initial, []
        for y, m in months:
            balance += per.get((y, m), 0.0)
            series.append((f"{y:04d}-{m:02d}", round(balance, 2)))
        name = acc.name if acc.name not in out else f"{acc.name} ({acc.key})"
        out[name] = series
    return out


__all__ = [
    "REPORT_TABLES", "REPORT_VIEWS", "ReportData", "add_report_layer", "build_report_data",
    "month_end_balances", "period_bounds", "period_label", "period_of", "period_title",
    "resolve_categories",
]
