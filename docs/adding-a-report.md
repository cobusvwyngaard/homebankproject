# Adding a report

A report is **one module in `hbkit/reports/` plus its tests**. It is found
automatically, appears in `python -m hbkit report list` and on the *Reports* page of
the browser interface, and runs with `python -m hbkit report run <name>`.

## The interface (`hbkit/reports/base.py`)

```python
from typing import Any

from ..analyses.base import Param
from .base import Report, ReportContext, ReportOutput
from .html import SERIES, esc, figure_html, money, table


class MonthlyCashFlow(Report):
    name = "monthly-cash-flow"
    description = "Money in and out per month for one year."
    period_kind = "single"          # "single": --year; "range": --year or --from/--to
    year_mode = None                # or "tax"/"calendar" to ignore the settings' year mode
    needs_charts = True             # False: the chart library is not embedded
    params = (Param("top", int, 8, "categories shown separately"),)

    def build(self, ctx: ReportContext, params: dict[str, Any]) -> ReportOutput:
        import plotly.graph_objects as go   # import Plotly inside build()

        period = ctx.periods[0]
        rows = ctx.conn.execute(
            "SELECT substr(date, 1, 7), flow, SUM(amount) FROM r_flows WHERE period = ? GROUP BY 1, 2",
            (period,)).fetchall()
        fig = go.Figure(...)
        body = f'<div class="card">{figure_html(fig)}</div>' + table([...], [...])
        return ReportOutput(
            title="Monthly cash flow",
            subtitle=ctx.title(period),
            body=body,
            counts={"Months": 12},           # numbers only: this is all the terminal shows
            csv=None,                        # or (header, rows) to save a CSV next to the page
        )


REPORT = MonthlyCashFlow()
```

`run_report` adds everything every report must show — parameters, year mode, the data
date range, excluded accounts, settings file, source fingerprint and generation date —
and writes `data/out/reports/<name>_<years>_<date_time>.html` without overwriting
earlier reports.

## What a report gets (`ReportContext`)

* `ctx.conn` — an in-memory SQLite database with the full export plus the report layer.
  **Build on the shared views**, so every report counts the same way:
  `r_lines`, `r_flows` (no transfers: use it for every total), `r_group_lines`,
  `r_category_period` (nominal and `real_total`), `r_period_totals`.
  See docs/sqlite-schema.md.
* `ctx.periods` (chosen with --year/--from/--to), `ctx.all_periods`, `ctx.first_date`,
  `ctx.last_date`, `ctx.mode`, `ctx.label(p)` (e.g. "2025/26 (partial)"),
  `ctx.title(p)`, `ctx.is_partial(p)`.
* `ctx.settings` (year mode, groups, CPI file, minimum yearly spend), `ctx.data`
  (excluded accounts, group category keys, CPI months), `ctx.book` (the parsed file),
  `ctx.symbol` (currency symbol).
* `month_end_balances(book, excluded)` in `hbkit/reports/data.py` for balances.

## Rules

1. **Privacy.** The terminal shows only `counts` (numbers). The page itself may show the
   owner's data: it is written to `data/out/reports/`. Escape every piece of file text
   with `esc()` (the `table()` helper does this for you).
2. **Offline.** Never reference anything on the internet: no CDN scripts, fonts or
   images. `figure_html()` leaves the chart library out of each chart; the page embeds it
   once.
3. **Charts.** Use `SERIES` (the validated categorical palette) in its fixed order, at most
   eight series (fold the rest into "Other"), one y-axis per chart, and a table with the
   same numbers. Check the result by rendering it.
4. **Partial years.** Mark them (`ctx.label`) and do not compare a partial year with a
   full one without saying so.

## Tests

Add tests to `tests/test_reports.py` (or a new file using `ReportTestCase` and the
`needs_plotly` decorator from it). Use the hand-written `MINI` file from
`tests/test_report_data.py` for numbers worked out by hand, and
`sample(reporting=True)` for the larger synthetic file. `CommonTests` automatically
checks every report for loading nothing from elsewhere, showing the standard header,
and keeping file text off the terminal.
