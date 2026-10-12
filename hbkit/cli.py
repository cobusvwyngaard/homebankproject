"""Command-line interface: python -m hbkit <command> [options].

Run ``python -m hbkit --help`` for the list of commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import HbkitError
from .privacy import DATA_DIR, OUT_DIR, Summary, Terminal, default_input, display_path, save_target, write_error_log


SAMPLE_SETTINGS = """# Settings for the synthetic sample file (made by generate-sample --reporting).
[period]
year_mode = "tax"

[accounts]
exclude = ["Business account"]

[groups."tax: rental – property A"]
categories = ["Rental A"]
tags = ["propA"]

[groups."tax: medical"]
categories = ["Medical"]
tags = ["medical"]

[groups.essential]
categories = ["Groceries", "Utilities", "Transport:Fuel", "Medical:Medical aid"]

[groups.discretionary]
categories = ["Eating out", "Entertainment", "Clothing", "Gifts"]

[inflation]
cpi_file = "{cpi}"
"""


def _input_path(args: argparse.Namespace) -> Path:
    return Path(args.file) if args.file else default_input()


def _default_settings_exists() -> bool:
    from .reports.settings import DEFAULT_PATH

    return DEFAULT_PATH.is_file()


def _add_file_option(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--file", metavar="PATH",
        help="the HomeBank .xhb file to read (default: the only .xhb file in the data folder)",
    )


def _add_out_option(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--out", metavar="FOLDER", default=str(OUT_DIR),
        help="folder for detailed results (default: data/out in the project folder)",
    )


# --- commands --------------------------------------------------------------------------------


def cmd_structure(args: argparse.Namespace) -> int:
    from .xhb.reader import read_file
    from .xhb.structure import structure_report

    book = read_file(_input_path(args))
    term = Terminal(book.sensitive_strings())
    for line in structure_report(book):
        term.line(line)
    return 0


def cmd_export_sqlite(args: argparse.Namespace) -> int:
    from .export.sqlite import export_sqlite
    from .xhb.reader import read_file

    book = read_file(_input_path(args))
    out = Path(args.output) if args.output else Path(args.out) / "homebank.sqlite"
    settings = None
    if args.settings or _default_settings_exists():
        from .reports.settings import load_settings

        settings = load_settings(args.settings)
    counts = export_sqlite(book, out, settings)
    s = Summary("SQLite export")
    s.heading("Rows written")
    for table in ("transactions", "splits", "accounts", "categories", "payees", "tags", "rules", "templates"):
        s.count(table, counts[table])
    if book.warnings:
        s.count("Reader warnings (see the structure command)", sum(book.warnings.values()))
    s.output("Database", out)
    s.note("The schema is described in docs/sqlite-schema.md.")
    Terminal(book.sensitive_strings()).summary(s)
    return 0


def cmd_prepare_import(args: argparse.Namespace) -> int:
    from .imports import session as S
    from .imports.formats import read_import_file
    from .imports.memory import MEMORY_NAME, ImportMemory, check_month
    from .imports.predict import History, clear_winner, guess_accounts
    from .xhb.reader import read_file

    source = Path(args.input)
    if not source.is_file():
        raise HbkitError("the bank file was not found")
    raw = source.read_bytes()
    f = read_import_file(source.name, raw)
    book = read_file(_input_path(args))
    history = History(book)
    last = f.last_date
    month = check_month(args.month) if args.month else (f"{last.year:04d}-{last.month:02d}" if last else None)
    if month is None:
        raise HbkitError("the file has no readable dates; give the month with --month")
    memory = ImportMemory(Path(args.data) / MEMORY_NAME)
    if args.account:
        wanted = args.account.strip()
        matches = [a for a in book.accounts.values()
                   if (wanted.isdigit() and a.key == int(wanted)) or a.name.casefold() == wanted.casefold()]
        if len(matches) != 1:
            raise HbkitError("that account was not found in the HomeBank file (give its name as in HomeBank)")
        account, how = matches[0].key, "given with --account"
    else:
        remembered = memory.predict(source.name, month)
        best = clear_winner(guess_accounts(history, f))
        by_number = S.account_by_number(book, f.numbers) if f.numbers else None
        if by_number is not None:
            account, how = by_number, "recognised by the account number in the statement"
        elif remembered and remembered[0] in book.accounts:
            account, how = remembered[0], "remembered for this file name"
        elif best:
            account, how = best.account, "recognised from its contents"
        else:
            raise HbkitError("could not tell which account this file belongs to; give it with --account")
    rows = S.preview(book, history, f, account)
    choices = {r["index"]: r["suggested"] for r in rows if r["suggested"]}
    data, n = S.prepare(book, f, choices)
    original = S.add_file(Path(args.out), month, source.name, raw)
    target = S.save_prepared(original, data)
    memory.link(month, original.name, account, book.account_name(account) or "", sha256=S.sha256(raw),
                transactions=len(f.txns), categorised=n,
                prepared=target.relative_to(Path(args.out)).as_posix())
    memory.save()

    s = Summary("Prepare an import file")
    s.count("Transactions in the file", len(f.txns))
    s.note(f"Account: {how}.")
    s.heading("Categories written into the prepared copy")
    s.count("From this account's history", sum(1 for r in rows if r["suggested"] and r["source"] == "this account"))
    s.count("From all accounts", sum(1 for r in rows if r["suggested"] and r["source"] == "all accounts"))
    open_rows = [r for r in rows if not r["skip"] and not r["suggested"]]
    s.count("Left to your HomeBank rules", sum(1 for r in open_rows if r["homebank"]))
    s.count("Without a category", sum(1 for r in open_rows if not r["homebank"]))
    s.count("Transfers, splits or already categorised (left as they are)", sum(1 for r in rows if r["skip"]))
    s.heading("Checks")
    s.count("Possibly already in HomeBank", sum(1 for r in rows if r["duplicate"]))
    s.count("Replaced on import by an overwrite rule", sum(
        1 for r in rows if r["suggested"] and r["overwritten_by"] and r["overwrite_category"] != r["suggested"]))
    if f.problems:
        s.count("Lines HomeBank may not read as transactions", len(f.problems))
    s.heading("Output")
    s.output("Prepared copy", target)
    s.note("Import the prepared copy in HomeBank (File > Import) into the same account. Details per", terminal_only=True)
    s.note("transaction are on the Imports page of the browser interface (python -m hbkit ui).", terminal_only=True)
    Terminal(book.sensitive_strings()).summary(s)
    return 0


def cmd_import_files(args: argparse.Namespace) -> int:
    from .analyses.base import write_csv
    from .imports import session as S
    from .imports.bulk import IMPORTABLE, default_output, import_transactions, plan_import
    from .imports.memory import MEMORY_NAME, ImportMemory, check_month
    from .imports.predict import History
    from .privacy import new_run_dir
    from .xhb.reader import read_file

    month = check_month(args.month)
    input_path = _input_path(args)
    book = read_file(input_path)
    term = Terminal(book.sensitive_strings())
    memory = ImportMemory(Path(args.data) / MEMORY_NAME)
    sources, not_linked = S.month_sources(book, History(book), memory, Path(args.out), month)
    if not sources:
        raise HbkitError("no files of that month are linked to an account (add them with prepare-import "
                         "or on the Imports page)")
    plan = plan_import(book, sources, days=args.days, run_rules=not args.no_rules,
                       payee_defaults=args.payee_defaults)
    folder = new_run_dir(Path(args.out), "import-files")
    write_csv(folder / "transactions.csv",
              ["file", "account", "date", "amount", "memo", "payee", "category", "status", "imported", "notes"],
              ([plan.sources[c.source].name, book.account_name(c.account), c.date, c.txn.amount, c.txn.memo,
                (book.payee_name(c.payee_key) or "") if c.payee_key else c.payee_name,
                book.category_path(c.category_key) or "", c.status, "yes" if c.status == "new" else "no",
                "; ".join(c.notes)] for c in plan.candidates))
    output, backups = save_target(Path(args.out), args.output, args.in_place, default_output)
    summary, path = import_transactions(book, plan, None, input_path, output, plan.source_sha256, backups)
    if path is not None:
        for i, src in enumerate(plan.sources):
            n = sum(1 for c in plan.candidates if c.source == i and c.status == "new")
            if n:
                memory.mark_imported(month, src.name, transactions=n, into=path.name, in_place=output is None)
        memory.save()
    if not_linked:
        summary.count("Files not linked to an account (left out)", len(not_linked))
    summary.output("List of every transaction and what happened to it", folder / "transactions.csv")
    if any(c.status in IMPORTABLE and c.status != "new" for c in plan.candidates):
        summary.note("Transactions that look like ones already in HomeBank were left out; to import some of", terminal_only=True)
        summary.note("them anyway, use the Imports page of the browser interface (python -m hbkit ui).", terminal_only=True)
    term.summary(summary)
    return 0


TRANSFER_HEADER = [
    "approve", "kind", "group", "out_index", "out_date", "out_account", "out_amount", "out_memo",
    "in_index", "in_date", "in_account", "in_amount", "in_memo", "days_apart", "signs",
]


def cmd_link_transfers(args: argparse.Namespace) -> int:
    import hashlib
    import json

    from .analyses.base import write_csv, write_json
    from .analyses.rule_mining import APPROVED, read_review
    from .imports.transfers import default_output, find_transfers, link_transfers
    from .privacy import new_run_dir
    from .xhb.reader import read_file

    input_path = _input_path(args)
    book = read_file(input_path)
    term = Terminal(book.sensitive_strings())
    if args.review:
        review = Path(args.review)
        if review.is_dir():
            review = review / "transfers.csv"
        if not review.is_file():
            raise HbkitError("transfers.csv was not found (give the file or the folder that contains it)")
        rows = read_review(review, ("approve", "out_index", "in_index"))
        meta_path = review.parent / "summary.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        approved = [r for r in rows if r.get("approve", "").strip().lower() in APPROVED]
        try:
            pairs = [(int(r["out_index"]), int(r["in_index"])) for r in approved]
        except ValueError:
            raise HbkitError("out_index and in_index must be whole numbers") from None
        output, backups = save_target(Path(args.out), args.output, args.in_place, default_output)
        summary, _ = link_transfers(book, pairs, int(meta.get("days", args.days)), input_path, output,
                                    meta.get("source_sha256"), backups)
        term.summary(summary)
        return 0

    result = find_transfers(book, args.days)
    folder = new_run_dir(Path(args.out), "transfers")

    def row(kind, group, p):
        return ["", kind, group, p.out.index, p.out.date, book.account_name(p.out.account_key), p.out.amount,
                p.out.memo, p.into.index, p.into.date, book.account_name(p.into.account_key), p.into.amount,
                p.into.memo, p.gap, "; ".join(p.signs)]

    write_csv(folder / "transfers.csv", TRANSFER_HEADER,
              [row("pair", "", p) for p in result.pairs]
              + [row("choice", i, p) for i, group in enumerate(result.choices, start=1) for p in group])
    write_csv(folder / "without_other_side.csv",
              ["date", "account", "amount", "memo", "why_it_looks_like_a_transfer", "likely_other_account"],
              ([x.txn.date, book.account_name(x.txn.account_key), x.txn.amount, x.txn.memo, "; ".join(x.reasons),
                book.account_name(x.likely_account) or "" if x.likely_account else ""] for x in result.lonely))
    write_json(folder / "summary.json", {"days": result.days, "source_sha256": hashlib.sha256(book.raw).hexdigest(),
                                         "pairs": len(result.pairs), "choice_groups": len(result.choices),
                                         "without_other_side": len(result.lonely)})
    s = Summary("Find transfers")
    s.count("Transactions without a category considered", result.considered)
    s.count("Pairs", len(result.pairs))
    s.count("Groups with more than one possible partner", len(result.choices))
    s.count("Without another side (handle by hand)", len(result.lonely))
    s.output("Folder", folder)
    s.note("Next: put y in the approve column of transfers.csv for the pairs to link (for a group,", terminal_only=True)
    s.note("only one row per transaction), save, then run", terminal_only=True)
    s.note("python -m hbkit link-transfers --review <that folder>", terminal_only=True)
    s.note("(or use the Transfers page of the browser interface: python -m hbkit ui)", terminal_only=True)
    term.summary(s)
    return 0


def cmd_fill_categories(args: argparse.Namespace) -> int:
    import json

    from .analyses.base import write_csv, write_json
    from .analyses.rule_mining import APPROVED, read_review
    from .privacy import new_run_dir
    from .rules.fill import default_output, fill_categories, plan_fill
    from .xhb import format as F
    from .xhb.reader import read_file

    input_path = _input_path(args)
    book = read_file(input_path)
    term = Terminal(book.sensitive_strings())
    if args.review:
        review = Path(args.review)
        if review.is_dir():
            review = review / "rules.csv"
        if not review.is_file():
            raise HbkitError("rules.csv was not found (give the file or the folder that contains it)")
        rows = read_review(review, ("approve", "rule_id"))
        meta_path = review.parent / "summary.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        chosen = [r["rule_id"] for r in rows if r.get("approve", "").strip().lower() in APPROVED]
        output, backups = save_target(Path(args.out), args.output, args.in_place, default_output)
        summary, _ = fill_categories(book, chosen, input_path, output, meta.get("source_sha256"), backups)
        term.summary(summary)
        return 0

    plan = plan_fill(book)
    everything = plan.decide({r.id for r in plan.rules})
    matches = plan.matches()
    filled_by: dict[str, int] = {}
    for rule, _ in everything.values():
        filled_by[rule.id] = filled_by.get(rule.id, 0) + 1
    folder = new_run_dir(Path(args.out), "fill-categories")
    write_csv(folder / "rules.csv", ["approve", "rule_id", "position", "rule_text", "field", "action", "category",
                                     "matches_empty", "fills_if_all_approved"], (
        ["", r.id, plan.positions[r.id], r.search, F.RULE_FIELD_NAMES.get(r.match_field, ""),
         "overwrite" if r.flags & F.ASGF_OVWCAT else "set if empty", book.category_path(r.category_key),
         matches.get(r.id, 0), filled_by.get(r.id, 0)]
        for r in plan.rules))
    write_csv(folder / "transactions.csv", ["date", "account", "memo", "payee", "amount", "matching_rules",
                                            "filled_by_if_all_approved", "category_if_all_approved"], (
        [t.date, book.account_name(t.account_key), t.memo, book.payee_name(t.payee_key) or "", t.amount,
         " ".join(plan.rules[ri].id for ri in rule_ids),
         everything[i][0].id if i in everything else "",
         book.category_path(everything[i][1]) if i in everything else ""]
        for i, (t, rule_ids) in enumerate(zip(plan.txns, plan.matched)) if rule_ids))
    write_json(folder / "summary.json", {"source_sha256": plan.source_sha256, "without_category": len(plan.txns),
                                         "rules": len(plan.rules), "fills_if_all_approved": len(everything)})
    s = Summary("Fill empty categories")
    s.count("Transactions without a category (open accounts)", len(plan.txns))
    if plan.closed_skipped:
        s.count("  left alone in closed accounts", plan.closed_skipped)
    s.count("Rules that can set a category", len(plan.rules))
    for reason, n in sorted(plan.left_out_rules.items()):
        s.count(f"  left out: {reason}", n)
    s.count("Would be filled if all rules are approved", len(everything))
    s.output("Folder", folder)
    s.note("Next: put y in the approve column of rules.csv for the rules to apply, save, then run", terminal_only=True)
    s.note("python -m hbkit fill-categories --review <that folder>", terminal_only=True)
    s.note("(or use the Fill categories page of the browser interface: python -m hbkit ui)", terminal_only=True)
    term.summary(s)
    return 0


def cmd_generate_sample(args: argparse.Namespace) -> int:
    from .fixtures.generator import GeneratorOptions, generate

    out = Path(args.output) if args.output else Path(args.out) / "sample.xhb"
    if out.exists() and not args.overwrite:
        raise HbkitError("the output file already exists; choose another --output or add --overwrite")
    opts = GeneratorOptions(seed=args.seed, start_year=args.start_year, years=args.years,
                            sentinel=args.sentinel, reporting=args.reporting,
                            home_loans=args.home_loans, unlinked_transfers=args.unlinked_transfers,
                            uncategorised=args.uncategorised)
    gen = generate(opts)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gen.data)
    s = Summary("Synthetic sample file")
    s.note("Entirely fictional data, for trying the toolkit out safely.")
    s.count("Transactions", gen.transactions)
    s.count("Years", args.years)
    s.output("Written to", out)
    if args.reporting:
        from .fixtures.generator import write_cpi

        cpi = out.with_name(out.stem + "-cpi.csv")
        settings = out.with_name(out.stem + "-settings.toml")
        write_cpi(cpi, start_year=args.start_year, years=args.years)
        settings.write_text(SAMPLE_SETTINGS.format(cpi=cpi.name), encoding="utf-8")
        s.output("Sample CPI table", cpi)
        s.output("Sample settings", settings)
        s.note(f"Try: python -m hbkit report run annual-overview --file {out} --settings {settings}")
    Terminal().summary(s)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import webbrowser

    from .ui.server import UiState, make_server

    state = UiState(out_root=Path(args.out), input_path=Path(args.file) if args.file else None,
                    settings_path=Path(args.settings) if args.settings else None)
    server = make_server(state, args.port)
    term = Terminal()
    term.line("The HomeBank toolkit is running in your browser at:")
    term.line(f"  {server.url}")
    term.line("If the browser does not open by itself, copy that address into it.")
    term.line("It only works on this computer. Keep this window open; press Ctrl+C here to stop.")
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    term.line("Stopped.")
    return 0


def cmd_report_list(args: argparse.Namespace) -> int:
    from .reports import discover

    term = Terminal()
    term.line("Reports (run with: python -m hbkit report run <name> [--year Y | --from Y1 --to Y2]):")
    for name, report in discover().items():
        term.line(f"  {name:<20} {report.description}")
    return 0


def _run_report(args: argparse.Namespace) -> int:
    from .reports.base import run_report
    from .reports.settings import load_settings
    from .xhb.reader import read_file

    report = args.report
    path = _input_path(args)
    book = read_file(path)
    settings = load_settings(args.settings)
    params = {p.name: getattr(args, p.name) for p in report.params}
    summary, _ = run_report(report, book, path, settings, Path(args.out), params,
                            year=args.year, start=args.start, end=args.end)
    Terminal(book.sensitive_strings()).summary(summary)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .analyses import discover

    term = Terminal()
    term.line("Analyses (run with: python -m hbkit run <name> [options]):")
    for name, analysis in discover().items():
        term.line(f"  {name:<16} {analysis.description}")
        for c in analysis.commands:
            term.line(f"    related command: {c.name} - {c.help}")
    term.line("")
    term.line("Options of one analysis: python -m hbkit run <name> --help")
    return 0


def _run_analysis(args: argparse.Namespace) -> int:
    from .analyses.base import Context
    from .xhb.reader import read_file

    analysis = args.analysis
    path = _input_path(args)
    book = read_file(path)
    params = {p.name: getattr(args, p.name) for p in analysis.params}
    ctx = Context(book=book, input_path=path, out_root=Path(args.out), analysis_name=analysis.name)
    summary = analysis.run(ctx, params)
    Terminal(book.sensitive_strings()).summary(summary)
    return 0


def _command_handler(command):
    def handler(args: argparse.Namespace) -> int:
        summary, book = command.run(args)
        Terminal(book.sensitive_strings() if book else ()).summary(summary)
        return 0

    return handler


def _add_param(p: argparse.ArgumentParser, param) -> None:
    if param.type is bool:
        p.add_argument(param.option, dest=param.name, action=argparse.BooleanOptionalAction,
                       default=param.default, help=param.help)
    else:
        p.add_argument(param.option, dest=param.name, type=param.type, default=param.default,
                       choices=param.choices, help=f"{param.help} (default: {param.default})")


# --- parser ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hbkit",
        description="Analyse a HomeBank (.xhb) file locally. Only counts and percentages are "
                    "shown on screen; detailed results go to data/out.",
    )
    parser.add_argument("--version", action="version", version=f"hbkit {__version__}")
    parser.add_argument("--debug", action="store_true",
                        help="show full error details on screen (they may include text from your file)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    from .analyses import discover

    analyses = discover()

    from .reports import discover as discover_reports

    p = sub.add_parser("report", help="make HTML reports (see 'report list')")
    p.set_defaults(handler=cmd_report_list)
    rsub = p.add_subparsers(dest="report_command", metavar="<list|run>")
    rp = rsub.add_parser("list", help="list the available reports")
    rp.set_defaults(handler=cmd_report_list)
    rp = rsub.add_parser("run", help="make one report")
    rp.set_defaults(handler=cmd_report_list)
    run_reports = rp.add_subparsers(dest="report_name", metavar="<report>")
    for name, report in discover_reports().items():
        one = run_reports.add_parser(name, help=report.description, description=report.description)
        _add_file_option(one)
        _add_out_option(one)
        one.add_argument("--settings", metavar="PATH", help="settings file (default: data/settings.toml if it exists)")
        one.add_argument("--year", type=int, metavar="Y",
                         help="one year (tax year: the year in which it ends, e.g. 2026 for 2025/26)")
        if report.period_kind == "range":
            one.add_argument("--from", dest="start", type=int, metavar="Y", help="first year")
            one.add_argument("--to", dest="end", type=int, metavar="Y", help="last year")
        one.set_defaults(start=None, end=None)
        for param in report.params:
            _add_param(one, param)
        one.set_defaults(handler=_run_report, report=report)

    p = sub.add_parser("ui", help="open the browser interface (runs only on this computer)")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--settings", metavar="PATH", help="report settings (default: data/settings.toml if it exists)")
    p.add_argument("--port", type=int, default=0, help="port number (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.set_defaults(handler=cmd_ui)

    p = sub.add_parser("list", help="list the available analyses")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("run", help="run an analysis (see 'list')")
    p.set_defaults(handler=cmd_list)  # "run" without a name lists the analyses
    run_sub = p.add_subparsers(dest="analysis_name", metavar="<analysis>")
    for name, analysis in analyses.items():
        ap = run_sub.add_parser(name, help=analysis.description, description=analysis.description)
        _add_file_option(ap)
        _add_out_option(ap)
        for param in analysis.params:
            _add_param(ap, param)
        ap.set_defaults(handler=_run_analysis, analysis=analysis)

    for analysis in analyses.values():
        for command in analysis.commands:
            cp = sub.add_parser(command.name, help=command.help, description=command.help)
            command.add_arguments(cp)
            cp.set_defaults(handler=_command_handler(command))

    p = sub.add_parser("structure", help="show the file's structure (versions, element and attribute counts; no values)")
    _add_file_option(p)
    p.set_defaults(handler=cmd_structure)

    p = sub.add_parser("export-sqlite", help="export the file to a SQLite database (replaces the previous export)")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--output", metavar="PATH", help="database file (default: data/out/homebank.sqlite)")
    p.add_argument("--settings", metavar="PATH",
                   help="report settings, to add the r_* report views (default: data/settings.toml if it exists)")
    p.set_defaults(handler=cmd_export_sqlite)

    p = sub.add_parser("prepare-import",
                       help="add categories to a bank file (QIF or HomeBank CSV) before you import it in HomeBank")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--input", required=True, metavar="PATH", help="the file from your bank")
    p.add_argument("--account", metavar="NAME",
                   help="its HomeBank account (default: remembered for this file name, or recognised from the contents)")
    p.add_argument("--month", metavar="YYYY-MM", help="the month it is for (default: that of its latest transaction)")
    p.add_argument("--data", metavar="FOLDER", default=str(DATA_DIR), help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_prepare_import)

    p = sub.add_parser("import-files",
                       help="import a month's bank files (linked to their accounts) into your HomeBank file in one go")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--month", required=True, metavar="YYYY-MM", help="the month whose files to import")
    p.add_argument("--days", type=int, default=0,
                   help="treat a transaction as already in HomeBank when the account has one with the same amount "
                        "at most this many days apart (default 0, HomeBank's default)")
    p.add_argument("--no-rules", action="store_true", help="do not run your assignment rules on the new transactions")
    p.add_argument("--payee-defaults", action="store_true",
                   help="fill empty categories and payment modes from the payee's defaults (HomeBank's option)")
    p.add_argument("--output", metavar="PATH", help="name of the new copy (default: data/out/with-imports_<date>.xhb)")
    p.add_argument("--in-place", action="store_true",
                   help="change the HomeBank file itself instead of writing a new copy (a backup is kept in "
                        "data/out/backups; HomeBank must be closed)")
    p.add_argument("--data", metavar="FOLDER", default=str(DATA_DIR), help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_import_files)

    p = sub.add_parser("fill-categories",
                       help="fill empty categories with the rules you choose, in a NEW copy of your HomeBank file")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--review", metavar="PATH",
                   help="apply the rules approved in rules.csv (or the folder that contains it)")
    p.add_argument("--output", metavar="PATH", help="name of the new copy (default: data/out/with-categories_<date>.xhb)")
    p.add_argument("--in-place", action="store_true",
                   help="change the HomeBank file itself instead of writing a new copy (a backup is kept in data/out/backups; HomeBank must be closed)")
    p.set_defaults(handler=cmd_fill_categories)

    p = sub.add_parser("link-transfers",
                       help="find transfers between your accounts that arrived as two transactions, and link them "
                            "in a NEW copy of your HomeBank file")
    _add_file_option(p)
    _add_out_option(p)
    p.add_argument("--days", type=int, default=4, help="at most this many days between the two sides (default 4)")
    p.add_argument("--review", metavar="PATH",
                   help="link the pairs approved in transfers.csv (or the folder that contains it)")
    p.add_argument("--output", metavar="PATH", help="name of the new copy (default: data/out/with-transfers_<date>.xhb)")
    p.add_argument("--in-place", action="store_true",
                   help="change the HomeBank file itself instead of writing a new copy (a backup is kept in data/out/backups; HomeBank must be closed)")
    p.set_defaults(handler=cmd_link_transfers)

    p = sub.add_parser("generate-sample", help="write a synthetic HomeBank file to try the toolkit on")
    _add_out_option(p)
    p.add_argument("--output", metavar="PATH", help="where to write it (default: data/out/sample.xhb)")
    p.add_argument("--overwrite", action="store_true", help="replace the output file if it exists")
    p.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    p.add_argument("--start-year", type=int, default=2020, help="first year (default 2020)")
    p.add_argument("--years", type=int, default=6, help="number of years (default 6)")
    p.add_argument("--reporting", action="store_true",
                   help="add data for the reports (rental, medical, a business account) plus a sample CPI "
                        "table and settings file next to the sample")
    p.add_argument("--home-loans", action="store_true",
                   help="add two home loans whose transactions are categorised by account "
                        "(to try account rules in rule mining)")
    p.add_argument("--uncategorised", type=float, default=0.0, metavar="SHARE",
                   help="leave this share of transactions without a category, e.g. 0.3 (to try fill-categories)")
    p.add_argument("--unlinked-transfers", action="store_true",
                   help="add transfers whose two sides are not linked yet (to try link-transfers)")
    p.add_argument("--sentinel", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_generate_sample)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # old Windows consoles
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except HbkitError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - last line of defence for privacy
        if args.debug:
            raise
        log = write_error_log(Path(getattr(args, "out", OUT_DIR)), e)
        print(
            f"Unexpected error ({type(e).__name__}). The details may include text from your "
            f"file, so they were written to {display_path(log)} instead of the screen.",
            file=sys.stderr,
        )
        return 1
