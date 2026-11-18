"""Read a Compose project through the official executable, for the differential check to compare against.

flotilla no longer ships a Compose adapter: `flotilla.compose.parse` is the only parser. But the
claim that the parser agrees with Docker needs the thing it claims to agree with, so the call to
`docker compose config` lives here, in the test that makes the comparison, and nowhere in the
package.

What it does is deliberately the minimum the comparison needs, and in the same shape the parser
is given: an explicit project directory, project name, file list and `.env` path, a process
environment holding only an executable-search PATH and the caller's interpolation values, and
Harbor's `main` service supplied as a base file. It is not a supported entry point and nothing
but the differential check should call it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from flotilla.compose.model import MAIN, Project
from flotilla.compose.normalize import KEEPALIVE, normalize

VERSION = "2.39.4"

# Compose reports an undefined reference on stderr and substitutes a blank string, exiting 0. The
# name is quoted in the `WARN[0000] The "X" ...` format and backslash-escaped in the structured
# `msg="The \"X\" ..."` one, so accept both: a format this does not match would turn the check
# below into a silent no-op, and the parser's rejection would look like a divergence.
_UNSET = re.compile(r'The \\?"?([A-Za-z_][A-Za-z0-9_]*)\\?"? variable is not set')


class OfficialComposeError(RuntimeError):
    """The executable is missing, the wrong version, or failed for a reason that is not the input."""


def load_project(
    project_dir: Path,
    *,
    binary: str,
    files: Sequence[Path],
    environment: Mapping[str, str],
    main_image: str | None,
    main_build: bool,
    workdir: str | None = None,
) -> Project:
    """Resolve `files` with `binary` and normalize the result, mirroring `parse.load_project`."""
    root = project_dir.resolve()
    selected = [_input_path(root, path) for path in files]
    dotenv = root / ".env"
    env = {"PATH": os.defpath, **environment, "CONTEXT_DIR": str(root)}

    version = _run([binary, "version", "--short"], root, env).strip().removeprefix("v")
    if version != VERSION:
        raise OfficialComposeError(f"official Compose {VERSION} required for the comparison; found {version!r}")

    base: dict[str, Any] = {"command": list(KEEPALIVE)}
    if main_image is not None:
        base["image"] = main_image
    elif main_build:
        base["build"] = {"context": "."}
    with TemporaryDirectory(prefix="flotilla-official-compose-") as temporary:
        defaults = Path(temporary) / "defaults.yaml"
        defaults.write_text(yaml.safe_dump({"services": {MAIN: base}}))
        args = [
            binary,
            "--project-directory",
            str(root),
            "--project-name",
            "flotilla",
            "--env-file",
            str(dotenv) if dotenv.is_file() else os.devnull,
            "-f",
            str(defaults),
        ]
        for path in selected:
            args.extend(("-f", str(path)))
        args.extend(("config", "--format", "json", "--no-path-resolution"))
        # Compose config serializes every literal dollar as `$$`, including mapping keys, so its
        # output can be fed back in as a Compose file. Undo that transport escaping exactly once,
        # without interpreting any variable name or brace expression in the result.
        document = json.loads(_run(args, root, env).replace("$$", "$"))

    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise OfficialComposeError("official Compose config did not return a service mapping")
    main = document["services"].get(MAIN)
    if not isinstance(main, dict):
        raise ValueError(f"official Compose config is missing the {MAIN} service")
    if main_image is not None:
        main["image"] = main_image
        main.pop("build", None)
    if workdir is not None:
        main["working_dir"] = workdir
    _relative_paths(document, root)
    # Compose adds project-scoped resource names. They describe Docker's resources, not anything
    # here, and the parser never invents them, so they are stripped before the comparison.
    project_name = document.get("name", "flotilla")
    for category in ("networks", "volumes"):
        for name, spec in (document.get(category) or {}).items():
            if isinstance(spec, dict) and spec.get("name") == f"{project_name}_{name}":
                spec.pop("name")
    return normalize(document)


def _input_path(root: Path, path: Path) -> Path:
    value = str(path)
    if path.is_absolute():
        try:
            value = str(path.relative_to(root))
        except ValueError as exc:
            raise ValueError(f"Compose input path escapes the project directory: {value!r}") from exc
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes {root}: {value!r}")
    return resolved


def _relative_paths(document: dict[str, Any], root: Path) -> None:
    for service in document["services"].values():
        if not isinstance(service, dict):
            continue
        build = service.get("build")
        if isinstance(build, dict) and isinstance(build.get("context"), str):
            build["context"] = str(_input_path(root, Path(build["context"])).relative_to(root))
        for mount in service.get("volumes") or []:
            if isinstance(mount, dict) and mount.get("type") == "bind":
                mount["source"] = "./" + str(_input_path(root, Path(mount["source"])).relative_to(root))


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfficialComposeError(f"official Compose config unavailable: {exc}") from exc
    if result.returncode:
        raise ValueError(f"official Compose config failed: {result.stderr.strip()}")
    unset = sorted({match.group(1) for match in _UNSET.finditer(result.stderr)})
    if unset:
        # A reference with a default (`${VAR:-x}`) does not warn, so this is exactly the case the
        # input provides no value for at all -- which flotilla refuses, so Compose must too.
        raise ValueError(f"references variables the task does not provide and that have no default: {unset}")
    return result.stdout
