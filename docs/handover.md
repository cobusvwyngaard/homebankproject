# Handover: continuing work on hbkit in a new Claude account

For a new Claude Code session. CLAUDE.md (loaded automatically) holds the rules, the
working agreements and the status; this file adds what earlier sessions knew that is not
written down elsewhere. It contains no personal data, and must never contain any.

## Start here

* Repository: `github.com/cobusvwyngaard/homebankproject`. Branch
  `claude/brave-fermi-x7x407` is the only branch and the repository's default; all work
  is on it. There is no open pull request. Build on this branch (or a branch made from it).
* Set up a fresh container:

  ```
  python3.13 -m venv .venv
  .venv/bin/python -m pip install --no-index --find-links vendor/wheels -r requirements.txt   # Plotly, for the report tests
  .venv/bin/python -m unittest discover -s tests -t .
  ```

  All tests passed at the handover (338; report tests are skipped without Plotly).
* Read in this order: CLAUDE.md, README.md (what the owner sees), docs/xhb-format.md,
  docs/import-files.md, then the code you need.

## Sources used

* HomeBank's format and behaviour come from the HomeBank 5.10.3 source tarball
  (gethomebank.org; also on Launchpad). It is not in the repository; download it to a
  scratch folder when needed. Files used so far: `hb-xml.c` (load/save), `hb-import.c`,
  `hb-import-qif.c`, `hb-import-csv.c`, `ui-assist-import.c` (import), `hb-transaction.c`
  (adding, transfers, flags), `hb-assign.c` (rules), `hb-payee.c`, `hb-preferences.c`
  (defaults), `hb-encoding.c`.
* The PDF reader follows the PDF specification (ISO 32000); standard library only.
* Browser checks: Playwright for Node (installed globally in the cloud container), against
  synthetic files only. Pattern: a small Python script creates
  `hbkit.ui.server.UiState(out_root=..., data_dir=...)` and `make_server(state, 0)`,
  writes `server.url` to a file and serves; a Node script opens that address. Pitfall: the
  navigation button "Imports" also matches selectors like `button:has-text("Import ")`.

## The owner

* Windows user. HomeBank **5.8.5** file (about 27k transactions, 46 accounts,
  163 categories); descriptions are in the memo field. Runs the toolkit locally
  (`hb-ui.cmd` / `.\hb`) and never shares the file. After updating the toolkit, the
  browser interface must be restarted (an API request answers 409 when the code changed
  under a running server).
* Two banks: bank 1 (home loans) exports QIF; bank 2 (cheque, savings, card) exports
  HomeBank CSV, its own CSV layout with a header row, and PDF statements. Only their
  structure is recorded (docs/import-files.md).
* How they want answers: technical and accurate, fact-checked, no flattery; remind them
  that they are talking to an AI if there is any doubt. They approve plans before larger
  features ("yes, build it") and want new options to start unticked with a select-all.

## Owner decisions (beyond CLAUDE.md)

* Imports: the account of a file is suggested from (in order) the account number printed
  on a PDF statement, the remembered file name, then the contents; the user confirms it.
  Categories come from the account's own history first (latest 10, at least 3, 80%),
  then all accounts (at least 5, 95%).
* Bulk import (`hbkit/imports/bulk.py`): behaves as HomeBank's import with its standard
  preferences; rules on and payee defaults off by default; duplicates left out unless
  ticked. Deliberate differences are listed in README ("Importing into HomeBank in one
  go") and docs/import-files.md.
* Changing the file itself: requested by the owner; opt-in on the File page, remembered in
  the data folder's `ui-settings.json`; backup first, refused while HomeBank runs.
* Transfers: 4 days apart by default; pairs start unticked with a select-all; transactions
  without another side are only listed, never created.
* Reports: see CLAUDE.md. The "later" reports (recurring-commitments, monthly-cash-flow
  with Sankey, data-quality) need the owner's go-ahead first.

## Privacy in practice

* The rules in CLAUDE.md apply in full. Never put the text `data/` in a shell command;
  commit with `git commit -F <file>` when a message mentions it.
* In the old account the owner uploaded real sample files (QIF, CSV, a PDF statement) so
  that their layouts could be learnt. They are not in the repository and must not be.
  Twice, real values (an amount, a file name; later a reference number, dates and fees
  from the statement) slipped into tests or fixtures before a commit; both times they were
  caught and replaced before anything was pushed. So: invent fixture values from scratch,
  and before committing, compare new tests, fixtures and docs with any file the owner
  uploaded in that session (report only file:line of overlaps, never the values).
* For a new bank layout, ask for the column headings or a made-up example line, not a
  real statement.

## State and next steps (handover on 2026-10-22)

* Built and tested: everything listed under Status in CLAUDE.md, most recently PDF
  statements, bulk import and the option to change the file itself.
* **Not yet confirmed on the real file:** the bulk import and the in-place option. HomeBank
  cannot run in the container, so no file written by the bulk import has been opened in
  HomeBank yet. New `<ope>` and `<pay>` elements are written in HomeBank 5.10.3's format
  into the owner's 5.8.5 file (the same attributes). Advised first real run: "new copy"
  mode, open the copy in HomeBank, check, then report back. Expect feedback on this first.
* Not supported yet: OFX/QFX; bank CSV layouts with separate debit and credit columns;
  password-protected or scanned PDFs; split transactions and QIF cleared status in the
  bulk import; cheque numbers.
