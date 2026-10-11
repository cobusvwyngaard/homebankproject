# Bank files for import

What the toolkit knows about the files HomeBank imports, and what it does with them.
HomeBank's behaviour comes from its source code (`hb-import.c`, `hb-import-qif.c`,
`hb-import-csv.c`, `hb-encoding.c`, 5.10.3). The owner's two banks' layouts were
described from example files; only their structure is recorded here.

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

### Not read (yet)

OFX/QFX, and banks' own CSV layouts. HomeBank itself only imports CSV in the layout above.

## What HomeBank does on import

1. Reads the file as UTF-8 when all of it is valid UTF-8; otherwise detects another
   character set (`homebank_file_getencoding`).
2. Takes the category from the file (`da_cat_append_ifnew_by_fullname`).
3. If "auto from payee" is on: fills an **empty** category with the payee's default
   category (`transaction_auto_all_from_payee`).
4. If "auto assignment" is on: runs the assignment rules (`transaction_auto_assign`):
   "set category" fills only an empty category, "overwrite category" always replaces.

So a category written into the file survives, except against an overwrite rule.

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
