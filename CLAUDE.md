# CLAUDE.md — HomeBank analysis toolkit

A local Python toolkit that reads a HomeBank `.xhb` file, exports it to SQLite,
runs pluggable analyses and writes targeted changes to a *new copy* of the file.
The owner runs it on their real file; Claude builds and tests it and never sees
the real data.

## Privacy rules (non-negotiable)

1. **`data/` is private.** The owner's real `.xhb` lives in `data/`; results go to
   `data/out/`. Both are git-ignored (only the empty `.gitkeep` placeholders are
   tracked). `.claude/settings.json` denies `Read`/`Edit` on `data/` and any
   `Bash`/`PowerShell` command whose text contains `data/`.
2. **Never open, print, list, search, copy, hash or run anything against `data/`.**
   That includes `ls`/`dir`, `cat`, Grep/Glob, `git` commands that would show its
   contents, and running the toolkit with a path under `data/`. **Never run the
   toolkit on the real file — the owner does that.** Test only against synthetic data.
3. **Learn the `.xhb` format only from HomeBank's open-source code** (`src/hb-xml.c`
   and the headers it uses, from the source tarball at gethomebank.org or the
   Launchpad repository) and public documentation — never from the owner's file.
   What has been learnt is recorded in `docs/xhb-format.md`.
4. **Terminal output is aggregate-only by default:** counts, percentages and error
   messages without transaction text, payee/category/account names or amounts.
   Detailed output goes to files under `data/out/`. This is enforced in code (the
   terminal summary type accepts numbers only) and checked by tests that plant
   sentinel strings in synthetic files and assert they never reach stdout/stderr.
   Error messages name the element, attribute and line number, never the value.
5. **Tests use only synthetic fixtures** (from the fixture generator) or HomeBank's
   public `example.xhb`, written to temporary directories.
6. If the owner pastes tool output (e.g. the structure report), use it to adjust
   the code. Never ask for transaction text. If personal data is pasted by
   accident, do not repeat it, store it or commit it.
7. Never commit `.xhb` files (except under `tests/fixtures/`), CSV results or
   SQLite databases. `.gitignore` enforces this as a second line of defence.

### Limits of the technical guard (be honest about these)

Per the Claude Code permissions documentation, `Read`/`Edit` deny rules cover
Claude's built-in file tools and shell file commands Claude Code recognises
(`cat`, `head`, `tail`, `sed`, redirections), but **not** a program that opens
files itself (for example a Python script). The `Bash(*data/*)` rule only matches
commands whose text contains `data/`. Rule 2 above is therefore the real
safeguard. OS-level enforcement is available through the Claude Code sandbox
(`sandbox.filesystem.denyRead`) on macOS, Linux and WSL2 only, not native Windows.

Practical consequence: never put the literal text `data/` in a shell command
(including commit messages — use `git commit -F <file>` if a message must mention it).

## Working agreements

- Write in **South African English** (normalise, analyse, categorise, behaviour,
  colour, licence as a noun) in code comments, CLI text and documentation.
- **Python 3.13, standard library only.** Ask the owner before adding any dependency.
- Plan first, show the owner, then build in stages with all tests passing at each stage.
- The writer never overwrites its input; it refuses if the output path is the input.
- Adding an analysis later should mean one new module in `hbkit/analyses/` plus its tests.

## Status

Privacy set-up done. Architecture plan presented to the owner; awaiting approval
before any toolkit code is written. Project layout and commands will be recorded
here once built.
