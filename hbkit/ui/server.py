"""A local browser interface for the toolkit (python -m hbkit ui).

Built on Python's own http.server; no dependencies. Security model:

* The server listens on 127.0.0.1 only, so it cannot be reached from the network.
* Every API request must carry a random key (generated at start-up and passed to the
  browser in the URL fragment, which browsers never send to servers or other sites).
* The Host header must be 127.0.0.1 or localhost (protects against DNS rebinding),
  and a request from another web page (Origin header) is refused.
* Files are only read from and written to the output folder (and the HomeBank file
  the user selected); paths from the browser are checked to stay inside it.
* Pages are sent with a strict Content-Security-Policy and contain no external
  resources, so nothing is loaded from or sent to the internet.

The page files are read from disk on every request, but the Python code is loaded
once at start-up. If the toolkit's files are replaced while it runs (an update), the
browser would get the new page from the old program and fail in odd ways ("unknown
request"), so every API request first checks that the toolkit's files are unchanged.

The browser interface shows the user's own data (memos, names), because it only runs
on their computer. The terminal stays aggregate-only.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..errors import HbkitError
from ..privacy import DATA_DIR, OUT_DIR, write_error_log

STATIC = Path(__file__).parent / "static"
#: The toolkit's own files, watched for updates made while the server runs.
CODE_DIR = Path(__file__).resolve().parent.parent
UPDATED_MESSAGE = ("The toolkit was updated while it was running. Close its black window (or press Ctrl+C "
                   "in PowerShell), start hb-ui.cmd again and open the new address.")
MAX_BODY = 20 * 1024 * 1024
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class UiState:
    """What the interface works on: the chosen HomeBank file and the output folder."""

    def __init__(self, out_root: Path = OUT_DIR, data_dir: Path = DATA_DIR, input_path: Path | None = None,
                 settings_path: Path | None = None):
        self.out_root = Path(out_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.input_path = Path(input_path).resolve() if input_path else None
        #: report settings file; None = data/settings.toml if it exists, else defaults
        self.settings_path = Path(settings_path).resolve() if settings_path else None
        self.lock = threading.RLock()  # one job at a time
        self._book = None
        self._book_stamp = None
        self._matches = None  # (key, RuleMatches, sha256 of the file) for the rule review page
        self._history = None  # (stamp, imports History) for the imports page
        if self.input_path is None:
            files = self.data_files()
            if len(files) == 1:
                self.input_path = files[0]

    def data_files(self) -> list[Path]:
        if not self.data_dir.is_dir():
            return []
        return sorted(p.resolve() for p in self.data_dir.glob("*.xhb") if p.is_file())

    def book(self):
        """The selected file, read again whenever it changes on disk."""
        from ..xhb.reader import read_file

        if self.input_path is None:
            raise HbkitError("choose a HomeBank file first")
        try:
            st = self.input_path.stat()
        except OSError:
            raise HbkitError("the HomeBank file was not found") from None
        stamp = (st.st_mtime_ns, st.st_size)
        if self._book is None or self._book_stamp != stamp:
            self._book = read_file(self.input_path)
            self._book_stamp = stamp
        return self._book

    def rule_matches(self, field_name: str, include_transfers: bool):
        """A RuleMatches for the selected file, kept until the file or the settings change."""
        from ..analyses.rule_mining import RuleMatches

        book = self.book()
        key = (self._book_stamp, field_name, include_transfers)
        if self._matches is None or self._matches[0] != key:
            self._matches = (key, RuleMatches.for_book(book, field_name, include_transfers),
                             hashlib.sha256(book.raw).hexdigest())
        return self._matches[1], self._matches[2]

    def import_history(self):
        """The selected file's history for import predictions, kept until the file changes."""
        from ..imports.predict import History

        book = self.book()
        if self._history is None or self._history[0] != self._book_stamp:
            self._history = (self._book_stamp, History(book))
        return book, self._history[1]

    def import_memory(self):
        from ..imports.memory import MEMORY_NAME, ImportMemory

        return ImportMemory(self.data_dir / MEMORY_NAME)

    def inside_out(self, rel: str) -> Path:
        """A path under the output folder; refuses anything that escapes it."""
        target = (self.out_root / rel).resolve()
        if target != self.out_root and not target.is_relative_to(self.out_root):
            raise ApiError(HTTPStatus.FORBIDDEN, "that location is outside the output folder")
        return target


# --- API handlers ---------------------------------------------------------------------------------


def _file_info(p: Path) -> dict:
    st = p.stat()
    return {"path": str(p), "name": p.name, "size": st.st_size, "modified": int(st.st_mtime)}


def api_status(state: UiState, q: dict, body: Any) -> dict:
    return {
        "version": __version__,
        "data_dir": str(state.data_dir),
        "out_root": str(state.out_root),
        "data_files": [_file_info(p) for p in state.data_files()],
        "selected": _file_info(state.input_path) if state.input_path and state.input_path.is_file() else None,
    }


def api_select(state: UiState, q: dict, body: Any) -> dict:
    path = Path(str(body.get("path", "")).strip().strip('"')).expanduser()
    if not path.is_file():
        raise HbkitError("no file was found at that location")
    if path.suffix.lower() != ".xhb":
        raise HbkitError("please choose a HomeBank file (.xhb)")
    with state.lock:
        previous = state.input_path, state._book, state._book_stamp
        state.input_path, state._book = path.resolve(), None
        try:
            state.book()
        except Exception:
            state.input_path, state._book, state._book_stamp = previous
            raise
    return api_structure(state, q, body)


def api_structure(state: UiState, q: dict, body: Any) -> dict:
    from ..xhb.structure import structure_report

    with state.lock:
        book = state.book()
    txns = book.transactions
    d = book.data_version
    return {
        "file": _file_info(state.input_path),
        "facts": {
            "file_version": book.version,
            "written_by": f"{d // 10000}.{(d // 100) % 100}.{d % 100}" if d else "unknown",
            "transactions": len(txns),
            "accounts": len(book.accounts),
            "categories": len(book.categories),
            "rules": len(book.rules),
            "first_year": min((t.date.year for t in txns if t.date), default=None),
            "last_year": max((t.date.year for t in txns if t.date), default=None),
            "warnings": sum(book.warnings.values()),
            "unknown_elements": len(book.unknown_elements),
        },
        "report": structure_report(book),
    }


def api_analyses(state: UiState, q: dict, body: Any) -> list:
    from ..analyses import discover

    return [{
        "name": a.name,
        "description": a.description,
        "params": [{"name": p.name, "option": p.option, "type": p.type.__name__, "default": p.default,
                    "help": p.help, "choices": list(p.choices) if p.choices else None} for p in a.params],
    } for a in discover().values()]


def _coerce(param, value: Any) -> Any:
    try:
        if param.type is bool:
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        if value is None or value == "":
            return param.default
        value = param.type(value)
    except (TypeError, ValueError):
        raise HbkitError(f"the value for '{param.name}' is not valid") from None
    if param.choices and value not in param.choices:
        raise HbkitError(f"the value for '{param.name}' is not one of the choices")
    return value


def api_run(state: UiState, q: dict, body: Any) -> dict:
    from ..analyses import discover
    from ..analyses.base import Context

    analyses = discover()
    analysis = analyses.get(str(body.get("analysis", "")))
    if analysis is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "unknown analysis")
    given = body.get("params") or {}
    params = {p.name: _coerce(p, given.get(p.name, p.default)) for p in analysis.params}
    with state.lock:
        book = state.book()
        ctx = Context(book=book, input_path=state.input_path, out_root=state.out_root, analysis_name=analysis.name)
        before = set(_run_dirs(state, analysis.name))
        summary = analysis.run(ctx, params)
        after = [p for p in _run_dirs(state, analysis.name) if p not in before]
    run = after[-1].relative_to(state.out_root).as_posix() if after else None
    return {"summary": summary.to_dict(), "run": run, "analysis": analysis.name}


def _run_dirs(state: UiState, analysis: str | None = None) -> list[Path]:
    from ..analyses import discover

    names = [analysis] if analysis else list(discover())
    out = []
    for name in names:
        base = state.out_root / name
        if base.is_dir():
            out += sorted(p for p in base.iterdir() if p.is_dir())
    return out


def api_runs(state: UiState, q: dict, body: Any) -> list:
    runs = []
    for d in _run_dirs(state):
        runs.append({
            "run": d.relative_to(state.out_root).as_posix(),
            "analysis": d.parent.name,
            "stamp": d.name,
            "files": sorted(p.name for p in d.iterdir() if p.suffix in (".csv", ".json", ".txt")),
        })
    runs.sort(key=lambda r: r["stamp"], reverse=True)
    return runs


def _run_file(state: UiState, run: str, name: str) -> Path:
    folder = state.inside_out(run)
    if not folder.is_dir() or folder == state.out_root:
        raise ApiError(HTTPStatus.NOT_FOUND, "that results folder does not exist")
    if not name or Path(name).name != name or Path(name).suffix not in (".csv", ".json", ".txt"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "not a results file")
    path = folder / name
    if not path.is_file():
        raise ApiError(HTTPStatus.NOT_FOUND, "that results file does not exist")
    return path


def api_table(state: UiState, q: dict, body: Any) -> dict:
    path = _run_file(state, q.get("run", ""), q.get("name", ""))
    if path.suffix != ".csv":
        return {"text": path.read_text(encoding="utf-8", errors="replace")}
    from ..analyses.rule_mining import read_review

    if path.name == "review.csv":
        rows = read_review(path)
        header = list(rows[0]) if rows else []
        return {"header": header, "rows": [[r.get(h, "") for h in header] for r in rows]}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        return {"header": header, "rows": list(reader)}


def api_review_matches(state: UiState, q: dict, body: Any) -> dict:
    """The transactions a rule text matches in the selected file (also for edited texts)."""
    from ..analyses.rule_mining import MATCH_HEADER

    meta_path = _run_file(state, q.get("run", ""), "summary.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        params = meta.get("parameters") or {}
    except ValueError:
        meta, params = {}, {}
    field_name = params.get("field") if params.get("field") in ("memo", "payee") else "memo"
    text = q.get("text", "")
    with state.lock:
        matches, sha = state.rule_matches(field_name, bool(params.get("include_transfers")))
        category = matches.book.category_by_path(q.get("category", ""))
        rows = matches.rows(text, category.key if category else 0)
    return {
        "header": MATCH_HEADER,
        "rows": rows,
        "field": field_name,
        "category_found": category is not None,
        "file_changed": bool(meta.get("source_sha256")) and meta.get("source_sha256") != sha,
    }


def api_categories(state: UiState, q: dict, body: Any) -> list:
    with state.lock:
        book = state.book()
    return sorted(filter(None, (book.category_path(k) for k in book.categories)), key=str.casefold)


def _review_with_edits(state: UiState, body: Any) -> tuple[Path, list[dict]]:
    """Load review.csv of a run, apply the browser's edits and save it again."""
    from ..analyses.base import write_csv
    from ..analyses.rule_mining import read_review

    path = _run_file(state, str(body.get("run", "")), "review.csv")
    rows = read_review(path)
    edits = {str(e.get("rule_id")): e for e in body.get("edits") or [] if isinstance(e, dict)}
    for row in rows:
        e = edits.get(row.get("rule_id", ""))
        if e is None:
            continue
        row["approve"] = "y" if e.get("approve") else ""
        for key in ("rule_text", "category"):
            if key in e and isinstance(e[key], str):
                row[key] = e[key].strip()
    header = list(rows[0]) if rows else []
    write_csv(path, header, ([r.get(h, "") for h in header] for r in rows))
    return path, rows


def api_review_save(state: UiState, q: dict, body: Any) -> dict:
    with state.lock:
        _, rows = _review_with_edits(state, body)
    return {"saved": len(rows), "approved": sum(1 for r in rows if r.get("approve"))}


def api_review_apply(state: UiState, q: dict, body: Any) -> dict:
    from ..analyses.rule_mining import apply_review_rows, default_output

    with state.lock:
        path, rows = _review_with_edits(state, body)
        book = state.book()
        summary, output = apply_review_rows(rows, book, state.input_path, default_output(state.out_root), path.parent)
    return {"summary": summary.to_dict(), "output": str(output) if output else None}


def api_export_sqlite(state: UiState, q: dict, body: Any) -> dict:
    from ..export.sqlite import export_sqlite
    from ..privacy import Summary

    with state.lock:
        book = state.book()
        out = state.out_root / "homebank.sqlite"
        from ..reports.settings import DEFAULT_PATH

        has_settings = state.settings_path is not None or DEFAULT_PATH.is_file()
        counts = export_sqlite(book, out, _settings(state) if has_settings else None)
    s = Summary("SQLite export")
    for table in ("transactions", "splits", "accounts", "categories", "payees", "tags", "rules", "templates"):
        s.count(table, counts[table])
    s.output("Database", out)
    return {"summary": s.to_dict(), "output": str(out)}


def open_in_file_manager(path: Path) -> None:  # replaced in tests; opens folders and files alike
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


# --- reports ---------------------------------------------------------------------------------------


def _settings(state: UiState):
    from ..reports.settings import DEFAULT_PATH, load_settings

    if state.settings_path is not None:
        return load_settings(state.settings_path)
    return load_settings(DEFAULT_PATH) if DEFAULT_PATH.is_file() else load_settings(None)


def api_reports(state: UiState, q: dict, body: Any) -> dict:
    import importlib.util

    from ..reports import discover
    from ..reports.html import INSTALL_HINT

    info: dict[str, Any] = {"plotly": importlib.util.find_spec("plotly") is not None, "install_hint": INSTALL_HINT}
    try:
        s = _settings(state)
        info["settings"] = {
            "path": str(s.path) if s.path else None, "year_mode": s.year_mode,
            "excluded_accounts": s.exclude_accounts, "groups": [g.name for g in s.groups],
            "cpi_file": str(s.cpi_file) if s.cpi_file else None, "error": None,
        }
    except HbkitError as e:
        info["settings"] = {"error": str(e)}
    info["reports"] = [{
        "name": r.name, "description": r.description, "period_kind": r.period_kind,
        "needs_charts": r.needs_charts,
        "params": [{"name": p.name, "option": p.option, "type": p.type.__name__, "default": p.default,
                    "help": p.help, "choices": list(p.choices) if p.choices else None} for p in r.params],
    } for r in discover().values()]
    folder = state.out_root / "reports"
    files = sorted(folder.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    info["files"] = [_file_info(p) for p in files[:200]]
    return info


def _year(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HbkitError(f"the {name} year is not a number") from None


def api_report_run(state: UiState, q: dict, body: Any) -> dict:
    from ..reports import discover
    from ..reports.base import run_report

    report = discover().get(str(body.get("report", "")))
    if report is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "unknown report")
    given = body.get("params") or {}
    params = {p.name: _coerce(p, given.get(p.name, p.default)) for p in report.params}
    with state.lock:
        book = state.book()
        summary, path = run_report(report, book, state.input_path, _settings(state), state.out_root, params,
                                   year=_year(body.get("year"), "chosen"), start=_year(body.get("from"), "first"),
                                   end=_year(body.get("to"), "last"))
    return {"summary": summary.to_dict(), "output": str(path)}


# --- imports ---------------------------------------------------------------------------------------


def _file_dict(info) -> dict:
    d = dict(vars(info))
    d["first"] = info.first.isoformat() if info.first else ""
    d["last"] = info.last.isoformat() if info.last else ""
    return d


def _account(book, value: Any) -> int:
    try:
        key = int(value)
    except (TypeError, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "choose an account") from None
    if key not in book.accounts:
        raise ApiError(HTTPStatus.BAD_REQUEST, "that account does not exist in the HomeBank file")
    return key


def api_import_month(state: UiState, q: dict, body: Any) -> dict:
    """Everything the imports page shows for one month."""
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(q.get("month", ""))
    with state.lock:
        book, history = state.import_history()
        memory = state.import_memory()
        accounts = S.month_accounts(book, history, memory, month)
        files, problems = [], []
        for path in S.month_files(state.out_root, month):
            try:
                files.append(_file_dict(S.file_info(book, history, memory, month, path)))
            except HbkitError as e:
                problems.append({"name": path.name, "error": str(e)})
    linked: dict[int, list] = {}
    for f in files:
        if f["account"]:
            linked.setdefault(f["account"], []).append({"name": f["name"], "prepared": bool(f["prepared"])})
    for a in accounts:
        a["files"] = linked.get(a["key"], [])
    record = sorted(memory.links, key=lambda x: (x.get("month", ""), x.get("linked", "")), reverse=True)
    return {"month": month, "accounts": accounts, "files": files, "unreadable": problems, "record": record,
            "folder": str(S.month_dir(state.out_root, month).relative_to(state.out_root).as_posix()),
            "expected_chosen": memory.expected_accounts is not None}


def api_import_expected(state: UiState, q: dict, body: Any) -> dict:
    keys = body.get("accounts")
    if not isinstance(keys, list):
        raise ApiError(HTTPStatus.BAD_REQUEST, "accounts must be a list")
    with state.lock:
        book = state.book()
        memory = state.import_memory()
        memory.expected_accounts = sorted({_account(book, k) for k in keys})
        memory.save()
    return {"expected": memory.expected_accounts}


def api_import_upload(state: UiState, q: dict, body: Any) -> dict:
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(str(body.get("month", "")))
    try:
        raw = base64.b64decode(str(body.get("content", "")), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "the file could not be received") from None
    with state.lock:
        path = S.add_file(state.out_root, month, str(body.get("name", "")), raw)
    return {"name": path.name}


def api_import_link(state: UiState, q: dict, body: Any) -> dict:
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(str(body.get("month", "")))
    with state.lock:
        book = state.book()
        path = S.original(state.out_root, month, str(body.get("file", "")))
        memory = state.import_memory()
        if body.get("account") in (None, ""):
            memory.unlink(month, path.name)
        else:
            key = _account(book, body.get("account"))
            memory.link(month, path.name, key, book.account_name(key) or "", sha256=S.sha256(path.read_bytes()))
        memory.save()
    return {"linked": body.get("account")}


def api_import_remove(state: UiState, q: dict, body: Any) -> dict:
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(str(body.get("month", "")))
    with state.lock:
        path = S.original(state.out_root, month, str(body.get("file", "")))
        prepared = path.parent.parent / "prepared" / S.prepared_name(path.name)
        path.unlink()
        prepared.unlink(missing_ok=True)
        memory = state.import_memory()
        memory.unlink(month, path.name)
        memory.save()
    return {"removed": path.name}


def _import_file(state: UiState, month: str, name: str):
    from ..imports import session as S
    from ..imports.formats import read_import_file

    path = S.original(state.out_root, month, name)
    return path, read_import_file(path.name, path.read_bytes())


def api_import_preview(state: UiState, q: dict, body: Any) -> dict:
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(q.get("month", ""))
    with state.lock:
        book, history = state.import_history()
        key = _account(book, q.get("account"))
        path, f = _import_file(state, month, q.get("file", ""))
        rows = S.preview(book, history, f, key)
    return {"file": path.name, "format": f.format, "account": key, "account_name": book.account_name(key),
            "rows": rows, "problems": f.problems, "date_order": f.date_order}


def api_import_prepare(state: UiState, q: dict, body: Any) -> dict:
    from ..imports import session as S
    from ..imports.memory import check_month

    month = check_month(str(body.get("month", "")))
    choices = body.get("categories")
    if not isinstance(choices, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "categories must be given per transaction")
    with state.lock:
        book = state.book()
        key = _account(book, body.get("account"))
        path, f = _import_file(state, month, str(body.get("file", "")))
        try:
            parsed = {int(i): str(v) for i, v in choices.items()}
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid transaction number") from None
        data, n = S.prepare(book, f, parsed)
        target = S.save_prepared(path, data)
        memory = state.import_memory()
        memory.link(month, path.name, key, book.account_name(key) or "", sha256=S.sha256(path.read_bytes()),
                    transactions=len(f.txns), categorised=n,
                    prepared=target.relative_to(state.out_root).as_posix())
        memory.save()
    return {"path": str(target), "rel": target.relative_to(state.out_root).as_posix(), "categorised": n,
            "transactions": len(f.txns), "account_name": book.account_name(key)}


def api_open(state: UiState, q: dict, body: Any) -> dict:
    """Show a results folder in Windows Explorer, or open a report page in the browser."""
    target = state.inside_out(str(body.get("path", "")))
    if body.get("file"):
        if not target.is_file() or target.suffix.lower() not in (".html", ".csv"):
            raise ApiError(HTTPStatus.NOT_FOUND, "that report does not exist")
        open_in_file_manager(target)
        return {"opened": str(target)}
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        raise ApiError(HTTPStatus.NOT_FOUND, "that folder does not exist")
    open_in_file_manager(target)
    return {"opened": str(target)}


ROUTES = {
    ("GET", "/api/status"): api_status,
    ("POST", "/api/select"): api_select,
    ("GET", "/api/structure"): api_structure,
    ("GET", "/api/analyses"): api_analyses,
    ("POST", "/api/run"): api_run,
    ("GET", "/api/runs"): api_runs,
    ("GET", "/api/table"): api_table,
    ("GET", "/api/categories"): api_categories,
    ("GET", "/api/review/matches"): api_review_matches,
    ("POST", "/api/review/save"): api_review_save,
    ("POST", "/api/review/apply"): api_review_apply,
    ("POST", "/api/export-sqlite"): api_export_sqlite,
    ("POST", "/api/open"): api_open,
    ("GET", "/api/reports"): api_reports,
    ("POST", "/api/report/run"): api_report_run,
    ("GET", "/api/import/month"): api_import_month,
    ("POST", "/api/import/expected"): api_import_expected,
    ("POST", "/api/import/upload"): api_import_upload,
    ("POST", "/api/import/link"): api_import_link,
    ("POST", "/api/import/remove"): api_import_remove,
    ("GET", "/api/import/preview"): api_import_preview,
    ("POST", "/api/import/prepare"): api_import_prepare,
}


# --- HTTP plumbing ------------------------------------------------------------------------------------


def code_fingerprint() -> tuple:
    """Name, size and modification time of every toolkit file (not Python's caches)."""
    files = []
    for p in sorted(CODE_DIR.rglob("*")):
        if "__pycache__" not in p.parts and p.is_file():
            st = p.stat()
            files.append((p.relative_to(CODE_DIR).as_posix(), st.st_size, st.st_mtime_ns))
    return tuple(files)


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: UiState, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.state = state
        self.token = secrets.token_urlsafe(24)
        self.code = code_fingerprint()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "hbkit"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    # security checks ------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.lower() in (f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}")

    def _token_ok(self) -> bool:
        given = self.headers.get("X-Hbkit-Token") or ""
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    # responses -----------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        file = (STATIC / name).resolve()
        if not file.is_relative_to(STATIC.resolve()) or not file.is_file() or file.suffix not in CONTENT_TYPES:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send(HTTPStatus.OK, file.read_bytes(), CONTENT_TYPES[file.suffix])

    def _handle(self, method: str) -> None:
        if not self._host_ok() or not self._origin_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "refused"})
            return
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            if method == "GET":
                self._static(url.path)
            else:
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "not allowed"})
            return
        if not self._token_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong key; open the address printed in PowerShell"})
            return
        if code_fingerprint() != self.server.code:
            self._json(HTTPStatus.CONFLICT, {"error": UPDATED_MESSAGE})
            return
        handler = ROUTES.get((method, url.path))
        if handler is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown request"})
            return
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body: Any = {}
        if method == "POST":
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON expected"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
                return
        try:
            self._json(HTTPStatus.OK, handler(self.server.state, query, body))
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except HbkitError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log = write_error_log(self.server.state.out_root, e)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": f"Unexpected error ({type(e).__name__}: {e}). Details: {log}",
                "trace": traceback.format_exception_only(e)[-1].strip(),
            })

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def make_server(state: UiState, port: int = 0) -> UiServer:
    return UiServer(state, port)
