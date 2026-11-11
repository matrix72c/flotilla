"""Offline test doubles for the Compose adapter boundary.

`OfflineComposeAdapter` mirrors the adapter contract — Harbor overlay of the main
image/build and workdir over the project — through the deterministic no-CLI
loader, so select/prepare tests run without the Compose binary. Interpolation
differs from the official adapter: only dotenv files and the built-in Harbor
variables are substituted, so unresolved prepare parameters surface exactly the
way the no-CLI loader reports them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.task import load_environment


class OfflineComposeAdapter(ComposeConfigAdapter):
    """Compose adapter double for select/prepare tests; never executes a tool."""

    def load(
        self,
        project_dir: Path,
        *,
        files: Sequence[Path],
        environment: Mapping[str, str],
        main_image: str | None,
        main_build: bool,
        workdir: str | None = None,
    ) -> Any:
        config: dict[str, Any] = {}
        if main_image is not None:
            config["docker_image"] = main_image
        if workdir is not None:
            config["workdir"] = workdir
        return load_environment(project_dir.parent, project_dir, config, name=project_dir.parent.name).project
