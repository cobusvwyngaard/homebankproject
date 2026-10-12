# Bank files for import

What the toolkit knows about the files HomeBank imports, and what it does with them.
HomeBank's behaviour comes from its source code (`hb-import.c`, `hb-import-qif.c`,
`hb-import-csv.c`, `hb-encoding.c`, 5.10.3), the PDF format from the PDF specification
(ISO 32000). The owner's banks' layouts were described from example files; only their
structure is recorded here.

## Formats read

### QIF

```
!Type:Bank
D24/04/2026
T-123.45
N0
MDESCRIPTION OF THE TRANSACTION
^
```

* A header line `!Type:` names the kind of account: `Bank`, `Cash`, `CCard`, `Oth A`,
  `Oth L` hold transactions; other types (`Cat`, `Class`, `Memorized`, ...) are lists
  and are skipped. `!Account` blocks describe accounts, not transactions.
* Each transaction ends with `^`. Fields by first letter: `D` date, `T` (or `U`)
  amount, `N` number, `P` payee, `M` memo, `L` category, `S`/`E`/`$` split lines.
* `L[Name]` is a transfer to account *Name*. Otherwise HomeBank looks the category up
  by its full name, split on the first `:` (Parent:Sub), and **creates it when it does
  not exist**.
* A last record without `^` is not stored by HomeBank.
* Dates: the order of day and month is taken from the whole file (a first part above 12
  means day first, a second part above 12 means month first); when every date is
  ambiguous, day first (South Africa). Years may have two digits, or follow `'`.

Bank 1 (home loans): `!Type:Bank`, no `!Account` block, dates `DD/MM/YYYY`, `N0` on every
transaction, the description in `M` (often with trailing spaces), no `P` lines.

### HomeBank CSV

Exactly 8 columns, no header needed:

| # | Column | Notes |
|---|---|---|
| 1 | date | order chosen in HomeBank's import; the toolkit detects it as for QIF |
| 2 | payment mode | number; 5 = internal transfer (left without a category) |
| 3 | info | |
| 4 | payee | |
| 5 | memo | |
| 6 | amount | |
| 7 | category | full name, as for QIF `L` |
| 8 | tags | |

* The separator is HomeBank's preference (tab, comma, semicolon — the default — or
  space); the toolkit detects semicolon, comma or tab.
* A field may be enclosed in double quotes, with `""` for a quote inside; separators
  inside quotes do not split. Lines without 8 columns are not imported by HomeBank.

Bank 2 (cheque, savings, card): semicolon, no header, dates `DD-MM-YYYY`, transfers
between own accounts with payment mode 5 and a payee such as "To: …" or "From: …".

### A bank's own CSV layout (converted)

HomeBank cannot import these; the toolkit converts them to HomeBank CSV when preparing.
Recognised when the first non-empty row names the columns (case and spacing ignored):

| Meaning | Column names recognised |
|---|---|
| date (required) | Value Date, Transaction Date, Date |
| description (required) | Description |
| amount (required, signed) | Amount |
| transaction type | Type, Transaction Type |
| payee | Beneficiary or CardHolder, Beneficiary |

Other columns (e.g. Value Time) are not used. The separator (comma, semicolon or tab)
is taken from the header row; fields may be quoted. Rows whose date or amount cannot be
read are left out and reported by line number.

Bank 2 (cheque, savings): comma-separated, a header row, dates `YYYY-MM-DD`, a time
column, a type column, text in double quotes, signed amounts with a decimal point.

The conversion writes, per transaction: date `DD-MM-YYYY`; the payment mode from the
type (first match: debit order 11, transfer or re-direct 4, EFT or payment 8, card or
POS 6, ATM or cash 3, fee or charge 10, deposit 9, otherwise 0); no info; the payee; the
description as memo (spacing collapsed to single spaces); the amount with two decimals;
the category; no tags. Separator `;`, UTF-8 without a byte-order mark (a mark would spoil
the first date), Windows line endings; fields containing `;` or `"` are quoted. Transfers
are not marked as internal transfers: they are linked after the import (Transfers page).

### PDF statements (converted)

HomeBank cannot import PDF files; the toolkit reads the statement's transaction table
and converts it to HomeBank CSV as for a bank CSV (above).

**Reading the text** (`hbkit/imports/pdf.py`, standard library only): the objects of the
file (also in object streams), the page tree, streams compressed with FlateDecode (or
ASCIIHex/ASCII85), and the text operators of each page's content stream with the
graphics state. Characters come from the font's encoding: a ToUnicode map, `/Differences`,
WinAnsi/Standard encoding, or the built-in encoding of an embedded Type 1 font
(`dup <code> /<glyph name> put`); glyph names are turned into characters. Glyph widths
give the end of each text, so right-aligned amounts can be placed under their column.
Glyphs are grouped into lines (same baseline) and the lines into chunks (a gap wider than
about a space separates two chunks).

Refused, with a message: password-protected PDFs (an `/Encrypt` entry), PDFs without a
text layer (scanned pages), and other compressions.

**Finding the table** (`hbkit/imports/statement.py`): a line whose chunks are column
headings, with at least a date, a description and debit/credit (or one amount) column.
Headings recognised (case, "(R)" and the like ignored):

| Meaning | Headings |
|---|---|
| date | Date, Transaction Date, Value Date, Posting Date, Trans Date |
| description | Description, Details, Narrative, Transaction Description/Details |
| reference (→ info) | Tran list no, Reference, Ref, Ref no, Reference no |
| fees | Fees, Fee, Charges, Service fee |
| debit | Debits, Debit, Money out, Withdrawals, Payments |
| credit | Credits, Credit, Money in, Deposits, Receipts |
| amount (signed) | Amount |
| balance | Balance, Running balance |

Two headings printed close together (one chunk) are split. Text cells belong to the
column under whose heading they start; amounts to the column whose heading they end
under (right-aligned). The table continues on every page where the header is repeated,
and ends at a "Closing balance" line, a line in another font size, a line starting left
of the table, or a large gap (a footer).

**Rows**: a line without a date and without amounts continues the description of the
row above. "Opening balance", "Closing balance" and "brought/carried forward" lines are
not transactions. Debits and fees are money out, credits money in; in a single amount
column, a leading or trailing `-` or `Dr` means money out, `Cr` money in. Amounts marked `*`
(charges including VAT) and amounts in the fees column get payment mode 10 (fee); a row
with a fee and another amount gives two transactions, the fee with "(fee)" after the
description. The reference column becomes *info*.

**Check**: each amount must lead from the previous printed balance to the balance
printed on its row, and the opening balance plus all amounts to the closing balance
(within half a cent). Any difference means the layout was misread, so the file is
refused and nothing is imported.

**Account**: digit strings of 6 to 16 digits anywhere in the statement (with spaces or
hyphens between digits removed) are compared with the account numbers in the HomeBank
file (*number* attribute of `<account>`); exactly one match suggests that account.

The owner's example statement: PDF 1.4, compressed content, embedded Type 1 fonts with
their own built-in encodings and no ToUnicode maps; a summary page with the account
number; then a bank-charges table and the transaction table with columns *Tran list no,
Date, Description, Fees (R), Debits (R), Credits (R), Balance (R)*, dates `DD/MM/YYYY`,
amounts right-aligned with thousands separators, charges marked ` *`, an opening balance
line at the top and a closing balance line at the end. The synthetic statements in
`hbkit/fixtures/statement_pdf.py` follow this structure with made-up content, in three
font variants (standard font, embedded font with a scrambled built-in encoding, composite
font with a ToUnicode map).

### Not read (yet)

OFX/QFX, bank CSV layouts with other column names (for example separate debit and
credit columns), password-protected or scanned PDF statements.

## What HomeBank does on import

1. Reads the file as UTF-8 when all of it is valid UTF-8; otherwise detects another
   character set (`homebank_file_getencoding`).
2. Takes the category from the file (`da_cat_append_ifnew_by_fullname`).
3. If "auto from payee" is on: fills an **empty** category with the payee's default
   category (`transaction_auto_all_from_payee`).
4. If "auto assignment" is on: runs the assignment rules (`transaction_auto_assign`):
   "set category" fills only an empty category, "overwrite category" always replaces.

So a category written into the file survives, except against an overwrite rule.

In more detail (`hb_import_option_apply`, `hb_import_convert_txn`, `hb_import_apply` in
hb-import.c; `transaction_add` in hb-transaction.c; defaults from hb-preferences.c):

* **Fields.** QIF: `P` payee and `N` number are stripped of spaces, `M` memo is kept as
  it is (trailing spaces included); with the default options the memo is imported and
  payee and memo are not swapped. HomeBank CSV: payee and category are stripped, memo and
  info kept as they are; payment mode 5 becomes 4. No capitalising by default ("ucfirst"
  off).
* **Payee** (`da_pay_get_by_name`): the stripped name is compared with existing payee
  names with `strcasecmp` (capitals of A–Z ignored); an empty name means no payee. A
  name that is not found becomes a new payee with the next key (highest + 1).
* **Category**: an `L`/column 7 value starting with `[` is a transfer to that account
  (created if it does not exist); otherwise the category by full name, created if new.
* **Flags**: income (2) when the amount is positive (`da_transaction_set_flag`); status
  cleared or reconciled only from a QIF `C` line; "Import as pending" (5.9+) adds flag 512.
* **Closed accounts** are refused by `transaction_add`; it inserts each transaction after
  the account's last one with the same or an earlier date. When HomeBank opens a file it
  sorts every account's transactions by date anyway, so their order in the file does not
  matter.
* **Duplicates** in the import assistant (both are unticked, and can be ticked):
  "similar in the target account" — same account, same amount, date within the date
  tolerance (preference "DayGap", 0 by default), memo not compared
  (`hb_import_gen_txn_check_target_similar`); "similar in the import" — same account,
  date, amount, memo, payee, category and number (`hb_import_gen_txn_check_duplicate`).
* **Automation**: "Enrich with payee default" (off by default) fills an empty category
  and payment mode from the payee; "Run automatic assignment rules" (off by default)
  runs `transaction_auto_assign` on the imported transactions only. For each
  transaction all rules are matched first (memo, or the payee's name), then each
  matching rule in position order sets payee, category, payment mode or tags when empty
  ("set"), or always ("overwrite").

## Importing into the HomeBank file (hbkit/imports/bulk.py)

The toolkit can add a month's linked files to the HomeBank file itself (a new copy, or
the file after a backup). It follows the steps above, with these choices:

* Source per file: the prepared copy when it was made for the file's current link,
  otherwise the original with the suggested categories (`month_sources` in session.py).
* Categories are never created: a name that is not in the file is left empty and noted.
  Tags from a CSV tag column are kept only when they exist.
* QIF transfers (`L[Account]`) and CSV payment-mode-5 lines come in as ordinary
  transactions (no transfer link); they are linked afterwards with link-transfers.
  Split transactions are not imported.
* Duplicates: "already in HomeBank" as HomeBank's target check (same account and amount,
  date within the tolerance), but **one-to-one**: an existing transaction accounts for
  one imported transaction only (the closest date, then the same memo). "Twice in these
  files": same account, date, amount, memo, payee and info in two **different** files;
  unlike HomeBank, repeats within one file are not flagged.
* Automation as HomeBank's, with the options shown (rules on, payee defaults off by
  default). A new payee whose name a rule replaces is not added (HomeBank adds it).
  Cheque numbers are not carried into the account; a QIF `C` line's cleared/reconciled
  status is not read (no status); flags 512 (pending) and 1024 (past) are not set.
* New `<pay>` elements go after the existing payees; new `<ope>` elements after the
  account's last transaction with the same or an earlier date (else after the last
  transaction in the file), in date order. All new elements are formatted by
  serialise.py, and the result is checked element by element before it is saved.
* The import record notes per file when it was imported, how many transactions and into
  which file.

## What the toolkit writes

* Only categories: a new `L` line just before the `^` of a QIF transaction (or the text
  of an empty `L` line), or the 7th CSV field. Every other byte stays as it was:
  line endings, spacing, the header, lines it does not understand.
* In the original's encoding: UTF-8, or Windows-1252 for files that are not valid UTF-8.
  A category that the encoding cannot hold is refused.
* Never into a transfer, a split transaction, or a transaction that already has a
  category. Only categories that exist in the HomeBank file, with HomeBank's spelling.
* In CSV, a category containing the separator or a quote is enclosed in quotes.
* The prepared copy is read back and compared with the original (same transactions,
  only the intended categories added) before it is saved.

## How categories are chosen (hbkit/imports/predict.py)

The description is cleaned as in rule mining (memo, or payee when there is no memo).

1. **This account**: the latest 10 categorised transactions with that cleaned
   description in the account; at least 3, and 80% with one category.
2. **All accounts**: at least 5 transactions, 95% in one category (as for a rule).
3. HomeBank's payee defaults and rules are simulated to show what HomeBank would do.

Possible duplicates: same account, date, amount and exact description (ignoring case
and spacing). The exact text is used because cleaning removes the policy or loan
numbers that tell otherwise identical transactions in two accounts apart.

## Which account a file belongs to

A link you made for this month comes first. Otherwise:

0. For a PDF statement: the account number printed on it, when it equals the number of
   exactly one account in the HomeBank file.
1. The import record (`data/import-accounts.json`): the same file name, then a similar
   one (lower case; dates, month names, copy markers such as " (1)" and "- Copy", and a
   time after a date removed; other numbers kept), from the latest earlier month.
2. The contents, per transaction: 2 points to an account that already has it, 1 shared
   by the accounts with the exact description (else 0.5 shared by those with the cleaned
   description), and 1 shared by the accounts whose latest amount for that description
   is within 10%. A guess is only offered when its average is at least 0.5 and 1.5 times
   the next account's.

Record format:

```json
{"version": 1,
 "expected_accounts": [1, 7],
 "links": [{"month": "2026-04", "file": "statement.qif", "name_key": "statement.qif",
            "account": 7, "account_name": "...", "linked": "2026-05-02T09:15:00",
            "sha256": "...", "transactions": 3, "categorised": 2,
            "prepared": "imports/2026-04/prepared/statement-prepared.qif"}]}
```
