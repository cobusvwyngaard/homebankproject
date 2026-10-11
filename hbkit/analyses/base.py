"""The interface every analysis implements.

An analysis is one module in hbkit/analyses/ that defines a module-level
``ANALYSIS`` object, an instance of a subclass of :class:`Analysis`:

    class MyAnalysis(Analysis):
        name = "my-analysis"                    # used on the command line
        description = "One line shown by 'list'."
        params = (Param("min_count", int, 5, "minimum number of ..."),)

        def run(self, ctx: Context, params: dict) -> Summary:
            folder = ctx.new_run_dir()           # data/out/my-analysis/<date_time>/
            write_csv(folder / "details.csv", [...], rows)
            summary = Summary("My analysis")
            summary.count("Things found", 12)   # numbers only on screen
            summary.output("Details", folder)
            return summary

    ANALYSIS = MyAnalysis()

Parameters become command-line options automatically (``--min-count 3``).
An analysis may also offer extra top-level commands (``commands``), such as
rule mining's ``apply-rules``. See docs/adding-an-analysis.md.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..privacy import Summary, new_run_dir
from ..xhb.model import Book


@dataclass(frozen=True)
class Param:
    """One parameter of an analysis; becomes the option --<name with dashes>."""

    name: str
    type: type
    default: Any
    help: str
    choices: tuple | None = None

    @property
    def option(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass
class Context:
    """What an analysis gets to work with."""

    book: Book
    input_path: Path
    out_root: Path
    analysis_name: str = ""

    def new_run_dir(self) -> Path:
        return new_run_dir(self.out_root, self.analysis_name)


@dataclass(frozen=True)
class Command:
    """An extra top-level command contributed by an analysis."""

    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], tuple[Summary, Book | None]]


class Analysis:
    name: str = ""
    description: str = ""
    params: tuple[Param, ...] = ()
    commands: tuple[Command, ...] = ()

    def run(self, ctx: Context, params: dict[str, Any]) -> Summary:
        raise NotImplementedError

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}


# --- output helpers ---------------------------------------------------------------------------


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """Write a CSV that Excel opens correctly (UTF-8 with byte-order mark). Returns row count."""
    n = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
            n += 1
    return n


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0
