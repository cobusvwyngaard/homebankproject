"""Overview: how complete the categorisation is, per year.

A small analysis that also serves as a template for new ones. On screen: counts per
year and the share categorised. In the output folder: monthly totals per category.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..privacy import Summary
from .base import Analysis, Context, Param, pct, write_csv


class Overview(Analysis):
    name = "overview"
    description = "Transactions per year, how many are categorised, and monthly category totals (CSV)."
    params = (
        Param("include_transfers", bool, False, "count internal transfers too"),
    )

    def run(self, ctx: Context, params: dict[str, Any]) -> Summary:
        book = ctx.book
        per_year: Counter = Counter()
        categorised: Counter = Counter()
        totals: dict[tuple[str, str], float] = defaultdict(float)
        lines: Counter = Counter()
        for t in book.transactions:
            if t.is_void or t.date is None:
                continue
            if t.is_internal_transfer and not params["include_transfers"]:
                continue
            per_year[t.date.year] += 1
            parts = [(s.category_key, s.amount) for s in t.splits] or [(t.category_key, t.amount)]
            if all(k for k, _ in parts):
                categorised[t.date.year] += 1
            month = t.date.strftime("%Y-%m")
            for key, amount in parts:
                path = book.category_path(key) if key else "(uncategorised)"
                totals[(month, path or "(missing category)")] += amount
                lines[(month, path or "(missing category)")] += 1

        folder = ctx.new_run_dir()
        write_csv(
            folder / "monthly_category_totals.csv",
            ["month", "category", "total", "lines"],
            ((m, c, round(v, 2), lines[(m, c)]) for (m, c), v in sorted(totals.items())),
        )

        s = Summary("Overview")
        s.heading("Transactions per year (share fully categorised)")
        for year in sorted(per_year):
            s.percent(str(year), categorised[year], per_year[year])
        s.heading("Overall")
        s.percent("Categorised", sum(categorised.values()), sum(per_year.values()))
        s.output("Details", folder)
        return s


ANALYSIS = Overview()
