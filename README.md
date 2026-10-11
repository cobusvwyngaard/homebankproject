# HomeBank analysis toolkit (hbkit)

A small program that runs on your own computer and analyses your HomeBank file
(`.xhb`). It can:

* show the **structure** of your file (versions and counts only, no values);
* **export** your file to a SQLite database you can query;
* run **analyses** — today: an overview, and **assignment-rule mining**, which suggests
  HomeBank rules from how you categorised your transactions in the past;
* add the rules you approve to a **new copy** of your file. Your original file is
  never changed;
* make **reports** — an annual overview, category trends (with inflation) and a tax-year
  statement — as pages that open in your browser and work offline.

HomeBank stays your system of record: for analyses and reports the toolkit only *reads*
your file.

You can use it in your **web browser** (easiest) or by typing commands in PowerShell.
It needs only Python. The reports also use one chart library (Plotly), which is included
in the project and installs without internet.

**Just downloaded the ZIP?** Follow [INSTRUCTIONS.txt](INSTRUCTIONS.txt) in this folder:
the set-up below in short form, plus what to do when something goes wrong.

---

## Your privacy

* Everything runs on your computer. Nothing is sent anywhere.
* **The browser interface shows your own data** (memos, categories, rules) so that you
  can review rules. It is served by the toolkit itself, only to your own computer
  (address `127.0.0.1`), protected by a secret key that changes every time it starts, and
  it loads nothing from the internet.
* **In PowerShell you only see counts and percentages** — never memos, payee, category
  or account names, or amounts. (One exception you have to ask for: the option
  `--show-categories` of rule mining shows your category names.)
* **Detailed results go into the `data\out` folder**, as CSV files you can open in Excel,
  and reports go into `data\out\reports`. Report pages contain your data and open from
  your own disk; they load nothing from the internet.
* The `data` folder is excluded from Git, so your file and results are never uploaded
  to GitHub, even by accident.
* Claude (the AI that built this toolkit) never sees your file. It built and tested
  everything with made-up data, and the project settings forbid it from reading the
  `data` folder. **Never paste transaction details into a chat.** The output of the
  `structure` command is safe to paste: it contains no values.

---

## Setting up (once)

These steps are for Windows and do not need administrator rights.

### 1. Check (or install) Python 3.13

Open PowerShell (Start menu → type `PowerShell`) and run:

```powershell
python --version
```

If it prints `Python 3.13.…`, go to step 2. If not, install Python 3.13 for your user
only (no admin rights needed), following the official
[Python on Windows](https://docs.python.org/3/using/windows.html) guide:

* **Python install manager** (what python.org now recommends): install it from
  <https://www.python.org/downloads/> or from the Microsoft Store, then run
  `py install 3.13`; or
* the classic **Python 3.13 installer** from python.org: on its first screen untick
  *Use admin privileges when installing py.exe*, tick *Add python.exe to PATH*, then
  choose *Install Now*.

Then open a new PowerShell window and check again. If `python --version` shows another
version but `py -3.13 --version` shows 3.13, use `py -3.13` wherever this guide says
`python`.

### 2. Get the toolkit

Either:

* **GitHub Desktop** (easiest for updates, installs without admin rights): *File →
  Clone repository* → `cobusvwyngaard/homebankproject`, then choose the branch that holds
  the toolkit; or
* on the GitHub website: open the repository, choose the branch, then *Code →
  Download ZIP*, and unzip it, e.g. to `Documents\homebankproject`. Windows often puts
  the project in a folder inside a folder of the same name: the project folder is the
  one that contains `INSTRUCTIONS.txt` and `hb-ui.cmd`. Rename or move it now if you
  want to, because the environment of step 3 stops working when the folder is moved
  (if that happens, delete `.venv` and repeat steps 3 and 5).

### 3. Create the project's Python environment

In PowerShell, go to the project folder and create the environment (a private copy
of Python for this project, in the folder `.venv`):

```powershell
cd $HOME\Documents\homebankproject
python -m venv .venv
```

Check that everything works by running the tests (about a minute):

```powershell
.venv\Scripts\python -m unittest discover -s tests -t .
```

The last line should say `OK`.

### 4. Put your HomeBank file in the `data` folder

Close HomeBank, then **copy** (do not move) your `.xhb` file into the `data` folder of
the project. If the `data` folder holds exactly one `.xhb` file, every command uses it
automatically. Otherwise, name the file with `--file "path\to\file.xhb"`.

When you have made new entries in HomeBank, copy the file again before running analyses.

### 5. Install the report library (once, no internet needed)

The reports draw their charts with Plotly. Its install files are included in the
project (folder `vendor\wheels`), so this works offline:

```powershell
.venv\Scripts\python -m pip install --no-index --find-links vendor\wheels -r requirements.txt
```

Run the tests again afterwards; the report tests, which were skipped before, now run too.

---

## The browser interface (easiest)

**Double-click `hb-ui.cmd`** in the project folder, or run this in PowerShell from the
project folder:

```powershell
.\hb ui
```

Your browser then shows the toolkit. The black window (or your PowerShell window) must
stay open while you use it — it *is* the toolkit. If the browser does not open by itself,
copy the address shown in that window (`http://127.0.0.1:…/#token=…`) into it. To stop,
close the window or press Ctrl+C in it.

The pages, from left to right:

1. **File** — choose your HomeBank file (the one in the `data` folder is chosen for you)
   and check that the toolkit understands it.
2. **Analyse** — run an analysis, change its options under *Options*, and see the summary.
3. **Rule review** — the suggested rules in a table. Tick the ones you want, change the
   text or category if you like (the category box suggests your categories as you type),
   filter and sort, then click **Add approved rules to a new copy…**. *Save approvals*
   keeps your ticks for later.
   * **View** shows every transaction a rule matches, so you can check that they belong
     together: grouped by description (card numbers, dates and references removed), or
     one by one, with how many have the rule's category, another category (highlighted)
     or none yet. You can change the text there and click *Show matches* to see what the
     new text would catch. *Previous rule* / *Next rule* step through the rules as shown.
   * **Download shown (CSV)** saves the rules as currently filtered, with your ticks and
     edits, to your browser's Downloads folder (same columns as `review.csv`, so it also
     works with `apply-rules`). Each *View* window can download its transactions too.
     Downloaded files contain your data like everything in `data\out`; keep them private.
   * Other tabs show your existing rules with their scores, inconsistent descriptions,
     descriptions whose category **depends on the account**, overlapping rules, the
     back-test and the suggestions that were set aside.
4. **Reports** — make a report (choose the years and options), then *Open report*. It
   also shows which settings file is used, and lists the reports you made before.
5. **Results** — every result file of every analysis run, as sortable tables.
6. **Export** — create the SQLite database.

*Show folder* buttons open the results folder in Windows Explorer.

The browser interface and the commands below do exactly the same work and write to
the same `data\out` folder, so you can mix them.

---

## Running commands

All commands are run from the project folder in PowerShell, using the launcher `hb`:

```powershell
.\hb <command> [options]
```

(`.\hb` is short for `.venv\Scripts\python -m hbkit`.) Add `--help` to any command to
see its options, e.g. `.\hb run rule-mining --help`.

### Try it on made-up data first (optional)

```powershell
.\hb generate-sample
.\hb structure --file data\out\sample.xhb
.\hb run rule-mining --file data\out\sample.xhb
```

With two made-up home loans whose transactions are categorised by account (to see
account rules):

```powershell
.\hb generate-sample --home-loans --output data\out\sample-loans.xhb
.\hb run rule-mining --file data\out\sample-loans.xhb
```

And for the reports (a fake file with rental and medical data, plus a fake CPI table and
a sample settings file):

```powershell
.\hb generate-sample --reporting --output data\out\sample-rep.xhb
.\hb report run category-trends --file data\out\sample-rep.xhb --settings data\out\sample-rep-settings.toml
```

### `structure` — check that the toolkit understands your file

```powershell
.\hb structure
```

Shows the HomeBank version that wrote your file, its encoding and line endings, how
many of each element it contains, and how often each attribute is filled in — no
values. **Run this first.** Anything marked `*` is something the toolkit does not
know yet; you can paste the output to Claude so it can adjust the reader.

### `export-sqlite` — your file as a database

```powershell
.\hb export-sqlite
```

Creates `data\out\homebank.sqlite` (each export replaces the previous one). Open it with
[DB Browser for SQLite](https://sqlitebrowser.org) (the "no installer" .zip version runs
without admin rights). Start with the views `v_transactions`, `v_transaction_lines`
(splits expanded) and `v_monthly_category_totals`. The full description, with example
queries, is in [docs/sqlite-schema.md](docs/sqlite-schema.md).

### `ui` — the browser interface

```powershell
.\hb ui                    # options: --file, --out, --port N, --no-browser
```

### `list` and `run` — analyses

```powershell
.\hb list
.\hb run overview
.\hb run rule-mining
```

Each run writes its results to a new folder `data\out\<analysis>\<date_time>\`.

---

## Assignment-rule mining

HomeBank's assignment rules (*Manage → Assignments…*) set a category automatically
when a transaction's memo contains some text. Rule mining looks at your history and
suggests such rules.

### How it works

1. **Cleaning.** Each memo is cleaned: card numbers, dates, times, amounts, references,
   branch codes and other numbers are removed, so that
   `POS PURCHASE WOOLWORTHS SANDTON 458712******1234 12 MAR` becomes the pattern
   `POS PURCHASE WOOLWORTHS SANDTON`.
2. **Candidates.** A pattern that occurs at least **5 times**, with at least **95%** of
   those transactions in one category, becomes a candidate.
3. **Rule text.** HomeBank looks for the rule's text *inside* the memo, so the suggested
   text is the longest part of the pattern that appears in every one of those memos,
   e.g. `WOOLWORTHS SANDTON`. That text is then tested the way HomeBank would use it,
   on all your transactions. It is only suggested if it still reaches 95%, because a
   short text can catch other things too.
4. **Merged rules.** One rule can also cover several patterns, e.g. `WOOLWORTHS` for all
   branches, when that still reaches 95%. These are marked `merged`, and the branch
   rules they cover show it in the `covered_by` column. You choose which ones to approve.
5. **Account rules.** Sometimes the same description gets a different category depending
   on the account — for example interest on two home loans that belong to two rental
   properties. **HomeBank's rules cannot be limited to one account**: a rule only looks
   at the memo (or payee), optionally the amount, and applies to every account. So:
   * Where the descriptions differ in some fixed detail that cleaning removes, such as a
     loan or policy number (`BOND INSURANCE PREMIUM POLICY 4410023`), that text becomes
     an **account rule** (kind `account`): tested on all accounts like any other rule,
     and placed before the general rules so it wins.
   * Where the descriptions are identical (`HOME LOAN INTEREST` in both loan accounts),
     no HomeBank rule can tell them apart. These are listed in `account_dependent.csv`
     (and the *Depends on account* tab) so you know to categorise them by hand.
   * A suggested rule that another account contradicts (e.g. `UBER` is ride hailing on
     your own card but business travel on the business card) says so in its notes and
     in the column `other_accounts_differ`.

   "Consistent in an account" uses the same thresholds as everything else: at least 5
   transactions of that description in the account, 95% of them in one category.
6. **Back-test.** To see how well this works, rules are built from all years except the
   latest two calendar years, then tested on those two years: how many transactions would
   get the right category, the wrong one, or none. This is reported for your existing
   rules alone, the new rules alone, and both together (existing rules first, as
   HomeBank would apply them).
7. **Checks.**
   * Descriptions you categorised inconsistently.
   * Rules that match the same transactions. HomeBank applies rules in order, and for
     "set category" rules the first match wins.
   * A score and status for each of your existing rules: OK, weak, never matches,
     always overruled by another rule, not used recently, or broken.

The suggestions in `review.csv` are then built from **all** years.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--min-count N` | 5 | how often a pattern must occur |
| `--min-share S` | 0.95 | share in one category (`0.9` or `90` both mean 90%) |
| `--test-years N` | 2 | latest calendar years used for the back-test |
| `--field memo/payee` | memo | which text to use |
| `--no-merge` | | do not suggest merged rules |
| `--include-transfers` | | also use internal transfers |
| `--min-text-length N` | 4 | shortest rule text, in characters |
| `--show-categories` | | also show the back-test per category on screen (shows category names) |

### Output files (in `data\out\rule-mining\<date_time>\`)

| File | What it holds |
|---|---|
| `review.csv` | the suggested rules — this is the one you work with |
| `rule_transactions.csv` | every transaction each suggested rule matches, with its `result`: same category, other category, or none yet (the rule would fill it) |
| `account_dependent.csv` | descriptions whose category depends on the account, per account, with the account rule suggested or why there is none |
| `rejected_candidates.csv` | consistent patterns that did not become suggestions, and why |
| `backtest.csv`, `backtest_by_category.csv` | the back-test results |
| `existing_rules.csv` | the score and status of each of your existing rules |
| `overlaps.csv` | rules that match the same transactions, and which one wins |
| `inconsistent_patterns.csv` | descriptions you categorised in different ways |
| `summary.json`, `README.txt` | settings of the run, and a short guide |

Useful columns in `review.csv`:
* `transactions_matched` and `correct_pct` — how often the rule text matches and how
  often the category is right;
* `would_fill_uncategorised` — uncategorised transactions it would fill;
* `backtest_*` — how the same rule did in the back-test;
* `conflicts_with_existing_rules` — existing rules that give the same transactions
  another category;
* `kind` and `for_account` — `pattern`, `merged`, or `account` (with the account whose
  transactions the text picks out);
* `other_accounts_differ` — accounts where these transactions usually have another
  category;
* `existing_rules_already_right_pct` — how often your existing rules already get these
  right;
* `examples` — up to three memos.

### Approving rules and adding them to a copy of your file

The easiest way is the **Rule review** page of the browser interface (see above). Without
it:

1. Open `review.csv` in Excel. If everything appears in one column, use
   *Data → From Text/CSV* and choose *comma* as the delimiter.
2. Type `y` in the **approve** column of each rule you want. You may also change
   `rule_text`, or `category` (use the full name as HomeBank shows it, e.g.
   `Transport:Fuel`).
3. Save, keeping the CSV format.
4. Run, using the folder of that run (copy its name from the screen):

   ```powershell
   .\hb apply-rules --review "data\out\rule-mining\2026-10-11_153012"
   ```

   This writes a new file `data\out\with-rules_<date_time>.xhb`. Your original is not
   touched.
   * The new rules are placed after your existing ones: account rules first, then longer
     (more specific) texts before shorter ones.
   * They match "contains, ignoring case" on the memo, and set the category only when
     it is empty.
   * Approved rows are skipped, and counted on screen, when the category no longer
     exists, the same rule already exists, or the row is approved twice.
5. Open the new copy in HomeBank and check the rules under *Manage → Assignments…*. To
   try them, use *Auto. assignments* in an account window.
6. When you are happy: close HomeBank, keep a backup of your original file, then put the
   new copy in its place (rename it to your file's name).

---

## Reports

Reports are single web pages (with a CSV file where useful) saved in
`data\out\reports\`. Every report shows at the top its parameters, the year mode, the
date range of the data in your file, the settings used, and when it was made.

Make them on the *Reports* page of the browser interface, or with commands:

```powershell
.\hb report list
.\hb report run annual-overview                      # all years
.\hb report run annual-overview --from 2022 --to 2025
.\hb report run category-trends --year 2025
.\hb report run tax-year-statement --year 2026       # tax year 2025/26
.\hb report run tax-year-statement --year 2026 --groups "tax: medical"
```

### Settings: years, accounts, groups and inflation

The reports read their settings from **`data\settings.toml`** (private, like your HomeBank
file). To start, copy the example and edit it in Notepad:

```powershell
Copy-Item settings.example.toml data\settings.toml
notepad data\settings.toml
```

The example file explains every setting. In short:

* **`[period] year_mode`** — `"calendar"` (January–December) or `"tax"` (the SA tax year,
  1 March to the end of February). A tax year is shown as `2025/26` and chosen with
  `--year 2026`, the year in which it ends (SARS's "2026 year of assessment").
* **`[accounts] exclude`** — accounts to leave out of every report, by name. Transfers to
  or from them are left out too.
* **`[groups."…"]`** — category groups, each with `categories` (a top-level category
  includes its subcategories) and/or `tags`. Groups whose name starts with `tax:` make up
  the tax-year statement; all groups appear in category trends.
* **`[inflation] cpi_file`** — optional, for amounts in today's prices: a CSV file with
  two columns, `month` and `index`, e.g.

  ```
  month,index
  2024-01,113.0
  2024-02,114.2
  ```

  Use the headline consumer price index published monthly by Stats SA
  (<https://www.statssa.gov.za>, publication P0141, *Consumer Price Index*); copy the
  monthly index values into Excel with these two columns and save as CSV. Only the ratios
  between months matter, so the index's base year does not. Put the file next to the
  settings file (e.g. `data\cpi.csv`) and write `cpi_file = "cpi.csv"`.
* **`[reports] min_yearly_spend`** — category trends only flags categories that spend at
  least this much a year on average (default R1 000).

If a name in the settings does not exist in your HomeBank file, the report still runs; it
counts the problem on screen and lists it at the top of the report.

### How amounts are counted (the same in every report)

* **Split transactions** are counted per split line, each in its own category.
* **Transfers between your own accounts** are neither income nor expense. Transfers to or
  from an excluded account are left out entirely.
* **Income** is money in an income category (and uncategorised money in). Everything else
  is an **expense**, so a refund in an expense category reduces that category's spending.
* **Void transactions** and **excluded accounts** are left out.
* A year that is not complete in your file is marked **(partial)**.

### annual-overview

Per year: income, expenses, net savings and savings rate (tiles for the latest year,
charts, and a table); the total balance of your accounts over time, the larger accounts
one by one, and every account's balance at each year end; and the top 10 expense
categories with the change against the year before. A year still in progress is compared
with the same months of the year before. Option: `--top N`.

### category-trends

Spending per category (and per category group) for every year: a stacked chart, a chart
where you pick one category or group, and tables (subcategories can be shown). With a CPI
table, a switch shows amounts in today's prices, and categories growing faster than
inflation are highlighted — average yearly growth between the first and last complete
years, at least 1 percentage point a year above inflation (change with
`--inflation-margin`). Without a CPI table the amounts are nominal and nothing is flagged.

### tax-year-statement

One tax year (default: the latest that has ended), always in tax years whatever the year
mode. For each `tax:` group (or the groups given with `--groups`): a summary by category,
then every transaction — date, payee, memo, category, amount — with money in, money out
and net subtotals. A summary of all groups comes first. It prints neatly on A4 (each group
on a new page; use the *Print* button or Ctrl+P, and "Save as PDF" if you want a PDF), and
the same content is saved as a CSV file next to it. The statement warns when your file
does not cover the whole tax year yet.

### Asking Claude for a new report

Like analyses, a report is one new file plus its tests. Describe what you want to see,
the charts and tables, the years (one year or a range) and any options. For example:

> Add a report "monthly-cash-flow" to the HomeBank toolkit: for a chosen year, money in
> and out per month as bars, and a Sankey diagram from income categories to expense
> category groups. Option: show the top N categories separately.

Reports already discussed for later: **recurring-commitments**, **monthly-cash-flow**
(with Sankey) and **data-quality**. Claude builds and tests them with made-up data only;
you then update your copy and run them on your own file.

---

## Asking Claude for a new analysis

The toolkit is built so that a new analysis is one new file plus its tests. To ask for
one, describe:

* **what you want to know**, e.g. *"Which categories grew most compared with last year?"*;
* **what you want on screen** (only counts and percentages are allowed there);
* **what you want in the CSV** in `data\out`;
* any **options** you would like to change when running it.

For example:

> Add an analysis "category-trends" to the HomeBank toolkit: for each top-level category,
> compare spending in the last 12 months with the 12 months before. On screen: how many
> categories went up or down by more than 10%. In a CSV: category, both totals, the change
> in rand and in %. Option: the number of months to compare.

Claude builds it and tests it with made-up data, then pushes it to GitHub. You update
your copy (*Fetch/Pull* in GitHub Desktop, or download again — your `data` folder is
kept either way) and run `.\hb run category-trends` on your own file. Technical details
are in [docs/adding-an-analysis.md](docs/adding-an-analysis.md).

---

## Troubleshooting

| Message | What to do |
|---|---|
| `no .xhb file found in the data folder` | Copy your HomeBank file into `data`, or use `--file`. |
| `… .xhb files found in the data folder` | Keep one file there, or choose one with `--file`. |
| `the database could not be replaced` | Close the database in DB Browser (or any other program) and run again. |
| `Unexpected error (…)` | The details were written to `data\out\errors\`. They may contain text from your file, so do not paste them anywhere. Tell Claude the error type and what you ran instead. |
| `python` (or `py`) is not recognised, or the Microsoft Store opens | Python is not installed or not on the PATH; install Python 3.13 as in step 1, then open a new PowerShell window. |
| `The toolkit's Python environment was not found in this folder` | Do step 3 in the folder that contains `hb-ui.cmd` (also after moving or renaming the folder: delete `.venv` first). |
| `.\hb` does nothing / `.venv` missing | Do step 3 of the set-up again. |
| The browser says "missing or wrong key" | Use the full address shown in the black window (it ends in `#token=…`); the key changes each time the toolkit starts. |
| The browser page stops responding | The black window was closed. Start `hb-ui.cmd` again. |
| `the reports need Plotly, which is not installed yet` | Do step 5 of the set-up (no internet needed). |
| `the settings file is not valid TOML (… line N …)` | Check that line in `data\settings.toml`: text in "double quotes", lists in [ ], one setting per line. |
| `the CPI file named in the settings was not found` | Put the CSV next to `settings.toml`, or give its full path. |
| A report says some names were not found | Check the spelling against HomeBank (capitals do not matter); see the list at the top of the report. |

---

## For the technically curious

* Python 3.13, standard library only; tests: `.venv\Scripts\python -m unittest discover -s tests -t .`
* The `.xhb` format as learnt from HomeBank's source code: [docs/xhb-format.md](docs/xhb-format.md)
* The SQLite tables and views, including the shared report views (`r_lines`, `r_flows`,
  `r_group_lines`, `r_category_period`, `r_period_totals`): [docs/sqlite-schema.md](docs/sqlite-schema.md).
  With a settings file, `export-sqlite` adds those report views to `homebank.sqlite` too.
* How to add an analysis or a report: [docs/adding-an-analysis.md](docs/adding-an-analysis.md),
  [docs/adding-a-report.md](docs/adding-a-report.md)
* The reader copies the original file's bytes exactly, and the writer splices changes
  into a copy. An unchanged file therefore comes back byte for byte, and an edited copy
  differs only in the intended lines; both are checked by the tests and again before
  every save.
* Project rules for Claude, including the privacy rules: [CLAUDE.md](CLAUDE.md)
