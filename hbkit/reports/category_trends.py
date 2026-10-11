"""category-trends: spending per category and per category group for every year,
as charts and tables, in nominal rand or (with a CPI table) in today's prices, with
the categories that grow faster than inflation highlighted.

Growth is measured over complete years only (a year still in progress would distort
it), as the average yearly growth between the first and the last complete year with
spending. Inflation over the same years comes from the average CPI index of each year.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from ..analyses.base import Param
from .base import Report, ReportContext, ReportOutput
from .data import period_of
from .html import SERIES, esc, figure_html, money, percent, table

OTHER_GREY = "#8a8984"
MAX_STACKED = 7  # named categories in the stacked chart; the rest become "Other"

TOGGLE_SCRIPT = """
function hbkitMode(mode) {
  document.querySelectorAll('[data-mode]').forEach(function (el) {
    el.style.display = el.getAttribute('data-mode') === mode ? '' : 'none';
  });
  document.querySelectorAll('.toggle button').forEach(function (b) {
    b.classList.toggle('on', b.getAttribute('data-set') === mode);
  });
  document.querySelectorAll('[data-mode="' + mode + '"] .js-plotly-plot').forEach(function (d) {
    if (window.Plotly) { Plotly.Plots.resize(d); }
  });
}
function hbkitSubs(show) {
  document.querySelectorAll('tr.sub').forEach(function (r) { r.style.display = show ? '' : 'none'; });
}
document.addEventListener('DOMContentLoaded', function () {
  hbkitMode('nominal');
  hbkitSubs(false);
});
"""


def average_growth(values: dict[int, float], first: int | None, last: int | None) -> tuple[float | None, str]:
    """Average yearly growth from `first` to `last`, or None with a reason ("new", "stopped", "")."""
    if first is None or last is None or last <= first:
        return None, ""
    a, b = values.get(first, 0.0), values.get(last, 0.0)
    if a <= 0:
        return None, "new" if b > 0 else ""
    if b <= 0:
        return None, "stopped"
    return (b / a) ** (1 / (last - first)) - 1, ""


class CategoryTrends(Report):
    name = "category-trends"
    description = "Spending per category and category group for every year; nominal or in today's prices."
    period_kind = "range"
    params = (
        Param("inflation_margin", float, 1.0,
              "flag a category when it grows at least this many percentage points a year faster than inflation"),
    )

    def build(self, ctx: ReportContext, params: dict[str, Any]) -> ReportOutput:
        import plotly.graph_objects as go

        conn, sym, periods = ctx.conn, ctx.symbol, ctx.periods
        labels = [ctx.label(p) for p in periods]
        complete = [p for p in periods if not ctx.is_partial(p)]
        has_cpi = ctx.data.cpi_months > 0
        min_spend = ctx.settings.min_yearly_spend
        margin = float(params.get("inflation_margin", 1.0)) / 100.0
        first_full, last_full = (complete[0], complete[-1]) if len(complete) >= 2 else (None, None)

        # --- spending per category (positive = money spent; refunds reduce it) -------------------
        nominal: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        real: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        parent_of: dict[str, str] = {}
        without_cpi = 0
        for period, cat, top, nom, rl, missing in conn.execute(
                "SELECT period, category, category_top, -SUM(total), -SUM(COALESCE(real_total, 0)), "
                "SUM(lines_without_cpi) FROM r_category_period WHERE flow = 'expense' GROUP BY period, category"):
            if period not in periods:
                continue
            without_cpi += missing or 0
            for name in {cat, top}:
                nominal[name][period] += nom
                real[name][period] += rl
            if cat != top:
                parent_of[cat] = top
        tops = sorted((c for c in nominal if c not in parent_of),
                      key=lambda c: -sum(nominal[c].values()))
        subs: dict[str, list[str]] = defaultdict(list)
        for cat, top in parent_of.items():
            subs[top].append(cat)

        # --- spending per group ------------------------------------------------------------------
        g_nom: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        g_real: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for group, period, nom, rl in conn.execute(
                "SELECT group_name, period, -SUM(amount), -SUM(COALESCE(amount * cpi_factor, 0)) "
                "FROM r_group_lines WHERE flow = 'expense' GROUP BY group_name, period"):
            if period in periods:
                g_nom[group][period] += nom
                g_real[group][period] += rl
        groups = [g.name for g in ctx.settings.groups]

        # --- inflation per year (average index of the year's months) ----------------------------
        cpi_avg: dict[int, float] = {}
        if has_cpi:
            per: dict[int, list[float]] = defaultdict(list)
            for month, idx in conn.execute("SELECT month, idx FROM cpi"):
                y, m = map(int, month.split("-"))
                per[period_of(date(y, m, 1), ctx.mode)].append(idx)
            cpi_avg = {p: sum(v) / len(v) for p, v in per.items()}

        def inflation(first: int | None, last: int | None) -> float | None:
            if first is None or first not in cpi_avg or last not in cpi_avg:
                return None
            return (cpi_avg[last] / cpi_avg[first]) ** (1 / (last - first)) - 1

        flagged: set[str] = set()

        def growth_cells(values: dict[int, float], name: str, shown: dict[int, float],
                         real_view: bool) -> tuple[list[str], bool]:
            """Growth of the amounts shown, and the flag (always nominal growth vs inflation)."""
            g, reason = average_growth(values, first_full, last_full)
            infl = inflation(first_full, last_full)
            yearly = sum(values.get(p, 0) for p in complete) / len(complete) if complete else 0
            fast = g is not None and infl is not None and g >= infl + margin and yearly >= min_spend
            if fast:
                flagged.add(name)
            shown_g, shown_reason = average_growth(shown, first_full, last_full) if real_view else (g, reason)
            cells = [percent(shown_g * 100, 1, True) if shown_g is not None else (shown_reason or "–")]
            if has_cpi and not real_view:
                cells.append(percent(infl * 100, 1, True) if infl is not None else "–")
            if has_cpi:
                cells.append('<span class="badge">faster</span>' if fast else "")
            return cells, fast

        def headers(first: str, real_view: bool) -> list[str]:
            if not has_cpi:
                return [first, *labels, "Avg growth / yr"]
            if real_view:
                return [first, *labels, "Real growth / yr", "Faster than inflation"]
            return [first, *labels, "Avg growth / yr", "Inflation / yr", "Faster than inflation"]

        def category_table(data: dict[str, dict[int, float]], with_subs: bool, real_view: bool) -> str:
            header = headers("Category", real_view)
            rows, classes = [], []
            for top in tops:
                cells, fast = growth_cells(nominal[top], top, data[top], real_view)
                rows.append([top, *[money(data[top].get(p, 0.0), sym, 0) for p in periods], *cells])
                classes.append("highlight" if fast else None)
                if with_subs:
                    for sub in sorted(subs.get(top, []), key=lambda c: -sum(nominal[c].values())):
                        cells, fast = growth_cells(nominal[sub], sub, data[sub], real_view)
                        rows.append(["\u2003" + sub.split(":", 1)[-1], *[money(data[sub].get(p, 0.0), sym, 0)
                                                                          for p in periods], *cells])
                        classes.append("sub highlight" if fast else "sub")
            total = [sum(data[t].get(p, 0.0) for t in tops) for p in periods]
            rows.append(["Total", *[money(v, sym, 0) for v in total]] + [""] * (len(header) - len(labels) - 1))
            classes.append("subtotal")
            raw = [len(header) - 1] if has_cpi else []
            return table(header, rows, numeric=range(1, len(header) - (1 if has_cpi else 0)),
                         row_classes=classes, raw=raw)

        def group_table(data: dict[str, dict[int, float]], real_view: bool) -> str:
            if not groups:
                return '<p class="note">No category groups are set up in the settings file.</p>'
            header = headers("Group", real_view)
            rows, classes = [], []
            for g in groups:
                cells, fast = growth_cells(g_nom[g], f"group {g}", data[g], real_view)
                rows.append([g, *[money(data[g].get(p, 0.0), sym, 0) for p in periods], *cells])
                classes.append("highlight" if fast else None)
            raw = [len(header) - 1] if has_cpi else []
            return table(header, rows, numeric=range(1, len(header) - (1 if has_cpi else 0)),
                         row_classes=classes, raw=raw)

        # --- charts ----------------------------------------------------------------------------------
        def stacked(data: dict[str, dict[int, float]], title: str) -> str:
            fig = go.Figure()
            named, rest = tops[:MAX_STACKED], tops[MAX_STACKED:]
            hover = f"%{{fullData.name}}<br>%{{x}}: {sym} %{{y:,.0f}}<extra></extra>"
            for i, cat in enumerate(named):
                fig.add_bar(x=labels, y=[round(data[cat].get(p, 0.0), 2) for p in periods], name=cat,
                            marker=dict(color=SERIES[i], line=dict(color="#fcfcfb", width=1)), hovertemplate=hover)
            if rest:
                fig.add_bar(x=labels, y=[round(sum(data[c].get(p, 0.0) for c in rest), 2) for p in periods],
                            name=f"Other ({len(rest)} categories)",
                            marker=dict(color=OTHER_GREY, line=dict(color="#fcfcfb", width=1)), hovertemplate=hover)
            fig.update_layout(title=title, barmode="relative", separators=". ", height=480,
                              yaxis=dict(tickprefix=f"{sym} ", tickformat=",.0f"))
            return figure_html(fig)

        def single(data: dict[str, dict[int, float]], gdata: dict[str, dict[int, float]], title: str) -> str:
            series = [(t, data[t]) for t in tops] + [(f"Group: {g}", gdata[g]) for g in groups]
            if not series:
                return ""
            fig = go.Figure()
            for i, (name, values) in enumerate(series):
                fig.add_bar(x=labels, y=[round(values.get(p, 0.0), 2) for p in periods], name=name,
                            visible=i == 0, marker=dict(color=SERIES[0], cornerradius=4),
                            hovertemplate=f"%{{x}}: {sym} %{{y:,.0f}}<extra></extra>")
            buttons = [dict(label=name, method="update",
                            args=[{"visible": [j == i for j in range(len(series))]},
                                  {"title.text": f"{title}: {name}"}])
                       for i, (name, _) in enumerate(series)]
            fig.update_layout(title=f"{title}: {series[0][0]}", showlegend=False, separators=". ", height=420,
                              yaxis=dict(tickprefix=f"{sym} ", tickformat=",.0f"),
                              updatemenus=[dict(buttons=buttons, direction="down", x=1, xanchor="right",
                                                y=1.12, yanchor="bottom", showactive=True)])
            return figure_html(fig)

        def section(mode: str, data, gdata, unit_note: str) -> str:
            return (
                f'<div data-mode="{mode}">'
                f'<p class="note">{esc(unit_note)}</p>'
                f'<div class="card">{stacked(data, "Spending per category")}</div>'
                f'<div class="card">{single(data, gdata, "One category or group")}'
                '<p class="note">Choose a category or group in the list at the top right of the chart.</p></div>'
                f"<h2>Per category</h2>"
                '<div class="toggle noprint"><button type="button" onclick="hbkitSubs(true)">Show subcategories</button>'
                '<button type="button" onclick="hbkitSubs(false)">Hide subcategories</button></div>'
                f'<div class="card">{category_table(data, True, mode == "real")}</div>'
                f"<h2>Per category group</h2><div class=\"card\">{group_table(gdata, mode == 'real')}</div>"
                "</div>"
            )

        latest_cpi = conn.execute("SELECT MAX(month) FROM cpi").fetchone()[0] if has_cpi else None
        body = ""
        if has_cpi:
            body += ('<div class="toggle noprint">'
                     '<button type="button" data-set="nominal" onclick="hbkitMode(\'nominal\')">Nominal rand</button>'
                     '<button type="button" data-set="real" onclick="hbkitMode(\'real\')">'
                     f'In {esc(latest_cpi)} prices (inflation-adjusted)</button></div>')
        else:
            body += ('<p class="warn">No CPI table is set up, so amounts are nominal and growth is not compared '
                     "with inflation. Add <code>cpi_file</code> under <code>[inflation]</code> in the settings "
                     "file to switch to today's prices.</p>")
        body += section("nominal", nominal, g_nom,
                        "Amounts in rand as spent (nominal). Refunds reduce spending.")
        if has_cpi:
            note = f"Amounts in {latest_cpi} prices: each month's spending is multiplied by the latest CPI index ÷ that month's index."
            if without_cpi:
                note += f" {without_cpi} line(s) before the first CPI month are left out of these amounts."
            body += section("real", real, g_real, note)

        fast_list = sorted(n for n in flagged if not n.startswith("group "))
        fast_groups = sorted(n[len("group "):] for n in flagged if n.startswith("group "))
        summary = ""
        if has_cpi:
            if complete and len(complete) >= 2:
                summary = (f'<div class="card"><h3>Growing faster than inflation</h3><p>Average yearly growth from '
                           f'{esc(ctx.label(first_full))} to {esc(ctx.label(last_full))} (the first and last complete '
                           f'years), compared with CPI over the same years. Flagged when growth is at least '
                           f'{float(params.get("inflation_margin", 1.0)):g} percentage point(s) a year above inflation and the '
                           f'category averages at least {esc(money(min_spend, sym, 0))} a year.</p>'
                           f'<p><b>Categories:</b> {esc(", ".join(fast_list) or "none")}<br>'
                           f'<b>Groups:</b> {esc(", ".join(fast_groups) or "none")}</p></div>')
            else:
                summary = '<p class="note">At least two complete years are needed to compare growth with inflation.</p>'
        partial = [ctx.label(p) for p in periods if ctx.is_partial(p)]
        if partial:
            summary += (f'<p class="note">Not complete in your file: {esc(", ".join(partial))}. These years are shown '
                        "but left out of the growth figures.</p>")

        return ReportOutput(
            title="Category trends",
            subtitle=f"{ctx.label(periods[0])} to {ctx.label(periods[-1])}" if len(periods) > 1 else ctx.title(periods[0]),
            body=summary + body,
            script=TOGGLE_SCRIPT,
            counts={"Top-level categories": len(tops), "Category groups": len(groups),
                    "Growing faster than inflation": len(flagged)},
            extra_meta=[("CPI table", f"{ctx.settings.cpi_file} ({ctx.data.cpi_months} months, up to {latest_cpi})"
                         if has_cpi else "none"),
                        ("Minimum yearly spend to flag", f"{money(min_spend, sym, 0)} (from the settings)")],
        )


REPORT = CategoryTrends()
