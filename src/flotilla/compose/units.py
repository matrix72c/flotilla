"""Parse Compose units and service expansion."""

from __future__ import annotations

import math
import re

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: object, *, path: str) -> float:
    """Go-style duration (`5s`, `1m30s`, `500ms`) or numeric seconds; returns seconds."""
    if isinstance(value, bool):
        raise ValueError(f"{path}: not a duration: {value!r}")
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{path}: duration must be finite and non-negative")
        return float(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: not a duration: {value!r}")
    pos, total = 0, 0.0
    for m in _DURATION_PART.finditer(value):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNIT_S[m.group(2)]
        pos = m.end()
    if pos != len(value):
        raise ValueError(f"{path}: not a duration: {value!r}")
    return total
