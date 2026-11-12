"""Path joins inside a native release."""

from __future__ import annotations

from pathlib import Path

from .models import relative_path


def confined(root: Path, key: str) -> Path:
    """`root / key` for a canonical relative key, without following symlinks along the way."""
    relative_path(key)
    current = root
    for part in key.split("/"):
        current /= part
        if current.is_symlink():
            raise ValueError(f"symlink in release path: {key}")
    return current
