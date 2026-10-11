"""What the toolkit remembers about your import files, in data/import-accounts.json:

* which account each file was linked to, per month (the import record), so that a
  file with the same or a similar name is linked to the same account next time;
* which accounts you import every month (the checklist for a month).

Similar names: dates, copy numbers such as " (1)" and month names are left out when
names are compared, so "transact (1).csv" and "transact_20260531.csv" both count as
"transact.csv". Other numbers (an account or property number) are kept.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..errors import HbkitError

MEMORY_NAME = "import-accounts.json"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "januarie|februarie|maart|mei|junie|julie|augustus|oktober|desember|"
    "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|okt|nov|dec|des"
)
_DATES = [
    re.compile(r"(?<!\d)(?:19|20)\d{2}[-_. ]?(?:0[1-9]|1[0-2])[-_. ]?(?:0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-_. ](?:0?[1-9]|1[0-2])[-_. ](?:19|20)\d{2}(?!\d)"),
    re.compile(r"(?<!\d)(?:19|20)\d{2}[-_. ](?:0[1-9]|1[0-2])(?!\d)"),
    re.compile(rf"(?<![a-z])(?:{_MONTH_NAMES})(?![a-z])[-_. ]*(?:(?:19|20)\d{{2}})?"),
]


def check_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH.match(month):
        raise HbkitError("the month must look like 2026-04")
    return month


def name_key(filename: str) -> str:
    """A file name without what changes from month to month."""
    stem, ext = os.path.splitext(filename.strip().lower())
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"[\s_-]*(?:copy|kopie)(?:\s*\(?\d+\)?)?$", "", stem)
    for rx in _DATES:
        stem = rx.sub("#", stem)
    stem = re.sub(r"#[\s_.-]*\d{4,6}(?!\d)", "#", stem)  # a time after a date
    stem = stem.replace("#", " ")
    stem = re.sub(r"[\s_.-]+", "_", stem).strip("_")
    return (stem or "file") + ext


class ImportMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.expected_accounts: list[int] | None = None  # None: not chosen yet
        self.links: list[dict] = []
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                expected = data.get("expected_accounts")
                self.expected_accounts = None if expected is None else [int(k) for k in expected]
                self.links = [link for link in data.get("links", []) if isinstance(link, dict)]
            except (ValueError, TypeError, AttributeError):
                raise HbkitError(f"{MEMORY_NAME} in the data folder could not be read; rename or delete it "
                                 "to start a new import record") from None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "expected_accounts": self.expected_accounts,
                                   "links": self.links}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def link(self, month: str, file: str, account: int, account_name: str, **extra) -> dict:
        """Record (or replace) the account of a file in a month."""
        check_month(month)
        self.links = [x for x in self.links if not (x.get("month") == month and x.get("file") == file)]
        record = {"month": month, "file": file, "name_key": name_key(file), "account": int(account),
                  "account_name": account_name, "linked": datetime.now().isoformat(timespec="seconds"), **extra}
        self.links.append(record)
        return record

    def unlink(self, month: str, file: str) -> None:
        self.links = [x for x in self.links if not (x.get("month") == month and x.get("file") == file)]

    def get(self, month: str, file: str) -> dict | None:
        return next((x for x in self.links if x.get("month") == month and x.get("file") == file), None)

    def for_month(self, month: str) -> list[dict]:
        return [x for x in self.links if x.get("month") == month]

    def predict(self, file: str, month: str | None = None) -> tuple[int, str] | None:
        """(account, reason) from earlier links: the same name first, then a similar one.

        Links of the month itself are ignored, so the prediction comes from other months.
        """
        others = [x for x in self.links if x.get("month") != month]
        others.sort(key=lambda x: (x.get("month", ""), x.get("linked", "")))
        for x in reversed(others):
            if x.get("file") == file:
                return int(x["account"]), f"same file name as in {x['month']}"
        key = name_key(file)
        for x in reversed(others):
            if x.get("name_key") == key:
                return int(x["account"]), f"similar file name ({x['file']}) in {x['month']}"
        return None
