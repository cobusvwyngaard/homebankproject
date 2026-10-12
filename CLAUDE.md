# CLAUDE.md — HomeBank analysis toolkit

A local Python toolkit that reads a HomeBank `.xhb` file, exports it to SQLite,
runs pluggable analyses and writes targeted changes to a *new copy* of the file.
The owner runs it on their real file; Claude builds and tests it and never sees
the real data.

## Privacy rules (non-negotiable)

1. **`data/` is private.** The owner's real `.xhb` lives in `data/`; results go to
   `data/out/`. Both are git-ignored (only the empty `.gitkeep` placeholders are
   tracked). `.claude/settings.json` denies `Read`/`Edit` on `data/` and any
   `Bash`/`PowerShell` command whose text contains `data/`.
2. **Never open, print, list, search, copy, hash or run anything against `data/`.**
   That includes `ls`/`dir`, `cat`, Grep/Glob, `git` commands that would show its
   contents, and running the toolkit with a path under `data/`. **Never run the
   toolkit on the real file — the owner does that.** Test only against synthetic data.
3. **Learn the `.xhb` format only from HomeBank's open-source code** (`src/hb-xml.c`
   and the headers it uses, from the source tarball at gethomebank.org or the
   Launchpad repository) and public documentation — never from the owner's file.
   What has been learnt is recorded in `docs/xhb-format.md`.
4. **Terminal output is aggregate-only by default:** counts, percentages and error
   messages without transaction text, payee/category/account names or amounts.
   Detailed output goes to files under `data/out/`. This is enforced in code (the
   terminal summary type accepts numbers only) and checked by tests that plant
   sentinel strings in synthetic files and assert they never reach stdout/stderr.
   Error messages name the element, attribute and line number, never the value.
   **Exception, approved by the owner:** the browser interface (`hb ui`, `hbkit/ui/`)
   may show the owner's data (memos, names, rule texts), because it is served only to
   127.0.0.1 with a per-start key. It must load nothing from and send nothing to the
   internet (no CDNs, fonts or analytics), insert file text only as text (never
   `innerHTML`), and keep the security checks tested in `tests/test_ui.py`. The
   terminal stays aggregate-only.
5. **Tests use only synthetic fixtures** (from the fixture generator) or HomeBank's
   public `example.xhb`, written to temporary directories.
6. If the owner pastes tool output (e.g. the structure report), use it to adjust
   the code. Never ask for transaction text. If personal data is pasted by
   accident, do not repeat it, store it or commit it.
7. Never commit `.xhb` files (except under `tests/fixtures/`), CSV results or
   SQLite databases. `.gitignore` enforces this as a second line of defence.

### Limits of the technical guard (be honest about these)

Per the Claude Code permissions documentation, `Read`/`Edit` deny rules cover
Claude's built-in file tools and shell file commands Claude Code recognises
(`cat`, `head`, `tail`, `sed`, redirections), but **not** a program that opens
files itself (for example a Python script). The `Bash(*data/*)` rule only matches
commands whose text contains `data/`. Rule 2 above is therefore the real
safeguard. OS-level enforcement is available through the Claude Code sandbox
(`sandbox.filesystem.denyRead`) on macOS, Linux and WSL2 only, not native Windows.

Practical consequence: never put the literal text `data/` in a shell command
(including commit messages — use `git commit -F <file>` if a message must mention it).

## Working agreements

- Write in **South African English** (normalise, analyse, categorise, behaviour,
  colour, licence as a noun) in code comments, CLI text and documentation.
- **Python 3.13, standard library only.** Ask the owner before adding any dependency.
  Approved exception: **Plotly** (with narwhals and packaging), used only by the
  reports in `hbkit/reports/` and imported lazily there. The owner prefers an offline
  install, so the wheels are committed in `vendor/wheels/` (see vendor/README.md) and
  pinned in `requirements.txt`. Report tests are skipped when Plotly is not installed.
- Plan first, show the owner, then build in stages with all tests passing at each stage.
- By default the writer writes a new copy and refuses if the output path is the input.
  **Owner-approved exception:** the "Change this file itself" option (File page,
  `--in-place`) changes the selected file, and only via `XhbEditor.save_in_place`: the
  file on disk must still equal what was read, HomeBank must not be running, a backup goes
  to `data/out/backups/` first, and the file is replaced atomically and read back. Every
  step that writes the HomeBank file must offer both ways (`save_to`, `describe_save`).
- Adding an analysis later should mean one new module in `hbkit/analyses/` plus its tests.

## Commands for development

```
python3.13 -m venv .venv                                  # once
.venv/bin/python -m unittest discover -s tests -t .       # all tests (Windows: .venv\Scripts\python)
.venv/bin/python -m hbkit --help                          # Windows: .\hb --help (hb.cmd)
```

Try commands on a synthetic file only, e.g.
`python -m hbkit generate-sample --output <scratch>/s.xhb`, then
`python -m hbkit run rule-mining --file <scratch>/s.xhb --out <scratch>/out`.

## Layout

```
hbkit/cli.py                 command-line interface (python -m hbkit; hb.cmd on Windows)
hbkit/privacy.py             Summary (numbers only), Terminal (hides file text), output folders
hbkit/errors.py              exceptions with terminal-safe messages
hbkit/xhb/format.py          constants from HomeBank's source
hbkit/xhb/scanner.py         byte-exact, GMarkup-like scanner
hbkit/xhb/reader.py          scanner output -> typed objects (model.py)
hbkit/xhb/serialise.py       HomeBank 5.10.3 save functions, line for line
hbkit/xhb/writer.py          targeted edits to a NEW copy (verified before saving)
hbkit/xhb/structure.py       structure report (no values)
hbkit/export/sqlite.py       SQLite export; schema documented in docs/sqlite-schema.md
hbkit/rules/engine.py        simulator of HomeBank's rule application
hbkit/rules/normalise.py     cleaning of bank descriptions
hbkit/rules/mining.py        rule mining, back-test, existing-rule scores, overlaps
hbkit/rules/fill.py          fill empty categories with chosen rules (HomeBank's order) in a new copy
hbkit/analyses/              one module per analysis (ANALYSIS = ...), auto-discovered
hbkit/reports/               reporting layer: settings.py (TOML), data.py (r_* views, periods,
                             balances), base.py (Report plugins, runner), html.py (page, charts);
                             one module per report (REPORT = ...), auto-discovered
hbkit/imports/               preparing bank files for import: formats.py (QIF, HomeBank CSV: read,
                             add categories to a copy), predict.py (per-account categories, account
                             guess), memory.py (file name -> account record), session.py (month folders),
                             transfers.py (find unlinked transfer pairs, link them in a new copy),
                             pdf.py (PDF text with positions, stdlib only), statement.py (a PDF
                             statement's transaction table -> transactions, balance-chain check),
                             bulk.py (import linked files into the HomeBank file, HomeBank's way)
hbkit/ui/server.py           browser interface: stdlib HTTP server on 127.0.0.1, JSON API, security checks
hbkit/ui/static/             the page (index.html, app.js, style.css); no external resources
hbkit/fixtures/generator.py  synthetic .xhb files for tests and trying things out
hbkit/fixtures/statement_pdf.py  synthetic PDF bank statements (made-up content, three font variants)
tests/                       unittest; fixtures/homebank_example = HomeBank's own example.xhb (GPL)
docs/                        xhb-format.md, import-files.md, sqlite-schema.md, adding-an-analysis.md
```

## Adding a report

Follow docs/adding-a-report.md: one module in `hbkit/reports/` (`REPORT = ...`) plus
tests. Build totals on the shared `r_*` views (docs/sqlite-schema.md) so every report
counts transfers, splits and refunds the same way. Report pages may show the owner's
data (they are files in `data/out/reports/`); the terminal shows counts only. Owner
decisions: transfers to/from excluded accounts are left out entirely; income vs expense
is decided by category type (refunds reduce spending); tax years are labelled "2025/26"
and chosen by the year they end. Ask the owner before starting the "later" reports
(recurring-commitments, monthly-cash-flow with Sankey, data-quality).

## Adding an analysis

Follow docs/adding-an-analysis.md: one module in `hbkit/analyses/` plus
`tests/test_<name>.py`. `tests/test_analyses.py` automatically checks every analysis
for file text reaching the screen. Update README.md (command list) when adding one.

## Status

Built: reader + structure report, writer, SQLite export, analysis framework, rule
mining + apply-rules (with account rules for descriptions whose category depends on the
account, `rule_transactions.csv`, `account_dependent.csv`), browser interface (`hb ui`;
the rule review page shows each rule's transactions and downloads CSVs in the browser),
`INSTRUCTIONS.txt` for people who download the ZIP, the import preparation tool (Imports
page and `prepare-import`: categories per account written into a copy of a bank's QIF or
HomeBank CSV file before import, and bank CSV layouts with a header row and PDF
statements converted to HomeBank CSV; a PDF statement's account number suggests its
account; record of file names and accounts in the data folder's
`import-accounts.json`; bank file formats in docs/import-files.md, learnt from HomeBank's
import code plus the owner's description of their two banks' layouts — never put real
statement lines in tests or docs, nor any value (amount, date, reference, number) copied
from a file the owner uploaded, not even as an example; check new fixtures against the
upload before committing), importing a month's linked files into the HomeBank file in one
go (Imports page "Import into HomeBank" and `import-files`; as HomeBank's import: payees
by name and added when new, categories never created, rules on and payee defaults off by
default, duplicates against the account (one-to-one, HomeBank's date tolerance) and
across files left out unless ticked), the option to change the selected file itself
instead of writing copies (File page, `--in-place`; remembered in the data folder's
`ui-settings.json`), transfer linking after import (Transfers page and
`link-transfers`; owner decisions: 4 days apart by default, pairs start unticked with a
select-all, transactions without another side are only listed, never created), filling
empty categories with chosen rules (Fill categories page and `fill-categories`; only empty
categories in open accounts, rules unticked with a select-all, only the category attribute
is written), and the
reporting layer (settings,
`r_*` views, report framework) with three reports: annual-overview, category-trends,
tax-year-statement (also on the Reports page of the browser interface). The owner is a
Windows user (HomeBank 5.8.5 file, 27k transactions, 46 accounts, 163 categories);
descriptions are in the memo field; merged merchant-level rules are on.
Browser checks of the interface and of report pages can be done with the globally
installed Playwright for Node against synthetic files only.
