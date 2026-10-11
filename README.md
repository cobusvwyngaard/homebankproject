# HomeBank analysis toolkit (hbkit)

A small program that runs on your own computer and analyses your HomeBank file
(`.xhb`). It can:

* show the **structure** of your file (versions and counts only, no values);
* **export** your file to a SQLite database you can query;
* run **analyses** — today: an overview, and **assignment-rule mining**, which suggests
  HomeBank rules from how you categorised your transactions in the past;
* add the rules you approve to a **new copy** of your file. Your original file is
  never changed.

It needs only Python; nothing else is installed.

---

## Your privacy

* Everything runs on your computer. Nothing is sent anywhere.
* **On screen you only see counts and percentages** — never memos, payee, category
  or account names, or amounts. (One exception you have to ask for: the option
  `--show-categories` of rule mining shows your category names.)
* **Detailed results go into the `data\out` folder**, as CSV files you can open in Excel.
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
py -3.13 --version
```

If it prints `Python 3.13.…`, go to step 2. If not, install Python 3.13 for your user
only (no admin rights needed), following the official
[Python on Windows](https://docs.python.org/3/using/windows.html) guide:

* **Python install manager** (what python.org now recommends): install it from
  <https://www.python.org/downloads/> or from the Microsoft Store, then run
  `py install 3.13`; or
* the classic **Python 3.13 installer** from python.org, choosing *Install Now*.

Then run `py -3.13 --version` again. (If `py` is not recognised but `python --version`
shows 3.13, use `python` wherever this guide says `py -3.13`.)

### 2. Get the toolkit

Either:

* **GitHub Desktop** (easiest for updates, installs without admin rights): *File →
  Clone repository* → `cobusvwyngaard/homebankproject`, then choose the branch that holds
  the toolkit; or
* on the GitHub website: open the repository, choose the branch, then *Code →
  Download ZIP*, and unzip it, e.g. to `Documents\homebankproject`.

### 3. Create the project's Python environment

In PowerShell, go to the project folder and create the environment (a private copy
of Python for this project, in the folder `.venv`):

```powershell
cd $HOME\Documents\homebankproject
py -3.13 -m venv .venv
```

Check that everything works by running the tests (about 20 seconds):

```powershell
.venv\Scripts\python -m unittest discover -s tests -t .
```

The last line should say `OK`.

### 4. Put your HomeBank file in the `data` folder

Close HomeBank, then **copy** (do not move) your `.xhb` file into the `data` folder of
the project. If the `data` folder holds exactly one `.xhb` file, every command uses it
automatically. Otherwise, name the file with `--file "path\to\file.xhb"`.

When you have made new entries in HomeBank, copy the file again before running analyses.

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
5. **Back-test.** To see how well this works, rules are built from all years except the
   latest two calendar years, then tested on those two years: how many transactions would
   get the right category, the wrong one, or none. This is reported for your existing
   rules alone, the new rules alone, and both together (existing rules first, as
   HomeBank would apply them).
6. **Checks.**
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
* `existing_rules_already_right_pct` — how often your existing rules already get these
  right;
* `examples` — up to three memos.

### Approving rules and adding them to a copy of your file

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
   * The new rules are placed after your existing ones, longer (more specific) texts
     first.
   * They match "contains, ignoring case" on the memo, and set the category only when
     it is empty.
   * Approved rows are skipped, and counted on screen, when the category no longer
     exists, the same rule already exists, or the row is approved twice.
5. Open the new copy in HomeBank and check the rules under *Manage → Assignments…*. To
   try them, use *Auto. assignments* in an account window.
6. When you are happy: close HomeBank, keep a backup of your original file, then put the
   new copy in its place (rename it to your file's name).

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
| `py` is not recognised | Python is not installed, or was installed without the launcher; reinstall Python 3.13 as above. |
| `.\hb` does nothing / `.venv` missing | Do step 3 of the set-up again. |

---

## For the technically curious

* Python 3.13, standard library only; tests: `.venv\Scripts\python -m unittest discover -s tests -t .`
* The `.xhb` format as learnt from HomeBank's source code: [docs/xhb-format.md](docs/xhb-format.md)
* The reader copies the original file's bytes exactly, and the writer splices changes
  into a copy. An unchanged file therefore comes back byte for byte, and an edited copy
  differs only in the intended lines; both are checked by the tests and again before
  every save.
* Project rules for Claude, including the privacy rules: [CLAUDE.md](CLAUDE.md)
