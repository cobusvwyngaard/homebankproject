"""Export a HomeBank book to a local SQLite database.

The schema is documented in docs/sqlite-schema.md (a test checks that every table,
view and column is described there). Amounts are signed as in HomeBank (negative =
money out) and rounded to the currency's number of decimals. Dates are ISO text
(YYYY-MM-DD). Attributes this toolkit does not know are kept as JSON in
``extra_json`` columns.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..errors import HbkitError
from ..xhb import format as F
from ..xhb.model import Book, julian_to_date

SCHEMA_VERSION = 1

TABLES: list[tuple[str, str]] = [
    ("meta", """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )"""),
    ("currencies", """
        CREATE TABLE currencies (
            key           INTEGER PRIMARY KEY,
            iso           TEXT,
            name          TEXT,
            symbol        TEXT,
            symbol_prefix INTEGER,
            decimal_char  TEXT,
            grouping_char TEXT,
            frac_digits   INTEGER,
            rate          REAL,
            flags         INTEGER,
            extra_json    TEXT
        )"""),
    ("account_groups", """
        CREATE TABLE account_groups (
            key        INTEGER PRIMARY KEY,
            name       TEXT,
            extra_json TEXT
        )"""),
    ("accounts", """
        CREATE TABLE accounts (
            key             INTEGER PRIMARY KEY,
            name            TEXT,
            type            INTEGER,
            type_name       TEXT,
            currency_key    INTEGER,
            flags           INTEGER,
            is_closed       INTEGER,
            position        INTEGER,
            group_key       INTEGER,
            number          TEXT,
            bank_name       TEXT,
            initial_balance REAL,
            minimum_balance REAL,
            maximum_balance REAL,
            website         TEXT,
            notes           TEXT,
            cc_day          INTEGER,
            reconciled_date TEXT,
            extra_json      TEXT
        )"""),
    ("payees", """
        CREATE TABLE payees (
            key                  INTEGER PRIMARY KEY,
            name                 TEXT,
            default_category_key INTEGER,
            default_paymode      INTEGER,
            flags                INTEGER,
            notes                TEXT,
            extra_json           TEXT
        )"""),
    ("categories", """
        CREATE TABLE categories (
            key        INTEGER PRIMARY KEY,
            parent_key INTEGER,
            name       TEXT,
            full_name  TEXT,
            flags      INTEGER,
            is_income  INTEGER,
            is_hidden  INTEGER,
            extra_json TEXT
        )"""),
    ("category_budgets", """
        CREATE TABLE category_budgets (
            category_key INTEGER,
            month        INTEGER,
            amount       REAL,
            PRIMARY KEY (category_key, month)
        )"""),
    ("tags", """
        CREATE TABLE tags (
            key        INTEGER PRIMARY KEY,
            name       TEXT,
            extra_json TEXT
        )"""),
    ("rules", """
        CREATE TABLE rules (
            key                    INTEGER PRIMARY KEY,
            position               INTEGER,
            field                  INTEGER,
            field_name             TEXT,
            search_text            TEXT,
            notes                  TEXT,
            flags                  INTEGER,
            is_regex               INTEGER,
            case_sensitive         INTEGER,
            matches_amount         INTEGER,
            sets_payee_if_empty    INTEGER,
            sets_category_if_empty INTEGER,
            sets_paymode_if_empty  INTEGER,
            sets_tags_if_empty     INTEGER,
            overwrites_payee       INTEGER,
            overwrites_category    INTEGER,
            overwrites_paymode     INTEGER,
            overwrites_tags        INTEGER,
            payee_key              INTEGER,
            category_key           INTEGER,
            paymode                INTEGER,
            amount                 REAL,
            tags                   TEXT,
            extra_json             TEXT
        )"""),
    ("templates", """
        CREATE TABLE templates (
            key             INTEGER PRIMARY KEY,
            account_key     INTEGER,
            amount          REAL,
            paymode         INTEGER,
            flags           INTEGER,
            payee_key       INTEGER,
            category_key    INTEGER,
            memo            TEXT,
            info            TEXT,
            tags            TEXT,
            transfer_account_key INTEGER,
            recurrence_flags INTEGER,
            next_date       TEXT,
            every           INTEGER,
            unit            INTEGER,
            recurrence_limit INTEGER,
            splits_json     TEXT,
            extra_json      TEXT
        )"""),
    ("transactions", """
        CREATE TABLE transactions (
            id                   INTEGER PRIMARY KEY,
            date                 TEXT,
            date_julian          INTEGER,
            year                 INTEGER,
            month                INTEGER,
            account_key          INTEGER,
            amount               REAL,
            paymode              INTEGER,
            paymode_name         TEXT,
            status               INTEGER,
            status_name          TEXT,
            flags                INTEGER,
            is_income            INTEGER,
            is_split             INTEGER,
            is_transfer          INTEGER,
            is_void              INTEGER,
            is_remind            INTEGER,
            payee_key            INTEGER,
            category_key         INTEGER,
            memo                 TEXT,
            info                 TEXT,
            tags                 TEXT,
            transfer_link        INTEGER,
            transfer_account_key INTEGER,
            transfer_amount      REAL,
            extra_json           TEXT
        )"""),
    ("splits", """
        CREATE TABLE splits (
            transaction_id INTEGER,
            line_no        INTEGER,
            category_key   INTEGER,
            amount         REAL,
            memo           TEXT,
            PRIMARY KEY (transaction_id, line_no)
        )"""),
    ("transaction_tags", """
        CREATE TABLE transaction_tags (
            transaction_id INTEGER,
            tag_key        INTEGER,
            tag_name       TEXT
        )"""),
    ("filters", """
        CREATE TABLE filters (
            key         INTEGER PRIMARY KEY,
            name        TEXT,
            groups_json TEXT,
            extra_json  TEXT
        )"""),
    ("unknown_elements", """
        CREATE TABLE unknown_elements (
            id              INTEGER PRIMARY KEY,
            element         TEXT,
            line            INTEGER,
            attributes_json TEXT
        )"""),
]

VIEWS: list[tuple[str, str]] = [
    ("v_categories", """
        CREATE VIEW v_categories AS
        SELECT c.key,
               c.full_name,
               COALESCE(p.name, c.name)                         AS top_level,
               CASE WHEN p.key IS NULL THEN NULL ELSE c.name END AS sub_name,
               c.is_income,
               c.is_hidden
        FROM categories c
        LEFT JOIN categories p ON p.key = c.parent_key AND c.parent_key <> 0"""),
    ("v_transactions", """
        CREATE VIEW v_transactions AS
        SELECT t.id,
               t.date,
               t.year,
               t.month,
               substr(t.date, 1, 7)                                   AS year_month,
               a.name                                                 AS account,
               cur.iso                                                AS currency,
               t.amount,
               CASE WHEN t.is_transfer = 1 THEN 'transfer'
                    WHEN t.amount > 0 THEN 'income' ELSE 'expense' END AS type,
               p.name                                                 AS payee,
               CASE WHEN t.is_split = 1 THEN '(split)' ELSE vc.full_name END AS category,
               CASE WHEN t.is_split = 1 THEN '(split)' ELSE vc.top_level END AS category_top,
               t.memo,
               t.info,
               t.tags,
               t.status_name                                          AS status,
               t.is_split,
               t.is_void,
               t.payee_key,
               t.category_key,
               t.account_key
        FROM transactions t
        LEFT JOIN accounts a      ON a.key = t.account_key
        LEFT JOIN currencies cur  ON cur.key = a.currency_key
        LEFT JOIN payees p        ON p.key = t.payee_key
        LEFT JOIN v_categories vc ON vc.key = t.category_key"""),
    ("v_transaction_lines", """
        CREATE VIEW v_transaction_lines AS
        SELECT vt.id AS transaction_id, 1 AS line_no, vt.date, vt.year, vt.month, vt.year_month,
               vt.account, vt.currency, vt.amount, vt.type, vt.payee,
               vt.category_key, vt.category, vt.category_top, vt.memo, 0 AS is_split_line, vt.is_void
        FROM v_transactions vt
        WHERE vt.is_split = 0
        UNION ALL
        SELECT vt.id, s.line_no, vt.date, vt.year, vt.month, vt.year_month,
               vt.account, vt.currency, s.amount,
               CASE WHEN s.amount > 0 THEN 'income' ELSE 'expense' END, vt.payee,
               s.category_key, vc.full_name, vc.top_level,
               CASE WHEN s.memo <> '' THEN s.memo ELSE vt.memo END, 1, vt.is_void
        FROM splits s
        JOIN v_transactions vt    ON vt.id = s.transaction_id
        LEFT JOIN v_categories vc ON vc.key = s.category_key"""),
    ("v_monthly_category_totals", """
        CREATE VIEW v_monthly_category_totals AS
        SELECT year_month,
               currency,
               COALESCE(category, '(uncategorised)')     AS category,
               COALESCE(category_top, '(uncategorised)') AS category_top,
               ROUND(SUM(amount), 2)                     AS total,
               COUNT(*)                                  AS lines
        FROM v_transaction_lines
        WHERE is_void = 0 AND type <> 'transfer'
        GROUP BY year_month, currency, category, category_top"""),
    ("v_rules", """
        CREATE VIEW v_rules AS
        SELECT r.position, r.key, r.field_name, r.search_text, r.is_regex, r.case_sensitive,
               r.sets_category_if_empty, r.overwrites_category,
               vc.full_name AS category,
               p.name       AS payee,
               r.notes
        FROM rules r
        LEFT JOIN v_categories vc ON vc.key = r.category_key
        LEFT JOIN payees p        ON p.key = r.payee_key
        ORDER BY r.position, r.key"""),
]


def _extra(entity) -> str | None:
    return json.dumps(entity.extra, ensure_ascii=False) if entity.extra else None


def _iso(julian: int) -> str | None:
    d = julian_to_date(julian)
    return d.isoformat() if d else None


def _b(flag: int, bit: int) -> int:
    return 1 if flag & bit else 0


def build_database(book: Book, conn: sqlite3.Connection) -> dict[str, int]:
    """Create the schema in `conn` and fill it from `book`. Returns row counts."""
    cur = conn.cursor()
    for _, ddl in TABLES:
        cur.execute(ddl)
    for _, ddl in VIEWS:
        cur.execute(ddl)
    cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "toolkit_version": __version__,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "homebank_file_version": book.version,
        "homebank_data_version": book.data_version_text,
        "source_sha256": hashlib.sha256(book.raw).hexdigest(),
    }
    cur.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())

    cur.executemany("INSERT OR REPLACE INTO currencies VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (c.key, c.iso, c.name, c.symbol, c.symbol_prefix, c.decimal_char, c.grouping_char,
         c.frac_digits, c.rate, c.flags, _extra(c)) for c in book.currencies.values()])
    cur.executemany("INSERT OR REPLACE INTO account_groups VALUES (?,?,?)", [
        (g.key, g.name, _extra(g)) for g in book.groups.values()])
    cur.executemany("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (a.key, a.name, a.type, a.type_name, a.currency_key or book.properties.currency_key, a.flags,
         int(a.is_closed), a.pos, a.group_key, a.number, a.bank_name, a.initial, a.minimum, a.maximum,
         a.website, a.notes, a.cc_day, _iso(a.reconciled_julian) if a.reconciled_julian else None,
         _extra(a)) for a in book.accounts.values()])
    cur.executemany("INSERT OR REPLACE INTO payees VALUES (?,?,?,?,?,?,?)", [
        (p.key, p.name, p.category_key, p.paymode, p.flags, p.notes, _extra(p))
        for p in book.payees.values()])
    cur.executemany("INSERT OR REPLACE INTO categories VALUES (?,?,?,?,?,?,?,?)", [
        (c.key, c.parent_key, c.name, book.category_path(c.key), c.flags, int(c.is_income),
         int(c.is_hidden), _extra(c)) for c in book.categories.values()])
    cur.executemany("INSERT OR REPLACE INTO category_budgets VALUES (?,?,?)", [
        (c.key, month, amount) for c in book.categories.values() for month, amount in c.budget.items()])
    cur.executemany("INSERT OR REPLACE INTO tags VALUES (?,?,?)", [
        (t.key, t.name, _extra(t)) for t in book.tags.values()])
    cur.executemany(
        "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (r.key, r.pos, r.match_field, F.RULE_FIELD_NAMES.get(r.match_field, str(r.match_field)),
             r.search, r.notes, r.flags, _b(r.flags, F.ASGF_REGEX), _b(r.flags, F.ASGF_EXACT),
             _b(r.flags, F.ASGF_AMOUNT), _b(r.flags, F.ASGF_DOPAY), _b(r.flags, F.ASGF_DOCAT),
             _b(r.flags, F.ASGF_DOMOD), _b(r.flags, F.ASGF_DOTAG), _b(r.flags, F.ASGF_OVWPAY),
             _b(r.flags, F.ASGF_OVWCAT), _b(r.flags, F.ASGF_OVWMOD), _b(r.flags, F.ASGF_OVWTAG),
             r.payee_key, r.category_key, r.paymode, r.amount, " ".join(r.tags), _extra(r))
            for r in book.rules])
    cur.executemany("INSERT OR REPLACE INTO templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (t.key, t.account_key, t.amount, t.paymode, t.flags, t.payee_key, t.category_key, t.memo,
         t.info, " ".join(t.tags), t.dst_account_key, t.rec_flags,
         _iso(t.next_julian) if t.next_julian else None, t.every, t.unit, t.limit,
         json.dumps([{"category_key": s.category_key, "amount": s.amount, "memo": s.memo}
                     for s in t.splits], ensure_ascii=False) if t.splits else None,
         _extra(t)) for t in book.templates])

    tag_keys = {t.name: t.key for t in book.tags.values()}
    txn_rows, split_rows, tag_rows = [], [], []
    for t in book.transactions:
        tid = t.index + 1
        currency = book.currency_for_account(t.account_key)
        digits = currency.frac_digits if currency else 2
        d = t.date
        txn_rows.append((
            tid, d.isoformat() if d else None, t.date_julian, d.year if d else None, d.month if d else None,
            t.account_key, round(t.amount, digits), t.paymode, F.PAYMODE_NAMES.get(t.paymode, str(t.paymode)),
            t.status, F.STATUS_NAMES.get(t.status, str(t.status)), t.flags, int(t.is_income), int(t.is_split),
            int(t.is_internal_transfer), int(t.is_void), int(t.is_remind), t.payee_key, t.category_key,
            t.memo, t.info, " ".join(t.tags), t.kxfer, t.dst_account_key,
            round(t.xfer_amount, 8) if t.flags & F.OF_ADVXFER else None, _extra(t),
        ))
        for i, s in enumerate(t.splits, start=1):
            split_rows.append((tid, i, s.category_key, round(s.amount, digits), s.memo))
        for name in t.tags:
            tag_rows.append((tid, tag_keys.get(name), name))
    cur.executemany(f"INSERT INTO transactions VALUES ({','.join('?' * 26)})", txn_rows)
    cur.executemany("INSERT INTO splits VALUES (?,?,?,?,?)", split_rows)
    cur.executemany("INSERT INTO transaction_tags VALUES (?,?,?)", tag_rows)
    cur.executemany("INSERT OR REPLACE INTO filters VALUES (?,?,?,?)", [
        (f.key, f.name, json.dumps(f.groups, ensure_ascii=False), _extra(f)) for f in book.filters])
    cur.executemany("INSERT INTO unknown_elements VALUES (?,?,?,?)", [
        (i, el.name, el.line, json.dumps(el.attrs, ensure_ascii=False))
        for i, el in enumerate(book.unknown_elements, start=1)])
    for sql in (
        "CREATE INDEX ix_transactions_date ON transactions(date)",
        "CREATE INDEX ix_transactions_category ON transactions(category_key)",
        "CREATE INDEX ix_transactions_account ON transactions(account_key)",
        "CREATE INDEX ix_splits_category ON splits(category_key)",
    ):
        cur.execute(sql)
    conn.commit()
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name, _ in TABLES}


def export_sqlite(book: Book, path: str | Path) -> dict[str, int]:
    """Write a fresh database to `path`, replacing any previous export there."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        counts = build_database(book, conn)
    finally:
        conn.close()
    try:
        os.replace(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise HbkitError(
            "the database could not be replaced; close it in any other program and try again"
        ) from None
    return counts
