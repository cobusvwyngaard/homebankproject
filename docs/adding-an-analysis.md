# Adding an analysis

An analysis is **one module in `hbkit/analyses/` plus its tests**. It is found
automatically, appears in `python -m hbkit list` and on the *Analyse* page of the
browser interface (with a form for its parameters), runs with
`python -m hbkit run <name>`, and its CSV files can be browsed on the *Results* page.

## The interface (`hbkit/analyses/base.py`)

```python
from typing import Any

from ..privacy import Summary
from .base import Analysis, Context, Param, pct, write_csv


class CategoryTrends(Analysis):
    name = "category-trends"                       # command-line name
    description = "Compare spending per category with the year before."
    params = (                                     # become --months etc.
        Param("months", int, 12, "length of each period in months"),
        Param("include_transfers", bool, False, "count internal transfers too"),
    )

    def run(self, ctx: Context, params: dict[str, Any]) -> Summary:
        book = ctx.book                            # hbkit.xhb.model.Book
        folder = ctx.new_run_dir()                 # data/out/category-trends/<date_time>/
        rows = [...]
        write_csv(folder / "trends.csv", ["category", "before", "after", "change_pct"], rows)

        s = Summary("Category trends")
        s.count("Categories compared", len(rows))  # numbers only
        s.percent("Went up by more than 10%", up, len(rows))
        s.output("Details", folder)
        return s


ANALYSIS = CategoryTrends()
```

* `params`: `int`, `float`, `str` (optionally with `choices`) or `bool`
  (gives `--name` / `--no-name`).
* `Context` gives the parsed `book`, the input path and the output root.
* Extra top-level commands (like rule mining's `apply-rules`) go in `commands`.

## Privacy rules an analysis must follow

1. **Never print.** Return a `Summary`. It only accepts numbers (`count`, `percent`,
   `number`), fixed labels written in the code, notes written in the code, and
   output paths. Never put file text (memos, names, rule texts) in a label or note.
2. Write details with `write_csv` / `write_json` into `ctx.new_run_dir()`. Use
   `summary.note(..., terminal_only=True)` for hints that only make sense in PowerShell.
3. Raise `hbkit.errors.HbkitError` with a message that contains no file values for
   expected problems. Anything else is caught and written to a log file in
   `data/out/errors/`.
4. As a second guard, the terminal hides any line that contains free text from the
   file. If an option deliberately shows names (like `--show-categories`), call
   `summary.permit(names)` for exactly those names.

## Useful building blocks

* `hbkit.xhb.model.Book`: `transactions` (with `.date`, `.amount`, `.category_key`,
  `.splits`, `.is_internal_transfer`, `.is_void`, …), `category_path(key)`,
  `payee_name(key)`, `account_name(key)`, `rules_in_evaluation_order()`.
* `hbkit.rules.engine`: simulate HomeBank's assignment rules.
* `hbkit.rules.normalise.normalise(text)`: clean a bank description.
* `hbkit.export.sqlite.build_database(book, conn)`: an in-memory SQLite copy, if SQL
  is easier: `conn = sqlite3.connect(":memory:")`.
* `hbkit.xhb.writer.XhbEditor`: targeted changes written to a new copy of the file
  (`save`), or into the file itself after a backup when the owner chose so
  (`save_to(None, input_path, backup_dir)`; report the result with `describe_save`).

## Tests

Add `tests/test_<name>.py`. Use synthetic files only:

```python
from .helpers import TempDirTestCase, run_cli, sample

class CategoryTrendsTests(TempDirTestCase):
    def test_runs(self):
        path = self.write_sample()                 # synthetic .xhb in a temp folder
        code, out, err = run_cli("run", "category-trends", "--file", str(path), "--out", str(self.tmp))
        self.assertEqual(code, 0, err)
```

`tests/test_analyses.py` automatically runs **every** analysis with its default
options on a file in which all free text is marked, and fails if any of it reaches
the screen, or if the analysis writes no detailed output.

The generator (`hbkit/fixtures/generator.py`) can be extended when an analysis needs
data it does not produce yet (for example budgets or more currencies). Keep it
deterministic.
