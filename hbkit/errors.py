"""Exceptions whose messages are safe to show in the terminal.

A message must never contain values from the user's file (memo text, names,
amounts). It may name an element, an attribute and a line number.
"""

from __future__ import annotations


class HbkitError(Exception):
    """An expected error with a terminal-safe message."""


class XhbFormatError(HbkitError):
    """The file could not be read as a HomeBank file."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"{message} (line {line})" if line else message)


class WriteRefusedError(HbkitError):
    """The writer refused to write (for example, the output is the input file)."""
