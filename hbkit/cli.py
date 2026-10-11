"""Command-line interface: python -m hbkit <command> [options].

Run ``python -m hbkit --help`` for the list of commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import HbkitError
from .privacy import OUT_DIR, Summary, Terminal, default_input, display_path, write_error_log


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


def cmd_generate_sample(args: argparse.Namespace) -> int:
    from .fixtures.generator import GeneratorOptions, generate

    out = Path(args.output) if args.output else Path(args.out) / "sample.xhb"
    if out.exists() and not args.overwrite:
        raise HbkitError("the output file already exists; choose another --output or add --overwrite")
    opts = GeneratorOptions(seed=args.seed, start_year=args.start_year, years=args.years,
                            sentinel=args.sentinel)
    gen = generate(opts)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gen.data)
    s = Summary("Synthetic sample file")
    s.note("Entirely fictional data, for trying the toolkit out safely.")
    s.count("Transactions", gen.transactions)
    s.count("Years", args.years)
    s.output("Written to", out)
    Terminal().summary(s)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import webbrowser

    from .ui.server import UiState, make_server

    state = UiState(out_root=Path(args.out), input_path=Path(args.file) if args.file else None)
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

    p = sub.add_parser("generate-sample", help="write a synthetic HomeBank file to try the toolkit on")
    _add_out_option(p)
    p.add_argument("--output", metavar="PATH", help="where to write it (default: data/out/sample.xhb)")
    p.add_argument("--overwrite", action="store_true", help="replace the output file if it exists")
    p.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    p.add_argument("--start-year", type=int, default=2020, help="first year (default 2020)")
    p.add_argument("--years", type=int, default=6, help="number of years (default 6)")
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
