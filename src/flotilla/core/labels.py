"""Resource labels for ownership, reconciliation, and cleanup."""

from __future__ import annotations

import hashlib
import re

LAUNCH = "flotilla/launch"
TRIAL = "flotilla/trial"
UNIT = "flotilla/unit"
TASK = "flotilla/task"
ROLE = "flotilla/role"

ROLE_UNIT = "unit"
ROLE_ANCHOR = "anchor"

_VALID = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?)?")
_INVALID_CHARS = re.compile(r"[^-A-Za-z0-9_.]+")


def anchor_labels(launch_id: str) -> dict[str, str]:
    """Labels of a launch's anchor; also the query used to list it."""
    return {LAUNCH: launch_id, ROLE: ROLE_ANCHOR}


def is_valid_value(value: str, limit: int) -> bool:
    return len(value) <= limit and _VALID.fullmatch(value) is not None


def require_value(key: str, value: str, limit: int) -> str:
    """Label value used for listing: must be valid as-is, otherwise raises `ValueError`."""
    if not is_valid_value(value, limit):
        raise ValueError(
            f"label {key} value {value!r} is invalid (<={limit} chars of alphanumerics and -_., "
            "starting and ending with an alphanumeric)"
        )
    return value


def sanitize_value(value: str, limit: int) -> str:
    """Label value for diagnostics only: invalid chars become `-`, leading/trailing non-alphanumerics are
    stripped; overlong or rewritten values are truncated and suffixed with an 8-char hash so distinct
    originals stay distinguishable."""
    if is_valid_value(value, limit):
        return value
    cleaned = _INVALID_CHARS.sub("-", value).strip("-_.")
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    head = cleaned[: max(0, limit - len(digest) - 1)].rstrip("-_.")
    return f"{head}-{digest}" if head else digest
