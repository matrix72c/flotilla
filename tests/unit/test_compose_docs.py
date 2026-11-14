"""The Compose support table and the normalizer classify service keys the same way."""

from __future__ import annotations

import re
from pathlib import Path

from flotilla.compose.normalize import _EQUIVALENT, _REJECT

DOC = Path(__file__).resolve().parents[2] / "docs" / "Compose_Support.md"
# Inlined into `environment` by the task loader before normalization; documented as supported.
_PREPROCESSED = {"env_file"}


def _service_rows() -> dict[str, str]:
    text = DOC.read_text()
    section = text[text.index("## Service keys") : text.index("## Volumes and bind mounts")]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0].startswith("`"):
            continue
        for key in re.findall(r"`([^`]+)`", cells[0]):
            rows[key] = cells[1]
    return rows


def test_documented_rejections_match_normalizer() -> None:
    rows = _service_rows()
    documented = {key for key, status in rows.items() if status.startswith("Rejected")}
    assert documented <= set(_REJECT), documented - set(_REJECT)
    assert set(_REJECT) - _PREPROCESSED <= documented, set(_REJECT) - _PREPROCESSED - documented


def test_documented_equivalents_are_not_rejected() -> None:
    rows = _service_rows()
    for key in _EQUIVALENT:
        assert rows[key].startswith("Equivalent"), (key, rows[key])
    for key, status in rows.items():
        if status.startswith("Equivalent"):
            assert key not in _REJECT, key
