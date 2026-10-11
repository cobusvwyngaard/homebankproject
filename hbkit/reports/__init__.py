"""Report plugins. Every module here that defines ``REPORT`` is found automatically."""

from __future__ import annotations

import importlib
import pkgutil

from .base import Report

_SKIP = {"base", "data", "html", "settings"}


def discover() -> dict[str, Report]:
    found: dict[str, Report] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        report = getattr(module, "REPORT", None)
        if report is None:
            continue
        if not isinstance(report, Report) or not report.name or report.name in found:
            raise ValueError(f"report module {info.name} has a missing, wrong or duplicate REPORT")
        found[report.name] = report
    return found
