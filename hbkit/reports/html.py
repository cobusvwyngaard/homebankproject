"""Self-contained HTML pages for reports: one file, chart library embedded, works offline.

All text from the file is escaped. Charts use Plotly with the reference palette
(fixed categorical order; checked for colour-blind separation) on a light page that
also prints well.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Any

# Categorical slots in fixed order (dataviz reference palette, light mode).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
TEXT, TEXT_2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f4f4f1; color: %(text)s; font: 14px/1.45 "Segoe UI", system-ui, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 28px 0 8px; }
h3 { font-size: 15px; margin: 18px 0 6px; }
.sub { color: %(text2)s; margin: 0 0 16px; }
.card { background: %(surface)s; border: 1px solid %(grid)s; border-radius: 10px; padding: 14px 16px; margin: 12px 0; }
table { border-collapse: collapse; width: 100%%; font-variant-numeric: tabular-nums; }
th, td { padding: 5px 8px; border-bottom: 1px solid %(grid)s; text-align: left; vertical-align: top; }
th { color: %(text2)s; font-weight: 600; border-bottom: 2px solid %(grid)s; }
td.num, th.num { text-align: right; white-space: nowrap; }
tr.subtotal td { font-weight: 650; border-top: 2px solid %(grid)s; }
tr.highlight td { background: #fff3df; }
.meta td { border: 0; padding: 2px 10px 2px 0; }
.meta td:first-child { color: %(text2)s; white-space: nowrap; width: 1%%; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.tile { background: %(surface)s; border: 1px solid %(grid)s; border-radius: 10px; padding: 12px 14px; }
.tile .label { color: %(text2)s; font-size: 13px; }
.tile .value { font-size: 24px; font-weight: 650; margin: 2px 0; }
.tile .delta { font-size: 13px; color: %(text2)s; }
.note { color: %(text2)s; font-size: 13px; }
.warn { background: #fff3df; border-left: 4px solid #c98500; padding: 8px 12px; border-radius: 6px; }
.badge { display: inline-block; padding: 0 7px; border-radius: 9px; font-size: 12px; background: #fff3df; border: 1px solid #c98500; }
.toggle { display: flex; gap: 6px; margin: 8px 0; flex-wrap: wrap; }
.toggle button { font: inherit; padding: 5px 12px; border: 1px solid %(grid)s; border-radius: 6px; background: %(surface)s; cursor: pointer; }
.toggle button.on { background: %(text)s; color: %(surface)s; border-color: %(text)s; }
footer { color: %(muted)s; font-size: 12px; text-align: center; padding: 20px; }
details > summary { cursor: pointer; color: %(text2)s; }
@media print {
  body { background: #fff; font-size: 11pt; }
  main { max-width: none; padding: 0; }
  .card { border: 0; padding: 0; }
  .noprint, .toggle { display: none !important; }
  .pagebreak { break-before: page; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  @page { size: A4; margin: 15mm; }
}
""" % {"text": TEXT, "text2": TEXT_2, "muted": MUTED, "grid": GRID, "surface": SURFACE}


INSTALL_HINT = (
    "the reports need Plotly, which is not installed yet. Install it once (no internet needed) "
    "from the project folder with: .venv\\Scripts\\python -m pip install --no-index "
    "--find-links vendor\\wheels -r requirements.txt"
)


def require_plotly() -> None:
    try:
        import plotly  # noqa: F401
    except ImportError:
        from ..errors import HbkitError

        raise HbkitError(INSTALL_HINT) from None


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: float | None, symbol: str = "R", decimals: int = 2) -> str:
    if value is None:
        return "–"
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.{decimals}f}".replace(",", " ")
    return f"{sign}{symbol} {text}"


def percent(value: float | None, decimals: int = 1, signed: bool = False) -> str:
    if value is None:
        return "–"
    return f"{value:+.{decimals}f}%" if signed else f"{value:.{decimals}f}%"


def table(header: Sequence[str], rows: Iterable[Sequence[Any]], numeric: Iterable[int] = (),
          row_classes: Sequence[str | None] | None = None, raw: Iterable[int] = ()) -> str:
    """An HTML table. Cells are escaped unless their column is in `raw` (already HTML)."""
    numeric, raw = set(numeric), set(raw)
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{esc(h)}</th>' for i, h in enumerate(header))
    body = []
    for n, row in enumerate(rows):
        cls = row_classes[n] if row_classes and n < len(row_classes) and row_classes[n] else ""
        cells = "".join(
            f'<td class="{"num" if i in numeric else ""}">{c if i in raw else esc(c)}</td>' for i, c in enumerate(row))
        body.append(f'<tr class="{cls}">{cells}</tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def figure_html(fig) -> str:
    """A chart as a <div>; the chart library itself is included once per page."""
    import plotly.io as pio

    style_figure(fig)
    return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                       config={"displaylogo": False, "responsive": True,
                               "modeBarButtonsToRemove": ["select2d", "lasso2d"]})


def style_figure(fig) -> None:
    fig.update_layout(
        template="plotly_white",
        colorway=SERIES,
        font=dict(family="Segoe UI, system-ui, sans-serif", size=13, color=TEXT),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        margin=dict(l=60, r=20, t=50, b=50),
        hoverlabel=dict(bgcolor="white", font_color=TEXT),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        bargap=0.25, bargroupgap=0.06,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickfont_color=TEXT_2)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor="#c9c8c2", tickfont_color=TEXT_2)


def page(title: str, subtitle: str, meta: Sequence[tuple[str, str]], body: str, charts: bool = True,
         script: str = "") -> str:
    """A complete HTML document. `meta` rows describe parameters, data range and generation date."""
    plotly_js = ""
    if charts:
        from plotly.offline import get_plotlyjs

        plotly_js = f"<script>{get_plotlyjs()}</script>"
    meta_rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in meta)
    return f"""<!doctype html>
<html lang="en-ZA">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
{plotly_js}
</head>
<body>
<main>
<h1>{esc(title)}</h1>
<p class="sub">{esc(subtitle)}</p>
<div class="card"><table class="meta">{meta_rows}</table></div>
{body}
</main>
<footer>Made by hbkit from your own HomeBank file, on your own computer. This page works offline.</footer>
{f"<script>{script}</script>" if script else ""}
</body>
</html>
"""
