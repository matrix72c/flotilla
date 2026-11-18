"""The Compose support table and the normalizer classify service keys the same way."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from flotilla.compose.normalize import _EQUIVALENT, _REJECT, Category

DOC = Path(__file__).resolve().parents[2] / "docs" / "Compose_Support.md"
CATEGORIES = frozenset(get_args(Category))
#: `Rejected (<category>[, mixed])`: the category a documented row claims.
CLAIM = re.compile(r"Rejected \(([a-z-]+)(?:, mixed)?\)")


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
    assert set(_REJECT) <= documented, set(_REJECT) - documented


def test_documented_rejection_categories_match_normalizer() -> None:
    """A rejected row states its category, and it is the one the normalizer records."""
    rows = _service_rows()
    for key, (category, _) in _REJECT.items():
        claim = CLAIM.search(rows[key])
        assert claim is not None, f"{key}: documented status states no category: {rows[key]!r}"
        assert claim.group(1) == category, (key, claim.group(1), category)


def test_every_category_is_declared() -> None:
    """Categories live in one place: the `Category` literal, the table, and the documented list."""
    text = DOC.read_text()
    section = text[text.index("## Why a field is rejected") : text.index("## Service keys")]
    documented = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.M))
    assert documented == CATEGORIES, documented ^ CATEGORIES
    for key, (category, _) in _REJECT.items():
        assert category in CATEGORIES, (key, category)


def test_documented_equivalents_are_not_rejected() -> None:
    rows = _service_rows()
    for key in _EQUIVALENT:
        assert rows[key].startswith("Equivalent"), (key, rows[key])
    for key, status in rows.items():
        if status.startswith("Equivalent"):
            assert key not in _REJECT, key
