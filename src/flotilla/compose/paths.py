"""Filesystem boundaries for offline task inputs and generated artifacts."""

import re
from pathlib import Path

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def valid_name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def confined(root: Path, relative: str) -> Path:
    """Resolve a relative path, including symlinks, inside a trusted root."""
    path = Path(relative)
    if path.is_absolute():
        raise ValueError(f"absolute input path is not allowed: {relative!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes {root}: {relative!r}")
    return resolved
