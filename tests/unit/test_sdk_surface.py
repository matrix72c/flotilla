"""The SDK surface doc and the import surface cannot drift apart silently."""

from __future__ import annotations

from pathlib import Path

import flotilla
from flotilla import native
from flotilla.integrations import harbor

SDK = Path(__file__).resolve().parents[2] / "docs" / "SDK.md"


def _table_symbols(section: str) -> set[str]:
    """The first-column symbols of one `##` section's table in the SDK surface doc."""
    lines = SDK.read_text().splitlines()
    start = lines.index(f"## {section}")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return {line[3 : line.index("`", 3)] for line in lines[start:end] if line.startswith("| `")}


def test_top_level_table_is_the_package_surface() -> None:
    documented = _table_symbols("Imported from `flotilla`")
    surface = set(flotilla.__all__) - {"__version__"}
    assert documented == surface
    for name in surface:  # every documented name is importable as written
        getattr(flotilla, name)


def test_native_table_is_the_native_surface() -> None:
    documented = _table_symbols("Imported from `flotilla.native`")
    assert documented == set(native.__all__)
    for name in documented:  # the lazily imported names resolve too
        getattr(native, name)


def test_harbor_table_is_the_integration_surface() -> None:
    documented = _table_symbols("Imported from `flotilla.integrations.harbor`")
    assert documented == set(harbor.__all__)
    for name in documented:
        getattr(harbor, name)
    # The top-level import must stay harbor-free; the integration is its own surface.
    assert not documented & set(flotilla.__all__)
