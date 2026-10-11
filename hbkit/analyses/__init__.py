"""Analysis plugins. Every module here that defines ``ANALYSIS`` is found automatically."""

from __future__ import annotations

import importlib
import pkgutil

from .base import Analysis


def discover() -> dict[str, Analysis]:
    """Import every analysis module and return the analyses by name."""
    found: dict[str, Analysis] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_") or info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        analysis = getattr(module, "ANALYSIS", None)
        if analysis is None:
            continue
        if not isinstance(analysis, Analysis):
            raise TypeError(f"{info.name}.ANALYSIS is not an Analysis instance")
        if not analysis.name or analysis.name in found:
            raise ValueError(f"analysis in module {info.name} has a missing or duplicate name")
        found[analysis.name] = analysis
    return found
