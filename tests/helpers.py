"""Shared test helpers. Tests only ever use synthetic files in temporary folders."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from functools import lru_cache
from pathlib import Path

from hbkit.cli import main
from hbkit.fixtures.generator import SENTINEL, GeneratedFile, GeneratorOptions, generate

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = FIXTURES / "homebank_example" / "example.xhb"


@lru_cache(maxsize=None)
def sample(**kwargs) -> GeneratedFile:
    """A generated file, cached per option set (generation is deterministic)."""
    return generate(GeneratorOptions(**kwargs))


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_sample(self, name: str = "sample.xhb", **kwargs) -> Path:
        path = self.tmp / name
        path.write_bytes(sample(**kwargs).data)
        return path

    def assertNoSentinel(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn(SENTINEL, text, "text from the HomeBank file reached the terminal")
