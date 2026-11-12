"""Version-checked official Compose config adapter; no daemon or fallback parser."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from flotilla.compose.model import MAIN, Project
from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.compose.paths import confined

COMPOSE_VERSION = "2.39.4"


def _input_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        try:
            value = str(path.relative_to(root))
        except ValueError as exc:
            raise ValueError(f"Compose input path escapes project directory: {value!r}") from exc
    return confined(root, value)


def _yaml_items(node: yaml.Node, ancestors: frozenset[int] = frozenset()) -> Iterator[tuple[str, yaml.Node]]:
    """Inspect fields including YAML merge aliases, without constructing or merging values."""
    if not isinstance(node, yaml.MappingNode):
        return
    if id(node) in ancestors:
        raise ValueError("recursive YAML merge is unsupported")
    ancestors = ancestors | {id(node)}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            raise ValueError("Compose mapping keys must be scalar strings")
        if key.value == "<<":
            for merged in value.value if isinstance(value, yaml.SequenceNode) else [value]:
                yield from _yaml_items(merged, ancestors)
        else:
            yield key.value, value


def _env_file_gate(root: Path, node: yaml.Node) -> None:
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _env_file_gate(root, item)
        return
    if isinstance(node, yaml.MappingNode):
        paths = [value for key, value in _yaml_items(node) if key == "path"]
        if not paths:
            raise ValueError("env_file mapping requires a confined literal path")
        for path in paths:
            _env_file_gate(root, path)
        return
    if isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null":
        return
    if not isinstance(node, yaml.ScalarNode) or "$" in node.value or not node.value:
        raise ValueError("env_file requires a project-local literal path; dynamic paths are unsupported")
    _input_path(root, node.value)


def _input_gate(root: Path, files: Sequence[Path]) -> None:
    """Reject file-reading features before Compose can open anything outside the task.

    YAML nodes retain !reset/!override tags for the official CLI. This is only a
    path/feature gate, not an alternate Compose merge or interpolation engine.
    """
    for path in files:
        try:
            document = yaml.compose(path.read_text(), Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid Compose YAML in {path.name}: {exc}") from exc
        if document is None:
            continue
        for key, value in _yaml_items(document):
            if key in ("include", "configs", "secrets"):
                raise ValueError(f"Compose {key} is unsupported before config normalization")
            if key != "services":
                continue
            for service, definition in _yaml_items(value):
                for field, spec in _yaml_items(definition):
                    if field in ("extends", "label_file", "configs", "secrets"):
                        raise ValueError(f"services.{service}.{field} is unsupported before config normalization")
                    if field == "env_file":
                        _env_file_gate(root, spec)


# The executable's version is checked once per process, not once per task.
_VERSION_CHECKED: set[tuple[tuple[str, ...], str]] = set()


@dataclass(frozen=True)
class ComposeConfigAdapter:
    """The executable and exact version are explicit dependencies, never auto-installed.

    Callers provide an attempt-private environment directory and explicit interpolation
    values. Ambient shell variables and Compose file discovery are deliberately disabled.
    """

    command: tuple[str, ...] = ("docker", "compose")
    expected_version: str = COMPOSE_VERSION
    # Capacity inspection needs no CLI for an empty Compose input: the shared
    # Harbor base below has no resource declarations or interpolation to resolve.
    resources_only: bool = False

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Compose command must be nonempty")
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.expected_version):
            raise ValueError("Compose version must be an exact release (major.minor.patch)")

    def load(
        self,
        project_dir: Path,
        *,
        files: Sequence[Path],
        environment: Mapping[str, str],
        main_image: str | None,
        main_build: bool,
        workdir: str | None = None,
    ) -> Project:
        root = project_dir.resolve()
        selected = [_input_path(root, str(path)) for path in files]
        dotenv = confined(root, ".env")
        _input_gate(root, selected)
        # PATH locates the configured executable/plugin; no task interpolation values
        # are copied from os.environ. All other values are caller-owned.
        env = {"PATH": os.defpath, **environment, "CONTEXT_DIR": str(root)}
        base: dict[str, Any] = {"command": list(KEEPALIVE)}
        if main_image is not None:
            base["image"] = main_image
        elif main_build:
            base["build"] = {"context": "."}
        if self.resources_only and not selected:
            # load_environment still applies native task resource overrides. Do
            # not use this shortcut for even an empty, but present, Compose file.
            return normalize({"services": {MAIN: base}}, {}, resolved=True)
        if (self.command, self.expected_version) not in _VERSION_CHECKED:
            version = self._run([*self.command, "version", "--short"], root, env).strip().removeprefix("v")
            if version != self.expected_version:
                raise ValueError(f"Compose {self.expected_version} required; found {version!r}")
            _VERSION_CHECKED.add((self.command, self.expected_version))
        # Compose owns merging, interpolation, env_file parsing and short forms.
        with TemporaryDirectory(prefix="flotilla-compose-") as temporary:
            defaults = Path(temporary) / "defaults.yaml"
            defaults.write_text(yaml.safe_dump({"services": {MAIN: base}}))
            args = [
                *self.command,
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
            # Compose config serializes every literal dollar as $$ (including
            # mapping keys) so its output can be used as another Compose file.
            # Undo that transport escaping exactly once, without interpreting any
            # variable name, brace expression, or shell syntax in the result.
            document = json.loads(self._run(args, root, env).replace("$$", "$"))
        if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
            raise ValueError("Compose config did not return a service mapping")
        main = document["services"].get(MAIN)
        if not isinstance(main, dict):
            raise ValueError("Compose config is missing Harbor's main service")
        # Harbor's explicit prebuilt image supersedes the task build; these are
        # semantic Harbor overrides, not a second generic YAML merge.
        if main_image is not None:
            main["image"] = main_image
            main.pop("build", None)
        if workdir is not None:
            main["working_dir"] = workdir
        self._relative_paths(document, root)
        # Compose adds project-scoped resource names. They describe Docker's
        # resources, not cross-attempt storage; custom/external names still reject.
        project_name = document.get("name", "flotilla")
        for category in ("networks", "volumes"):
            for name, spec in (document.get(category) or {}).items():
                if isinstance(spec, dict) and spec.get("name") == f"{project_name}_{name}":
                    spec.pop("name")
        return normalize(document, {}, resolved=True)

    @staticmethod
    def _relative_paths(document: dict[str, Any], root: Path) -> None:
        for service in document["services"].values():
            if not isinstance(service, dict):
                continue
            build = service.get("build")
            if isinstance(build, dict) and isinstance(build.get("context"), str):
                context = _input_path(root, build["context"])
                build["context"] = str(context.relative_to(root))
            for mount in service.get("volumes") or []:
                if isinstance(mount, dict) and mount.get("type") == "bind":
                    source = _input_path(root, mount["source"])
                    mount["source"] = "./" + str(source.relative_to(root))

    @staticmethod
    def _run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
        try:
            result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"official Compose config unavailable: {exc}") from exc
        if result.returncode:
            raise ValueError(f"official Compose config failed: {result.stderr.strip()}")
        return result.stdout
