"""tax-year-statement: for one SA tax year, the totals of each configured tax group,
with every transaction line listed and subtotalled. Printable, and also saved as CSV.

Tax groups are the category groups whose name starts with "tax:" (or the groups named
with --groups). A line can belong to several groups and is then listed in each.
Transfers between your own accounts, void transactions and excluded accounts are left
out, as in every report.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..analyses.base import Param
from ..errors import HbkitError
from .base import Report, ReportContext, ReportOutput
from .data import period_bounds
from .html import esc, money, table

class TaxYearStatement(Report):
    name = "tax-year-statement"
    description = "One tax year: totals per tax group with every transaction listed; printable, plus CSV."
    period_kind = "single"
    year_mode = "tax"
    needs_charts = False
    params = (
        Param("groups", str, "", 'groups to include, separated by commas (default: every group starting "tax:")'),
    )

    def build(self, ctx: ReportContext, params: dict[str, Any]) -> ReportOutput:
        period = ctx.periods[0]
        sym = ctx.symbol
        configured = [g.name for g in ctx.settings.groups]
        wanted = [g.strip() for g in str(params.get("groups") or "").split(",") if g.strip()]
        if wanted:
            by_fold = {g.casefold(): g for g in configured}
            unknown = [g for g in wanted if g.casefold() not in by_fold]
            if unknown:
                raise HbkitError(f"{len(unknown)} of the groups given with --groups are not in the settings file")
            groups = [by_fold[g.casefold()] for g in wanted]
        else:
            groups = [g for g in configured if g.casefold().startswith("tax:")]

        start, end = period_bounds(period, ctx.mode)
        title = ctx.title(period)
        warn = ""
        if end > ctx.last_date:
            warn = (f'<p class="warn">Your HomeBank file only has transactions up to {ctx.last_date:%d %B %Y}, '
                    f"so this tax year is not complete yet.</p>")
        if not groups:
            body = warn + ('<p class="warn">No tax groups are set up. Add groups whose name starts with '
                           '<code>tax:</code> to the settings file, for example:<br><code>[groups."tax: medical"]'
                           '<br>categories = ["Medical"]</code></p>')
            return ReportOutput("Tax-year statement", title, body, charts=False, csv=(CSV_HEADER, []),
                                counts={"Tax groups": 0, "Transaction lines": 0})

        lines: dict[str, list[tuple]] = defaultdict(list)
        placeholders = ",".join("?" * len(groups))
        for row in ctx.conn.execute(
                f"SELECT group_name, date, COALESCE(payee, ''), COALESCE(memo, ''), "
                f"COALESCE(category, '(uncategorised)'), COALESCE(tags, ''), amount, transaction_id, line_no "
                f"FROM r_group_lines WHERE period = ? AND group_name IN ({placeholders}) "
                f"ORDER BY group_name, date, transaction_id, line_no", (period, *groups)):
            lines[row[0]].append(row[1:])

        def sums(rows) -> tuple[float, float, float]:
            money_in = round(sum(r[5] for r in rows if r[5] > 0), 2)
            money_out = round(-sum(r[5] for r in rows if r[5] < 0), 2)
            return money_in, money_out, round(money_in - money_out, 2)

        # summary of all groups
        summary_rows = []
        for g in groups:
            i, o, n = sums(lines[g])
            summary_rows.append([g, len(lines[g]), money(i, sym), money(o, sym), money(n, sym)])
        summary = table(["Group", "Lines", "Money in", "Money out", "Net"], summary_rows, numeric=[1, 2, 3, 4])

        sections, csv_rows = [], []
        for n_group, g in enumerate(groups):
            rows = lines[g]
            i, o, net = sums(rows)
            per_cat: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
            for r in rows:
                per_cat[r[3]][0] += 1
                per_cat[r[3]][1] += r[5]
            cat_table = table(["Category", "Lines", "Amount"],
                              [[c, n, money(v, sym)] for c, (n, v) in sorted(per_cat.items())], numeric=[1, 2])
            detail = [[f"{r[0][8:10]}/{r[0][5:7]}/{r[0][:4]}", r[1], r[2], r[3], money(r[5], sym)] for r in rows]
            detail += [["", "", "", "Money in", money(i, sym)], ["", "", "", "Money out", money(-o, sym)],
                       ["", "", "", f"Net — {g}", money(net, sym)]]
            classes = [None] * len(rows) + ["subtotal", None, "subtotal"]
            sections.append(
                f'<section class="{"pagebreak" if n_group else ""}">'
                f"<h2>{esc(g)}</h2>"
                f'<p class="note">{esc(title)} · {len(rows)} line(s)</p>'
                + (f'<div class="card"><h3>By category</h3>{cat_table}</div>' if rows else "")
                + (f'<div class="card"><h3>Transactions</h3>'
                   f'{table(["Date", "Payee", "Memo", "Category", "Amount"], detail, numeric=[4], row_classes=classes)}'
                   f"</div>" if rows else '<p class="note">No transactions in this group in this tax year.</p>')
                + "</section>"
            )
            for r in rows:
                csv_rows.append([g, "line", r[0], r[1], r[2], r[3], r[4], round(r[5], 2)])
            csv_rows += [[g, "subtotal: money in", "", "", "", "", "", i],
                         [g, "subtotal: money out", "", "", "", "", "", -o],
                         [g, "subtotal: net", "", "", "", "", "", net]]

        overlap = sum(1 for _ in ctx.conn.execute(
            f"SELECT transaction_id, line_no FROM r_group_lines WHERE period = ? AND group_name IN ({placeholders}) "
            f"GROUP BY transaction_id, line_no HAVING COUNT(*) > 1", (period, *groups)))
        notes = (f'<p class="note">Amounts are signed: money in is positive, money out negative. Transfers between your '
                 f"own accounts, void transactions and excluded accounts are left out. Dates are day/month/year."
                 + (f" {overlap} line(s) belong to more than one group and are listed in each." if overlap else "")
                 + "</p>")
        body = (
            warn
            + '<p class="noprint"><button type="button" onclick="window.print()">Print</button></p>'
            + f'<div class="card"><h2>Summary</h2>{summary}{notes}</div>'
            + "".join(sections)
        )
        return ReportOutput(
            title="Tax-year statement",
            subtitle=title,
            body=body,
            charts=False,
            csv=(CSV_HEADER, csv_rows),
            counts={"Tax groups": len(groups), "Transaction lines": sum(len(v) for v in lines.values())},
            extra_meta=[("Tax year", f"{start:%d %B %Y} – {end:%d %B %Y}"), ("Groups included", ", ".join(groups))],
        )


CSV_HEADER = ["group", "row_type", "date", "payee", "memo", "category", "tags", "amount"]

REPORT = TaxYearStatement()
