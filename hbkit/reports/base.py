"""The interface every report implements, and the code that runs one.

A report is one module in hbkit/reports/ that defines ``REPORT``, an instance of a
subclass of :class:`Report`. Its ``build`` method returns a :class:`ReportOutput`;
the runner adds the header (parameters, data range, generation date) and writes a
self-contained HTML file to data/out/reports/.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..analyses.base import Param
from ..errors import HbkitError
from ..privacy import Summary, timestamp
from ..xhb.model import Book
from .data import ReportData, build_report_data, period_bounds, period_label, period_title
from .settings import Settings


@dataclass
class ReportContext:
    book: Book
    data: ReportData
    settings: Settings
    input_path: Path
    periods: list[int]  # the periods chosen with --year / --from / --to
    all_periods: list[int]  # every period with data
    first_date: date
    last_date: date

    @property
    def mode(self) -> str:
        return self.settings.year_mode

    @property
    def conn(self):
        return self.data.conn

    @property
    def symbol(self) -> str:
        cur = self.book.currencies.get(self.book.properties.currency_key)
        return cur.symbol if cur and cur.symbol and cur.symbol != "(null)" else "R"

    def label(self, period: int) -> str:
        text = period_label(period, self.mode)
        return text + " (partial)" if self.is_partial(period) else text

    def is_partial(self, period: int) -> bool:
        start, end = period_bounds(period, self.mode)
        return end > self.last_date or start < self.first_date.replace(day=1)

    def title(self, period: int) -> str:
        return period_title(period, self.mode)


@dataclass
class ReportOutput:
    title: str
    subtitle: str
    body: str  # HTML
    charts: bool = True
    script: str = ""
    csv: tuple[list[str], list[list[Any]]] | None = None  # optional CSV alongside the HTML
    counts: dict[str, int] = field(default_factory=dict)  # for the terminal (numbers only)
    extra_meta: list[tuple[str, str]] = field(default_factory=list)


class Report:
    name: str = ""
    description: str = ""
    #: "range": --from/--to (or --year for one); "single": exactly one period.
    period_kind: str = "range"
    #: Set to "tax" or "calendar" to use that year mode whatever the settings say.
    year_mode: str | None = None
    #: False for reports without charts (the chart library is then not embedded).
    needs_charts: bool = True
    params: tuple[Param, ...] = ()

    def build(self, ctx: ReportContext, params: dict[str, Any]) -> ReportOutput:
        raise NotImplementedError


def choose_periods(kind: str, all_periods: list[int], last_date: date, mode: str,
                   year: int | None, start: int | None, end: int | None) -> list[int]:
    if not all_periods:
        raise HbkitError("there are no transactions to report on")
    if year is not None and (start is not None or end is not None):
        raise HbkitError("use either --year or --from/--to, not both")
    if kind == "single":
        if start is not None or end is not None:
            raise HbkitError("this report covers one year: use --year")
        if year is None:  # the latest period that has ended
            ended = [p for p in all_periods if period_bounds(p, mode)[1] <= last_date]
            year = (ended or all_periods)[-1]
        if year not in all_periods:
            raise HbkitError("there are no transactions in that year")
        return [year]
    if year is not None:
        start = end = year
    start = all_periods[0] if start is None else start
    end = all_periods[-1] if end is None else end
    chosen = [p for p in all_periods if start <= p <= end]
    if not chosen:
        raise HbkitError("there are no transactions in that range of years")
    return chosen


def run_report(report: Report, book: Book, input_path: Path, settings: Settings, out_root: Path,
               params: dict[str, Any], year: int | None = None, start: int | None = None,
               end: int | None = None) -> tuple[Summary, Path]:
    from dataclasses import replace

    from .html import page, require_plotly

    if report.needs_charts:
        require_plotly()
    if report.year_mode and report.year_mode != settings.year_mode:
        settings = replace(settings, year_mode=report.year_mode)
    data = build_report_data(book, settings)
    try:
        conn = data.conn
        all_periods = [r[0] for r in conn.execute("SELECT DISTINCT period FROM r_lines ORDER BY period")]
        first, last = conn.execute("SELECT MIN(date), MAX(date) FROM r_lines").fetchone()
        if first is None:
            raise HbkitError("there are no transactions to report on")
        first_date, last_date = date.fromisoformat(first), date.fromisoformat(last)
        periods = choose_periods(report.period_kind, all_periods, last_date, settings.year_mode, year, start, end)
        ctx = ReportContext(book, data, settings, input_path, periods, all_periods, first_date, last_date)
        out = report.build(ctx, params)

        mode_text = "SA tax year (1 March – end February)" if settings.year_mode == "tax" else "calendar year"
        chosen = (ctx.title(periods[0]) if len(periods) == 1
                  else f"{period_label(periods[0], settings.year_mode)} to {period_label(periods[-1], settings.year_mode)}")
        meta = [
            ("Report", report.name),
            ("Years", chosen),
            ("Year mode", mode_text),
            *[(p.name.replace("_", " ").capitalize() + " option",
               f"{params[p.name]:g}" if isinstance(params[p.name], float)
               else (str(params[p.name]) if params[p.name] != "" else "(not given)"))
              for p in report.params],
            ("Data range", f"{first_date:%d %b %Y} – {last_date:%d %b %Y} (all transactions in the file)"),
            ("Excluded accounts", ", ".join(data.excluded_accounts) or "none"),
            ("Settings", settings.describe()),
            ("HomeBank file", f"{input_path.name} (fingerprint {hashlib.sha256(book.raw).hexdigest()[:12]})"),
            ("Generated", datetime.now().strftime("%d %b %Y %H:%M")),
            *out.extra_meta,
        ]
        if data.problem_details:
            meta.append(("Settings problems", "; ".join(data.problem_details)))
        html_text = page(out.title, out.subtitle, meta, out.body, out.charts, out.script)

        folder = Path(out_root) / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        span = period_label(periods[0], settings.year_mode)
        if len(periods) > 1:
            span += "_to_" + period_label(periods[-1], settings.year_mode)
        stem = f"{report.name}_{span.replace('/', '-')}_{timestamp()}"
        n = 2
        while (folder / f"{stem}.html").exists():  # never overwrite an earlier report
            stem = f"{report.name}_{span.replace('/', '-')}_{timestamp()}_{n}"
            n += 1
        path = folder / f"{stem}.html"
        path.write_text(html_text, encoding="utf-8")
        s = Summary(f"Report: {report.name}")
        s.count("Years covered", len(periods))
        for label, n in out.counts.items():
            s.count(label, n)
        for problem, n in sorted(data.problems.items()):
            s.count(f"Settings: {problem}", n)
        s.output("Report", path)
        if out.csv is not None:
            from ..analyses.base import write_csv

            csv_path = folder / f"{stem}.csv"
            write_csv(csv_path, out.csv[0], out.csv[1])
            s.output("CSV", csv_path)
        return s, path
    finally:
        data.close()
