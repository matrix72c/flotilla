"""Read the main task environment deterministically, independently of optional adapters."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from flotilla.compose.model import MAIN, Finding, Project
from flotilla.compose.normalize import harbor_overlay, normalize
from flotilla.compose.paths import confined
from flotilla.compose.resources import cpu, memory

TASK_FILE = "task.toml"
ENV_DIR = "environment"
COMPOSE_FILE = "docker-compose.yaml"
PUBLIC = "public"
#: Variables Harbor's docker environment injects into Compose interpolation, translated to flotilla's project-relative
#: paths: `CONTEXT_DIR` is the environment directory. As in Harbor, they take precedence over `.env`.
HARBOR_VARIABLES = {"CONTEXT_DIR": "."}


class TaskError(ValueError):
    """Unreadable task directory (missing files, TOML / YAML syntax errors)."""


@dataclass(frozen=True)
class HarborTask:
    name: str  # `[task].name`, defaults to the directory name
    path: Path
    env_dir: Path
    project: Project
    findings: tuple[Finding, ...]  # `task.toml`-level findings, alongside `project.findings`
    environment_config: Mapping[str, Any] | None = None


def find_tasks(roots: Iterable[Path]) -> Iterator[Path]:
    """Task directories under `roots`: dirs containing `task.toml`, not descended further; hidden dirs skipped."""
    for root in roots:
        if (root / TASK_FILE).is_file():
            yield root
            continue
        for current, dirs, files in os.walk(root):
            if TASK_FILE in files:
                dirs.clear()
                yield Path(current)
                continue
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))


def load_task(path: Path) -> HarborTask:
    try:
        return _load_task(path)
    except TaskError:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise TaskError(f"task input: {exc}") from exc


def _load_task(path: Path) -> HarborTask:
    try:
        config = tomllib.loads(confined(path, TASK_FILE).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"failed to read {TASK_FILE}: {exc}") from exc
    env_dir = confined(path, ENV_DIR)
    return load_environment(
        path,
        env_dir,
        _table(config, "environment"),
        name=_table(config, "task").get("name") or path.name,
    )


def load_environment(
    task_dir: Path,
    env_dir: Path,
    environment: Mapping[str, Any],
    *,
    name: str,
    findings: tuple[Finding, ...] = (),
) -> HarborTask:
    """Parse a concrete environment context without depending on Harbor's runtime."""
    if not env_dir.resolve().is_relative_to(task_dir.resolve()):
        raise TaskError("environment context escapes task directory")
    if not isinstance(name, str) or not name:
        raise TaskError("task.name must be a nonempty string")
    findings = (*findings, *_network_policies({"environment": environment}))
    compose_path = confined(env_dir, COMPOSE_FILE)
    compose: dict[str, Any] = {}
    if compose_path.is_file():
        try:
            loaded = yaml.safe_load(compose_path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise TaskError(f"failed to read {ENV_DIR}/{COMPOSE_FILE}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise TaskError(f"{ENV_DIR}/{COMPOSE_FILE} is not a mapping")
        compose = loaded
    image = environment.get("docker_image")
    main_image = image if isinstance(image, str) and image else None
    overlay = harbor_overlay(
        compose, main_image=main_image, main_build=main_image is None and (env_dir / "Dockerfile").is_file()
    )
    if environment.get("workdir") is not None:
        overlay["services"][MAIN]["working_dir"] = environment["workdir"]
    variables = {**_dotenv(confined(env_dir, ".env")), **HARBOR_VARIABLES}
    _env_files(overlay, env_dir)
    project = normalize(overlay, variables)
    if _network_mode(environment, baseline=True) == "no-network":
        # Preserve peer topology while removing external access from every service. This survives
        # compilation/publication and cannot be undone by a caller's deployment egress policy.
        project = replace(
            project, networks={name: replace(net, internal=True) for name, net in project.networks.items()}
        )
    if MAIN in project.services:
        declared = dict(project.services[MAIN].resources)
        try:
            if "cpus" in environment:
                declared["cpu"] = cpu(environment["cpus"])
            if "memory" in environment and "memory_mb" in environment:
                raise ValueError("use only one of memory or memory_mb")
            if "memory" in environment:
                declared["memory"] = memory(environment["memory"])
            if "memory_mb" in environment:
                declared["memory"] = memory(str(environment["memory_mb"]) + "Mi")
        except ValueError as exc:
            raise TaskError(f"task.toml environment resources: {exc}") from exc
        services = dict(project.services)
        services[MAIN] = replace(services[MAIN], resources=declared)
        project = replace(project, services=services)
    return HarborTask(
        name=name,
        path=task_dir,
        env_dir=env_dir,
        project=project,
        findings=findings,
        environment_config=environment,
    )


def _network_policies(config: Mapping[str, Any]) -> Iterator[Finding]:
    phases = [("environment", _table(config, "environment"), True)]
    for where, table, baseline in phases:
        mode = _network_mode(table, baseline=baseline)
        if mode is not None and mode not in (PUBLIC, "no-network"):
            yield Finding(
                f"task.toml:{where}.network_mode",
                "reject",
                f"environment network mode {mode!r} is unsupported by core task loading",
            )


def _network_mode(table: Mapping[str, Any], *, baseline: bool) -> str | None:
    """network_mode set explicitly or via `allow_internet`; None when unset (the baseline is public)."""
    mode = table.get("network_mode")
    if isinstance(mode, str):
        return mode
    if baseline and "allowed_hosts" not in table and isinstance(table.get("allow_internet"), bool):
        return PUBLIC if table["allow_internet"] else "no-network"
    return None


def _table(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TaskError(f"{key}: expected a table")
    return value


def _dotenv(path: Path, *, strict: bool = False) -> dict[str, str]:
    """`KEY=VALUE` lines; `#` starts a comment, surrounding quotes are stripped. No expansion, as in Compose."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if strict:
                raise TaskError(f"{path.name}: env_file entries require KEY=VALUE")
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")):
                raise TaskError(f"{path.name}: malformed quoted env_file value")
            value = value[1:end]
            if quote == "'" and strict:
                value = value.replace("$", "$$")
            elif "\\" in value:
                raise TaskError(f"{path.name}: escaped env_file values are not yet supported")
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        out[key] = value
    return out


def _env_files(compose: dict[str, Any], env_dir: Path) -> None:
    """Apply ordered env_file values, then explicit environment; never inherit the host."""
    services = compose.get("services")
    if not isinstance(services, dict):
        return
    for name, service in services.items():
        if not isinstance(service, dict) or "env_file" not in service:
            continue
        spec = service.pop("env_file")
        paths = [spec] if isinstance(spec, str) else spec
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise TaskError(f"services.{name}.env_file: only a path or list of paths is supported")
        values: dict[str, object] = {}
        for relative in paths:
            path = confined(env_dir, relative)
            if not path.is_file():
                raise TaskError(f"services.{name}.env_file: missing file {relative!r}")
            values.update(_dotenv(path, strict=True))
        environment = {} if service.get("environment") is None else service["environment"]
        if isinstance(environment, dict):
            values.update(environment)
        elif isinstance(environment, list):
            for item in environment:
                if not isinstance(item, str):
                    raise TaskError(f"services.{name}.environment: expected strings")
                key, sep, value = item.partition("=")
                values[key] = value if sep else None
        else:
            raise TaskError(f"services.{name}.environment: expected mapping or list")
        service["environment"] = values
