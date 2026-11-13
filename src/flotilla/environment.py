"""Explicit credential-file loading for CPU tools; never execute shell code."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def load_env_file(path: str | Path) -> None:
    """Load literal KEY=value assignments. No expansion, sourcing or logging values."""
    os.environ.update(read_env_file(path))


def read_env_file(path: str | Path) -> dict[str, str]:
    """Parse literal KEY=value assignments without touching the process environment."""
    values: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment assignment at line {number}")
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            raise ValueError(f"invalid environment quoting at line {number}") from None
        if len(parts) > 1:
            raise ValueError(f"quote whitespace in environment value at line {number}")
        if key in values:
            raise ValueError(f"duplicate environment variable {key} at line {number}")
        values[key] = parts[0] if parts else ""
    return values
