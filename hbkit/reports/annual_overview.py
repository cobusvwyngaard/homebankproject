"""annual-overview: income, expenses, net savings and savings rate per year; account
balances over time; the top expense categories with the change against the year before.

All totals come from the shared r_* views, so transfers between your own accounts are
never counted and excluded accounts are left out.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..analyses.base import Param
from .base import Report, ReportContext, ReportOutput
from .data import month_end_balances, period_bounds
from .html import SERIES, esc, figure_html, money, percent, table

#: Accounts drawn as separate lines; the rest are added together as "Other accounts".
MAX_ACCOUNT_LINES = 7
OTHER_GREY = "#8a8984"


def signed_money(value: float, symbol: str, decimals: int = 0) -> str:
    return ("+" if value > 0 else "") + money(value, symbol, decimals)


def same_day_a_year_earlier(d: date) -> date:
    return d.replace(year=d.year - 1, day=28) if (d.month, d.day) == (2, 29) else d.replace(year=d.year - 1)


def range_totals(conn, first: date, last: date) -> tuple[float, float, float, float | None]:
    """Income, expenses, net savings and savings rate between two dates (inclusive)."""
    inc, exp, net = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN flow = 'income' THEN amount END), 0), "
        "COALESCE(-SUM(CASE WHEN flow = 'expense' THEN amount END), 0), COALESCE(SUM(amount), 0) "
        "FROM r_flows WHERE date BETWEEN ? AND ?", (first.isoformat(), last.isoformat())).fetchone()
    return round(inc, 2), round(exp, 2), round(net, 2), (round(net / inc, 4) if inc > 0 else None)


def range_spend(conn, first: date, last: date) -> dict[str, float]:
    """Spending per top-level category between two dates (inclusive)."""
    return {cat: round(v, 2) for cat, v in conn.execute(
        "SELECT COALESCE(category_top, '(uncategorised)'), -SUM(amount) FROM r_flows "
        "WHERE flow = 'expense' AND date BETWEEN ? AND ? GROUP BY 1", (first.isoformat(), last.isoformat()))}


def change(now: float | None, before: float | None) -> tuple[float | None, float | None]:
    """Change in amount and in % (None when there is nothing to compare with)."""
    if now is None or before is None:
        return None, None
    diff = now - before
    return diff, (100.0 * diff / abs(before)) if before else None


class AnnualOverview(Report):
    name = "annual-overview"
    description = "Income, expenses, net savings and savings rate per year; balances; top expense categories."
    period_kind = "range"
    params = (
        Param("top", int, 10, "number of expense categories in the top list"),
    )

    def build(self, ctx: ReportContext, params: dict[str, Any]) -> ReportOutput:
        import plotly.graph_objects as go

        conn, sym = ctx.conn, ctx.symbol
        totals = {row[0]: row[1:] for row in conn.execute(
            "SELECT period, income, expenses, net_savings, savings_rate FROM r_period_totals ORDER BY period")}
        periods = [p for p in ctx.periods if p in totals]
        labels = [ctx.label(p) for p in periods]
        latest = periods[-1]
        prior = latest - 1 if (latest - 1) in totals else None

        # --- headline tiles for the latest year -------------------------------------------------
        # A year that has not ended yet is compared with the same months of the year before.
        inc, exp, net, rate = totals[latest]
        latest_end = period_bounds(latest, ctx.mode)[1]
        year_to_date = prior is not None and latest_end > ctx.last_date
        if prior is None:
            p_inc = p_exp = p_net = p_rate = None
            vs, prior_col = "no earlier year to compare with", "Year before"
            prior_spend: dict[str, float] = {}
        elif year_to_date:
            cut = same_day_a_year_earlier(ctx.last_date)
            first = period_bounds(prior, ctx.mode)[0]
            p_inc, p_exp, p_net, p_rate = range_totals(conn, first, cut)
            vs = f"vs the same months of {ctx.label(prior)}"
            prior_col = f"{ctx.label(prior)} same months"
            prior_spend = range_spend(conn, first, cut)
        else:
            p_inc, p_exp, p_net, p_rate = totals[prior]
            vs, prior_col = f"vs {ctx.label(prior)}", ctx.label(prior)
            prior_spend = range_spend(conn, *period_bounds(prior, ctx.mode))

        def tile(label: str, value: str, diff: str) -> str:
            return (f'<div class="tile"><div class="label">{esc(label)}</div>'
                    f'<div class="value">{esc(value)}</div><div class="delta">{esc(diff)}</div></div>')

        def money_delta(now, before) -> str:
            d, pc = change(now, before)
            if d is None:
                return vs
            return f"{signed_money(d, sym)} ({percent(pc, 1, True) if pc is not None else 'n/a'}) {vs}"

        rate_delta = (f"{(rate - p_rate) * 100:+.1f} percentage points {vs}"
                      if rate is not None and p_rate is not None else vs)
        tiles = (
            f"<h2>{esc(ctx.label(latest))}</h2><div class=\"tiles\">"
            + tile("Income", money(inc, sym, 0), money_delta(inc, p_inc))
            + tile("Expenses", money(exp, sym, 0), money_delta(exp, p_exp))
            + tile("Net savings", money(net, sym, 0), money_delta(net, p_net))
            + tile("Savings rate", percent(rate * 100 if rate is not None else None), rate_delta)
            + "</div>"
        )
        if year_to_date:
            tiles += (f'<p class="note">{esc(ctx.label(latest))} is not complete in your file yet (it runs to '
                      f'{ctx.last_date:%d %b %Y}), so it is compared with {esc(ctx.label(prior))} up to '
                      f'{same_day_a_year_earlier(ctx.last_date):%d %b %Y}.</p>')

        # --- chart: income, expenses and net savings per year -----------------------------------
        y_inc = [totals[p][0] for p in periods]
        y_exp = [totals[p][1] for p in periods]
        y_net = [totals[p][2] for p in periods]
        fig = go.Figure()
        hover = f"%{{x}}<br>%{{fullData.name}}: {sym} %{{y:,.0f}}<extra></extra>"
        fig.add_bar(x=labels, y=y_inc, name="Income", marker=dict(color=SERIES[0], cornerradius=4),
                    hovertemplate=hover)
        fig.add_bar(x=labels, y=y_exp, name="Expenses", marker=dict(color=SERIES[1], cornerradius=4),
                    hovertemplate=hover)
        fig.add_scatter(x=labels, y=y_net, name="Net savings", mode="lines+markers",
                        line=dict(color=SERIES[2], width=2), marker=dict(size=8), hovertemplate=hover)
        fig.update_layout(title="Income, expenses and net savings", barmode="group", separators=". ",
                          yaxis=dict(tickprefix=f"{sym} ", tickformat=",.0f"), height=420)
        chart_totals = figure_html(fig)

        # --- chart: savings rate -------------------------------------------------------------------
        rates = [totals[p][3] for p in periods]
        fig = go.Figure()
        fig.add_scatter(x=labels, y=rates, name="Savings rate", mode="lines+markers",
                        line=dict(color=SERIES[0], width=2), marker=dict(size=8),
                        hovertemplate="%{x}: %{y:.1%}<extra></extra>")
        fig.update_layout(title="Savings rate (net savings ÷ income)", showlegend=False, separators=". ",
                          yaxis=dict(tickformat=".0%", zeroline=True), height=320)
        chart_rate = figure_html(fig)

        # --- balances ------------------------------------------------------------------------------
        balances = month_end_balances(ctx.book, ctx.data.excluded_accounts)
        start = period_bounds(periods[0], ctx.mode)[0].strftime("%Y-%m")
        end = period_bounds(periods[-1], ctx.mode)[1].strftime("%Y-%m")
        balances = {name: [(m, v) for m, v in series if start <= m <= end] for name, series in balances.items()}
        balances = {name: s for name, s in balances.items() if s and any(v for _, v in s)}
        balance_html = '<p class="note">No account balances to show.</p>'
        year_end_rows: list[list[Any]] = []
        if balances:
            months = [m for m, _ in next(iter(balances.values()))]
            total = [round(sum(series[i][1] for series in balances.values()), 2) for i in range(len(months))]
            fig = go.Figure()
            fig.add_scatter(x=months, y=total, name="All included accounts", mode="lines",
                            line=dict(color=SERIES[0], width=2),
                            hovertemplate=f"%{{x}}: {sym} %{{y:,.0f}}<extra></extra>")
            fig.update_layout(title="Total balance of the included accounts (month end)", showlegend=False,
                              separators=". ", yaxis=dict(tickprefix=f"{sym} ", tickformat=",.0f"), height=360)
            chart_total_balance = figure_html(fig)

            # largest accounts by their latest balance; colour follows the account (file order), not the rank
            names = list(balances)
            biggest = sorted(names, key=lambda n: -abs(balances[n][-1][1]))[:MAX_ACCOUNT_LINES]
            shown = [n for n in names if n in biggest]
            rest = [n for n in names if n not in biggest]
            fig = go.Figure()
            for i, n in enumerate(shown):
                fig.add_scatter(x=months, y=[v for _, v in balances[n]], name=n, mode="lines",
                                line=dict(color=SERIES[i % len(SERIES)], width=2),
                                hovertemplate=f"%{{fullData.name}}<br>%{{x}}: {sym} %{{y:,.0f}}<extra></extra>")
            if rest:
                other = [round(sum(balances[n][i][1] for n in rest), 2) for i in range(len(months))]
                fig.add_scatter(x=months, y=other, name=f"Other accounts ({len(rest)})", mode="lines",
                                line=dict(color=OTHER_GREY, width=2, dash="dot"),
                                hovertemplate=f"%{{fullData.name}}<br>%{{x}}: {sym} %{{y:,.0f}}<extra></extra>")
            fig.update_layout(title="Balance per account (month end) — click a name in the legend to hide it",
                              separators=". ", yaxis=dict(tickprefix=f"{sym} ", tickformat=",.0f"), height=440)
            chart_accounts = figure_html(fig)

            # year-end balances table: every account, last month of each period
            ends = []
            for p in periods:
                last_month = min(period_bounds(p, ctx.mode)[1].strftime("%Y-%m"), months[-1])
                ends.append(months.index(last_month) if last_month in months else None)
            for n in names:
                year_end_rows.append([n] + [money(balances[n][i][1], sym, 0) if i is not None else "–" for i in ends])
            year_end_rows.append(["Total"] + [money(total[i], sym, 0) if i is not None else "–" for i in ends])
            balance_html = (
                chart_total_balance + chart_accounts
                + "<h3>Balance at the end of each year</h3>"
                + '<p class="note">The last month in your file is used for a year that has not ended yet.</p>'
                + table(["Account"] + labels, year_end_rows, numeric=range(1, len(labels) + 1),
                        row_classes=[None] * (len(year_end_rows) - 1) + ["subtotal"])
            )

        # --- top expense categories ----------------------------------------------------------------
        top_n = max(1, int(params.get("top", 10)))
        spend = {}
        for period, cat, total in conn.execute(
                "SELECT period, category_top, -SUM(total) FROM r_category_period WHERE flow = 'expense' "
                "GROUP BY period, category_top"):
            spend[(period, cat)] = total
        this_year = sorted(((cat, v) for (p, cat), v in spend.items() if p == latest), key=lambda cv: -cv[1])
        year_total = sum(v for _, v in this_year)
        top_rows = []
        for rank, (cat, v) in enumerate(this_year[:top_n], start=1):
            before = prior_spend.get(cat) if prior else None
            d, pc = change(v, before)
            top_rows.append([rank, cat, money(v, sym), money(before, sym) if before is not None else "–",
                             signed_money(d, sym, 2) if d is not None else "–", percent(pc, 1, True) if pc is not None else "–",
                             percent(100 * v / year_total if year_total else None)])
        top_html = (
            f"<h2>Top {top_n} expense categories in {esc(ctx.label(latest))}</h2>"
            + '<p class="note">Top-level categories (subcategories included). Refunds reduce spending.</p>'
            + table(["#", "Category", ctx.label(latest), prior_col,
                     "Change", "Change %", "Share of expenses"], top_rows, numeric=[0, 2, 3, 4, 5, 6])
        )

        # --- the numbers per year as a table ---------------------------------------------------------
        year_rows = [[ctx.label(p), money(totals[p][0], sym), money(totals[p][1], sym), money(totals[p][2], sym),
                      percent(totals[p][3] * 100 if totals[p][3] is not None else None)] for p in periods]
        year_table = table(["Year", "Income", "Expenses", "Net savings", "Savings rate"], year_rows,
                           numeric=[1, 2, 3, 4])

        body = (
            tiles
            + f'<div class="card">{chart_totals}</div>'
            + f'<div class="card">{chart_rate}</div>'
            + f"<h2>Per year</h2><div class=\"card\">{year_table}</div>"
            + f"{top_html}"
            + f"<h2>Balances</h2><div class=\"card\">{balance_html}</div>"
            + '<p class="note">Income and expenses leave out transfers between your own accounts, void '
              "transactions and excluded accounts. Income is money in income categories (and uncategorised "
              "money in); everything else is an expense.</p>"
        )
        return ReportOutput(
            title="Annual overview",
            subtitle=f"{ctx.label(periods[0])} to {ctx.label(periods[-1])}" if len(periods) > 1 else ctx.title(latest),
            body=body,
            counts={"Expense categories ranked": len(top_rows), "Accounts with balances": len(balances)},
        )


REPORT = AnnualOverview()
