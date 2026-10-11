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

    p = sub.add_parser("structure", help="show the file's structure (versions, element and attribute counts; no values)")
    _add_file_option(p)
    p.set_defaults(handler=cmd_structure)

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
