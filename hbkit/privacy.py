"""Privacy plumbing: what may reach the terminal, and where detailed output goes.

Rules (see CLAUDE.md):

* Commands and analyses never print directly. They return a :class:`Summary`, which
  holds fixed labels from the code plus numbers — never text from the file.
* Everything printed goes through :class:`Terminal`, which hides any line that
  contains free text from the file (names, memos, rule texts) as a second guard.
* Detailed results are written under ``data/out/`` (see :func:`new_run_dir`).
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .errors import HbkitError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = DATA_DIR / "out"

REDACTED = "[line hidden: it contained text from your HomeBank file]"


# --- terminal summary --------------------------------------------------------------------


@dataclass
class _Row:
    kind: str
    label: str
    value: object = None
    extra: object = None


@dataclass
class Summary:
    """Aggregate-only terminal output. Labels must be fixed text written in the code."""

    title: str
    rows: list[_Row] = field(default_factory=list)

    def heading(self, text: str) -> None:
        self.rows.append(_Row("heading", text))

    def note(self, text: str) -> None:
        self.rows.append(_Row("note", text))

    def count(self, label: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Summary.count() accepts integers only")
        self.rows.append(_Row("count", label, value))

    def number(self, label: str, value: float, decimals: int = 1, unit: str = "") -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("Summary.number() accepts numbers only")
        self.rows.append(_Row("number", label, float(value), (decimals, unit)))

    def percent(self, label: str, part: float, whole: float) -> None:
        for v in (part, whole):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise TypeError("Summary.percent() accepts numbers only")
        self.rows.append(_Row("percent", label, part, whole))

    def output(self, label: str, path: Path) -> None:
        """Where detailed output was written (shown relative to the project folder)."""
        self.rows.append(_Row("output", label, Path(path)))

    def render(self) -> list[str]:
        out = [self.title, "=" * len(self.title)]
        width = max([len(r.label) for r in self.rows if r.kind not in ("heading", "note")] + [10])
        for r in self.rows:
            if r.kind == "heading":
                out += ["", r.label]
            elif r.kind == "note":
                out.append(f"  {r.label}")
            elif r.kind == "count":
                out.append(f"  {r.label:<{width}}  {r.value:>10,}".replace(",", " "))
            elif r.kind == "number":
                decimals, unit = r.extra
                out.append(f"  {r.label:<{width}}  {r.value:>10.{decimals}f}{(' ' + unit) if unit else ''}")
            elif r.kind == "percent":
                part, whole = r.value, r.extra
                pct = (100.0 * part / whole) if whole else 0.0
                counts = f"{part:,} of {whole:,}".replace(",", " ") if isinstance(part, int) else ""
                out.append(f"  {r.label:<{width}}  {pct:>9.1f}%  {counts}".rstrip())
            elif r.kind == "output":
                out.append(f"  {r.label:<{width}}  {display_path(r.value)}")
        return out


class Terminal:
    """Prints lines, hiding any that contain free text from the HomeBank file."""

    def __init__(self, sensitive: Iterable[str] = (), stream: TextIO | None = None):
        self.sensitive = sorted({s for s in sensitive if s}, key=len)
        self.stream = stream or sys.stdout
        self.hidden = 0

    def _safe(self, text: str) -> str:
        for s in self.sensitive:
            if len(s) > len(text):
                break
            if s in text:
                self.hidden += 1
                return REDACTED
        return text

    def line(self, text: str = "") -> None:
        print(self._safe(text), file=self.stream)

    def summary(self, summary: Summary) -> None:
        for text in summary.render():
            self.line(text)


# --- paths ---------------------------------------------------------------------------------


def display_path(path: Path) -> str:
    """Show a path relative to the project folder when it is inside it."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def default_input(data_dir: Path = DATA_DIR) -> Path:
    """The single .xhb file in the data folder, if there is exactly one."""
    found = sorted(p for p in data_dir.glob("*.xhb") if p.is_file())
    if not found:
        raise HbkitError(
            "no .xhb file found in the data folder. Copy your HomeBank file there, "
            "or give its location with --file."
        )
    if len(found) > 1:
        raise HbkitError(
            f"{len(found)} .xhb files found in the data folder. Choose one with --file."
        )
    return found[0]


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def new_run_dir(out_root: Path, name: str) -> Path:
    """A fresh folder for one run: <out>/<name>/<date_time>/ (never reused)."""
    base = Path(out_root) / name
    stamp = timestamp()
    run = base / stamp
    n = 2
    while run.exists():
        run = base / f"{stamp}_{n}"
        n += 1
    run.mkdir(parents=True)
    return run


def write_error_log(out_root: Path, exc: BaseException) -> Path:
    """Write a full traceback (which may contain file values) to a private log file."""
    folder = Path(out_root) / "errors"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"error_{timestamp()}.log"
    path.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
    return path
