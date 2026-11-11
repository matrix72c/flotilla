"""Carry exact build provenance through Git -> sdist -> wheel, without .git at runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        carried = root / ".flotilla-build-provenance.json"
        if carried.is_file():
            provenance = json.loads(carried.read_text())
        else:

            def git(*args):
                result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
                return result.stdout.strip() if result.returncode == 0 else None

            commit = git("rev-parse", "HEAD") or os.environ.get("FLOTILLA_BUILD_COMMIT")
            if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise RuntimeError(
                    "build needs Git provenance or explicit FLOTILLA_BUILD_COMMIT from the source checkout"
                )
            status = git("status", "--porcelain", "--untracked-files=normal")
            dirty = bool(status) if status is not None else os.environ.get("FLOTILLA_BUILD_DIRTY", "true") == "true"
            provenance = {"commit": commit, "dirty": dirty, "url": "https://github.com/matrix72c/flotilla.git"}
        self._temporary = tempfile.TemporaryDirectory(prefix="flotilla-provenance-")
        source = Path(self._temporary.name) / "provenance.json"
        source.write_text(json.dumps(provenance, sort_keys=True) + "\n")
        target = ".flotilla-build-provenance.json" if self.target_name == "sdist" else "flotilla/_build_provenance.json"
        build_data.setdefault("force_include", {})[str(source)] = target

    def finalize(self, version, build_data, artifact_path):
        self._temporary.cleanup()
