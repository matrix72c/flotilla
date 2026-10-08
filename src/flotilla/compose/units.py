"""Compose 中的时长与容量写法。"""

from __future__ import annotations

import re

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: object, *, path: str) -> float:
    """Go 风格的时长（`5s`、`1m30s`、`500ms`）或数字秒，返回秒。"""
    if isinstance(value, bool):
        raise ValueError(f"{path}：不是时长：{value!r}")
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}：不是时长：{value!r}")
    pos, total = 0, 0.0
    for m in _DURATION_PART.finditer(value):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNIT_S[m.group(2)]
        pos = m.end()
    if pos != len(value):
        raise ValueError(f"{path}：不是时长：{value!r}")
    return total
