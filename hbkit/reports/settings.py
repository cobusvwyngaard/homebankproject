"""Report settings, read from a TOML file with the standard library's tomllib.

Default location: data/settings.toml (private, git-ignored). An example with
comments is in settings.example.toml at the top of the project.
"""

from __future__ import annotations

import csv
import io
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import HbkitError
from ..privacy import DATA_DIR, PROJECT_ROOT

DEFAULT_PATH = DATA_DIR / "settings.toml"
YEAR_MODES = ("calendar", "tax")


@dataclass
class Group:
    name: str
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Settings:
    year_mode: str = "calendar"
    exclude_accounts: list[str] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    cpi_file: Path | None = None
    min_yearly_spend: float = 1000.0
    path: Path | None = None  # the file these settings came from, if any

    def describe(self) -> str:
        return str(self.path) if self.path else "none (defaults)"


def _strings(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise HbkitError(f"settings: '{where}' must be a text or a list of texts")


def load_settings(path: str | Path | None = None) -> Settings:
    """Read settings from `path`, or data/settings.toml if it exists, else defaults."""
    if path is None:
        if not DEFAULT_PATH.is_file():
            return Settings()
        path = DEFAULT_PATH
    path = Path(path)
    if not path.is_file():
        raise HbkitError("the settings file was not found")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        # tomllib messages give a line and column, not file contents
        raise HbkitError(f"the settings file is not valid TOML ({e})") from None

    period = raw.get("period", {})
    mode = str(period.get("year_mode", "calendar")).strip().lower()
    if mode not in YEAR_MODES:
        raise HbkitError("settings: period.year_mode must be \"calendar\" or \"tax\"")
    accounts = raw.get("accounts", {})
    groups = []
    for name, spec in (raw.get("groups") or {}).items():
        if not isinstance(spec, dict):
            raise HbkitError("settings: every group must be a table with categories and/or tags")
        groups.append(Group(name, _strings(spec.get("categories"), "categories"),
                            _strings(spec.get("tags"), "tags")))
    inflation = raw.get("inflation", {})
    cpi = inflation.get("cpi_file")
    cpi_path = None
    if cpi:
        cpi_path = Path(str(cpi))
        if not cpi_path.is_absolute():
            candidates = [path.parent / cpi_path, PROJECT_ROOT / cpi_path]
            cpi_path = next((c for c in candidates if c.is_file()), candidates[0])
    reports = raw.get("reports", {})
    return Settings(
        year_mode=mode,
        exclude_accounts=_strings(accounts.get("exclude"), "accounts.exclude"),
        groups=groups,
        cpi_file=cpi_path,
        min_yearly_spend=float(reports.get("min_yearly_spend", 1000.0)),
        path=path.resolve(),
    )


_MONTH = re.compile(r"^\s*(\d{4})[-/]?(\d{1,2})(?:[-/]\d{1,2})?\s*$")


def read_cpi(path: Path) -> tuple[dict[str, float], int]:
    """Read a CPI table: rows of month (YYYY-MM, YYYY/MM, YYYYMM or YYYY-MM-DD) and index.

    Returns {"YYYY-MM": index} and the number of rows that could not be read.
    A header row, ';' separators and decimal commas are accepted.
    """
    if not path.is_file():
        raise HbkitError("the CPI file named in the settings was not found")
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text else ""
    delimiter = ";" if first.count(";") > first.count(",") else ","
    table, bad = {}, 0
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(row) < 2 or not any(c.strip() for c in row):
            continue
        m = _MONTH.match(row[0])
        value = row[1].strip().replace(" ", "")
        if delimiter == ";":
            value = value.replace(",", ".")
        try:
            index = float(value)
        except ValueError:
            index = None
        if not m or index is None or index <= 0 or not 1 <= int(m.group(2)) <= 12:
            if table or m:  # anything after the header that does not parse
                bad += 1
            continue
        table[f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"] = index
    if not table:
        raise HbkitError("the CPI file has no readable rows (expected: month, index)")
    return table, bad
