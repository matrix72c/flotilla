"""Generate real Compose inputs for public lifecycle contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from flotilla import Environment, EnvironmentDefinition, Flotilla, OpenOptions
from flotilla.runtime.types import Task


@dataclass
class FixtureRequest:
    task: Task
    request_id: str
    timeout: float | None = None
    runtime_params: dict[str, str] = field(default_factory=dict)
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict)
    disable_internet: bool = False


def definition(task: Task, root: Path) -> EnvironmentDefinition:
    manifest = task.manifest()
    services: dict[str, Any] = {}
    for name, unit in manifest.units.items():
        service: dict[str, Any] = {
            "image": unit.image,
            "entrypoint": unit.command or [],
            "command": [],
            "environment": unit.context.env,
            "user": f"{unit.context.uid}:{unit.context.gid}",
            "working_dir": unit.context.cwd,
            "restart": unit.restart,
            "read_only": unit.readonly_rootfs,
            "tmpfs": [
                f"{m.mount_path}:size={m.size_bytes},mode={m.mode:04o},uid={m.uid},gid={m.gid}"
                for m in unit.tmpfs_mounts
            ],
            "deploy": {"resources": {"limits": unit.resources.model_dump(exclude_none=True)}},
        }
        limits = service["deploy"]["resources"]["limits"]
        if "cpu" in limits:
            limits["cpus"] = limits.pop("cpu")
        networks = {
            n: {"aliases": [alias for alias, members in net.names.items() if name in members]}
            for n, net in manifest.networks.items()
            if name in net.members
        }
        if networks:
            service["networks"] = networks
        else:
            service["network_mode"] = "none"
        if unit.healthcheck:
            hc = unit.healthcheck
            service["healthcheck"] = {
                "test": ["CMD", *hc.test],
                "interval": f"{hc.interval_s}s",
                "timeout": f"{hc.timeout_s}s",
                "retries": hc.retries,
                "start_period": f"{hc.start_period_s}s",
                "start_interval": f"{hc.start_interval_s}s",
            }
        if name in manifest.depends_on:
            service["depends_on"] = {n: dep.model_dump() for n, dep in manifest.depends_on[name].items()}
        if unit.mounts:
            assert all(m.scope == "trial" for m in unit.mounts), "fixture only supports fresh named volumes"
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": m.key,
                    "target": m.target,
                    "read_only": m.read_only,
                    "volume": {"nocopy": True},
                }
                for m in unit.mounts
            ]
        services[name] = service
    # Core task inputs designate main as the agent; preserve service names used by live fixtures.
    if "main" not in services:
        services["main"] = {"image": next(iter(manifest.units.values())).image}
    compose = {
        "services": services,
        "networks": {n: {"internal": net.internal} for n, net in manifest.networks.items()},
        "volumes": {v.key: {} for v in manifest.trial_volumes},
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yaml").write_text(yaml.safe_dump(compose))
    return EnvironmentDefinition(root, root, manifest.task)


def options(request: FixtureRequest) -> OpenOptions:
    return OpenOptions(
        request_id=request.request_id,
        startup_timeout=request.timeout,
        lifetime=request.timeout,
        runtime_params=request.runtime_params,
        exec_env=request.exec_env,
        disable_internet=request.disable_internet,
    )


async def open_fixture(fleet: Flotilla, request: FixtureRequest) -> Environment:
    with TemporaryDirectory(prefix="flotilla-contract-") as directory:
        return await fleet.open_environment(definition(request.task, Path(directory)), options(request))


@asynccontextmanager
async def environment(fleet: Flotilla, request: FixtureRequest) -> AsyncIterator[Environment]:
    with TemporaryDirectory(prefix="flotilla-contract-") as directory:
        async with fleet.environment(definition(request.task, Path(directory)), options(request)) as env:
            yield env
