# SQLite database schema

`python -m hbkit export-sqlite` turns your HomeBank file into a SQLite database
(default: `data/out/homebank.sqlite`). Each export replaces the previous one; your
`.xhb` file is only read. Open the database with any SQLite tool, for example
[DB Browser for SQLite](https://sqlitebrowser.org) (it installs without admin rights
as the "no installer" .zip version), or query it from Python.

Conventions:

* **Amounts are signed** exactly as in HomeBank: negative = money out, positive =
  money in. They are rounded to the currency's number of decimals.
* **Dates** are ISO text `YYYY-MM-DD`, so they sort and compare correctly.
* `key` columns are HomeBank's own internal numbers; `*_key` columns refer to them
  (0 or NULL = none).
* Yes/no columns (`is_…`, `sets_…`, `overwrites_…`) hold 1 or 0.
* `extra_json` holds attributes this toolkit does not know (from other HomeBank
  versions) as JSON, so nothing is lost. Usually empty.
* `PRAGMA user_version` holds the schema version (currently 1).

The **views** (names starting `v_`) are the easiest place to start.

## Views

### v_transactions
One row per transaction, with names instead of keys.
Columns: `id`, `date`, `year`, `month`, `year_month` (`YYYY-MM`), `account`,
`currency` (ISO code), `amount`, `type` (`expense`, `income` or `transfer` — internal
transfers between your own accounts), `payee`, `category` (full path such as
`Transport:Fuel`, or `(split)`), `category_top` (top-level category), `memo`, `info`,
`tags`, `status`, `is_split`, `is_void`, `payee_key`, `category_key`, `account_key`.

### v_transaction_lines
Like `v_transactions`, but **split transactions are expanded**: one row per split
line, with the split's own amount, category and memo (the transaction memo when the
split memo is empty). Use this view for anything per category.
Columns: `transaction_id`, `line_no`, `date`, `year`, `month`, `year_month`,
`account`, `currency`, `amount`, `type`, `payee`, `category_key`, `category`,
`category_top`, `memo`, `is_split_line`, `is_void`.

### v_monthly_category_totals
Monthly totals per category and currency, from `v_transaction_lines`, excluding void
transactions and internal transfers. Uncategorised lines appear as `(uncategorised)`.
Columns: `year_month`, `currency`, `category`, `category_top`, `total`, `lines`.

### v_categories
Columns: `key`, `full_name`, `top_level`, `sub_name` (NULL for a top-level category),
`is_income`, `is_hidden`.

### v_rules
Assignment rules in the order HomeBank applies them.
Columns: `position`, `key`, `field_name`, `search_text`, `is_regex`, `case_sensitive`,
`sets_category_if_empty`, `overwrites_category`, `category`, `payee`, `notes`.

## Tables

### meta
`key`, `value`. Keys: `schema_version`, `toolkit_version`, `exported_at`,
`homebank_file_version`, `homebank_data_version`, `source_sha256` (fingerprint of the
.xhb file the database was made from).

### transactions
One row per `<ope>`, in file order.
`id` (position in the file, from 1), `date`, `date_julian` (HomeBank's day number),
`year`, `month`, `account_key`, `amount`, `paymode`, `paymode_name`, `status`
(0 none, 1 cleared, 2 reconciled, 3 void), `status_name`, `flags` (HomeBank's raw
flags), `is_income`, `is_split`, `is_transfer`, `is_void`, `is_remind`, `payee_key`,
`category_key` (0 for split transactions), `memo`, `info`, `tags` (space-separated
names), `transfer_link` (both sides of an internal transfer share it),
`transfer_account_key` (the other account), `transfer_amount` (only for transfers
between currencies), `extra_json`.

### splits
`transaction_id`, `line_no` (from 1), `category_key`, `amount`, `memo`.

### transaction_tags
`transaction_id`, `tag_key` (NULL if the tag is not in the tag list), `tag_name`.

### accounts
`key`, `name`, `type`, `type_name`, `currency_key`, `flags`, `is_closed`, `position`,
`group_key`, `number`, `bank_name`, `initial_balance`, `minimum_balance`,
`maximum_balance`, `website`, `notes`, `cc_day` (credit card closing day),
`reconciled_date`, `extra_json`.

### account_groups
`key`, `name`, `extra_json`.

### currencies
`key`, `iso`, `name`, `symbol`, `symbol_prefix`, `decimal_char`, `grouping_char`,
`frac_digits`, `rate`, `flags`, `extra_json`.

### payees
`key`, `name`, `default_category_key`, `default_paymode`, `flags`, `notes`, `extra_json`.

### categories
`key`, `parent_key` (0 for top level), `name`, `full_name` (`Parent:Child`), `flags`,
`is_income`, `is_hidden`, `extra_json`.

### category_budgets
`category_key`, `month` (0 = the same budget every month, 1–12 = that month), `amount`.

### tags
`key`, `name`, `extra_json`.

### rules
Assignment rules (`<asg>`); see docs/xhb-format.md for how HomeBank applies them.
`key`, `position`, `field` (0 memo, 1 payee), `field_name`, `search_text`, `notes`,
`flags`, `is_regex`, `case_sensitive`, `matches_amount`, `sets_payee_if_empty`,
`sets_category_if_empty`, `sets_paymode_if_empty`, `sets_tags_if_empty`,
`overwrites_payee`, `overwrites_category`, `overwrites_paymode`, `overwrites_tags`,
`payee_key`, `category_key`, `paymode`, `amount`, `tags`, `extra_json`.

### templates
Templates and scheduled transactions (`<fav>`).
`key`, `account_key`, `amount`, `paymode`, `flags`, `payee_key`, `category_key`,
`memo`, `info`, `tags`, `transfer_account_key`, `recurrence_flags`, `next_date`,
`every`, `unit`, `recurrence_limit`, `splits_json`, `extra_json`.

### filters
Saved filters. `key`, `name`, `groups_json` (HomeBank's filter settings as text),
`extra_json`.

### unknown_elements
Elements this toolkit does not know, kept for completeness.
`id`, `element`, `line`, `attributes_json`.

## Example queries

Spending per top-level category per year:

```sql
SELECT year, category_top, ROUND(SUM(amount), 2) AS total
FROM v_transaction_lines
WHERE type = 'expense' AND is_void = 0
GROUP BY year, category_top
ORDER BY year, total;
```

Uncategorised transactions, newest first:

```sql
SELECT date, account, amount, memo
FROM v_transactions
WHERE category IS NULL AND type <> 'transfer' AND is_void = 0
ORDER BY date DESC;
```

Monthly totals for one category:

```sql
SELECT year_month, total
FROM v_monthly_category_totals
WHERE category = 'Groceries'
ORDER BY year_month;
```
