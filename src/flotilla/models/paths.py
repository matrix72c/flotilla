"""Path rules shared by task inputs and outputs: canonical relative and absolute paths.

A key that escapes its root, or an output path that is not canonical, is a task input
error -- and it has to be caught before anything is created, not after a sandbox is
already running with the wrong mount.
"""

from __future__ import annotations

import posixpath


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"expected canonical relative path: {value!r}")
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("paths cannot contain backslashes or control characters")
    return value


def absolute_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or posixpath.normpath(value) != value:
        raise ValueError("expected canonical absolute path")
    relative_path(value[1:])
    return value
