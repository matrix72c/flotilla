"""Harbor execution/file view over an open Flotilla environment.

Task executions enter through `NativeAttempt`, which owns the workspace and both
roles. `FlotillaEnvironment.view(handle, ...)` borrows one live handle for
Harbor-agent I/O -- exec, uploads, downloads and service stops -- without taking
lifecycle ownership: resources and network policy were fixed when the owner
opened the handle, and releasing it is the owner's business.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy, TpuSpec
from harbor.models.trial.config import ResourceMode, ServiceVolumeConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from flotilla._io import owned_io
from flotilla.config import FlotillaConfig
from flotilla.runtime.environment import Environment, Service


class FlotillaEnvironment(BaseEnvironment):  # type: ignore[misc]
    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        logger: logging.Logger | None = None,
        override_cpus: int | None = None,
        override_memory_mb: int | None = None,
        override_storage_mb: int | None = None,
        override_gpus: int | None = None,
        override_tpu: TpuSpec | None = None,
        cpu_enforcement_policy: ResourceMode = ResourceMode.AUTO,
        memory_enforcement_policy: ResourceMode = ResourceMode.AUTO,
        persistent_env: dict[str, str] | None = None,
        mounts: list[ServiceVolumeConfig] | None = None,
        network_policy: NetworkPolicy | None = None,
        phase_network_policies: Sequence[NetworkPolicy] | None = None,
        extra_docker_compose: Sequence[Path | str] | None = None,
        *,
        stream: bool = False,
        enable_environment_dir_upload: bool = True,
        auto_mounts: list[ServiceVolumeConfig] | None = None,
        auto_mounting: bool = False,
        cfg: FlotillaConfig,
    ) -> None:
        if any(
            value is not None
            for value in (override_cpus, override_memory_mb, override_storage_mb, override_gpus, override_tpu)
        ):
            raise ValueError("resource overrides are applied when the owner opens the environment, not here")
        if extra_docker_compose:
            raise ValueError("additional Compose overlays are unsupported")
        self._cfg = cfg
        self._environment: Environment | None = None
        self._initialized = False
        self._service_error: BaseException | None = None
        self._stopped: set[str] = set()
        self._stopping: dict[str, asyncio.Task[None]] = {}
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=copy.deepcopy(task_env_config),
            logger=logger,
            override_cpus=override_cpus,
            override_memory_mb=override_memory_mb,
            cpu_enforcement_policy=cpu_enforcement_policy,
            memory_enforcement_policy=memory_enforcement_policy,
            persistent_env=persistent_env,
            mounts=mounts,
            network_policy=network_policy,
            phase_network_policies=phase_network_policies,
            stream=stream,
            enable_environment_dir_upload=enable_environment_dir_upload,
            # Output paths Harbor would bind to the host; the base refuses auto_mounting since
            # capabilities do not declare `mounted`.
            auto_mounts=auto_mounts,
            auto_mounting=auto_mounting,
        )

    @staticmethod
    def type() -> str:
        return "flotilla"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            docker_compose=True,
            disable_internet=self._cfg.platform_capabilities().supports_no_network,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    def _validate_definition(self) -> None:
        # Borrowed views carry platform metadata, not a local task checkout.
        self._directory = Path(self.environment_dir)
        self._task_root = self._directory.parent

    @classmethod
    def view(
        cls,
        handle: Environment,
        *,
        cfg: FlotillaConfig,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
    ) -> FlotillaEnvironment:
        """Borrow an open environment for Harbor I/O, without taking lifecycle ownership."""
        view = cls(
            cfg=cfg,
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
        )
        view._environment = handle
        return view

    async def start(self, force_build: bool = False) -> None:
        """Prepare the borrowed handle's Harbor log directories; its owner already started it."""
        self._check_resources()
        if force_build:
            raise ValueError("force_build is unsupported: images come from the platform workspace compiler")
        if not self._initialized:
            await self.initialize()
            self._initialized = True

    async def initialize(self) -> None:
        self._save_metadata("environment", asdict(await self._env().info()))
        paths = EnvironmentPaths.for_os(self.os)
        # Cloud environments copy logs rather than bind-mounting them. Harbor
        # still expects these paths to exist and be writable by its role users.
        directories = [str(p) for p in (paths.agent_dir, paths.user_agent_dir, paths.verifier_dir, paths.artifacts_dir)]
        for command in (
            ["/.flotilla/bin/busybox", "mkdir", "-p", *directories],
            ["/.flotilla/bin/busybox", "chmod", "777", *directories],
        ):
            result = await self._env().agent.exec(command, uid=0, gid=0, cwd="/")
            if result.exit_code != 0 or result.timed_out:
                raise RuntimeError("failed to initialize Harbor log directories")

    def _save_metadata(self, name: str, data: dict[str, Any]) -> None:
        # Separate verifier instances share trial_paths; keep each resource record.
        target = self._local_dir() / f"{name}.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            self.logger.exception("Could not persist Flotilla diagnostics")

    def _local_dir(self) -> Path:
        # Session IDs are supplied by callers, so do not interpret them as paths.
        from urllib.parse import quote

        return Path(self.trial_paths.trial_dir) / "flotilla" / quote(self.session_id, safe="")

    def _cached(self, source: str, service: str | None) -> Path | None:
        if not self.is_main_service(service) or self._env().agent.name not in self._stopped:
            return None
        root = self._local_dir() / "stopped-main"
        path = root / source.lstrip("/")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("download path escapes stopped service snapshot")
        return path

    def _env(self) -> Environment:
        if self._environment is None:
            raise RuntimeError("environment is not started")
        return self._environment

    @property
    def handle(self) -> Environment:
        """The borrowed Flotilla environment, for native service execution by integrations."""
        return self._env()

    def _check_resources(self) -> None:
        if self._service_error is not None:
            raise RuntimeError("Harbor service termination was not confirmed") from self._service_error

    def _service(self, name: str | None = None) -> Service:
        return self._env().agent if self.is_main_service(name) else self._env().service(str(name))

    async def stop(self, delete: bool = True) -> None:
        """Detach from the borrowed handle. Its owner releases it; `delete` is the owner's decision."""
        self._environment = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        return await self.service_exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)

    async def service_exec(
        self,
        command: str,
        *,
        service: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self._check_resources()
        target = self._service(service)
        uid, gid = await self.execution_identity(user, service=service)
        result = await target.shell(
            command,
            shell="/bin/bash" if self.is_main_service(service) else "/bin/sh",
            cwd=cwd,
            env=self._merge_env(env),
            timeout=timeout_sec,
            uid=uid,
            gid=gid,
        )
        await self._env().check()
        if result.timed_out:
            raise TimeoutError("Harbor command exceeded its execution timeout")
        stdout, stderr = result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
        callback = self._output_callback()
        if callback is not None:
            if stdout:
                await callback(stdout, "stdout")
            if stderr:
                await callback(stderr, "stderr")
        return ExecResult(stdout=stdout, stderr=stderr, return_code=result.exit_code)

    async def execution_identity(
        self,
        user: str | int | None,
        *,
        service: str | None = None,
    ) -> tuple[int | None, int | None]:
        """Resolve Harbor's role identity for native Flotilla operations."""
        target = self._service(service)
        user = self._resolve_user(user)
        uid = gid = None
        if user is not None:
            raw = str(user)
            parts = raw.split(":")
            if len(parts) == 2 and all(part.isdecimal() for part in parts):
                uid, gid = map(int, parts)
            else:
                values = []
                for flag in ("-u", "-g"):
                    result = await target.exec(["/.flotilla/bin/busybox", "id", flag, raw])
                    if result.exit_code or not result.stdout.strip().isdigit():
                        raise ValueError(f"cannot resolve execution identity {raw!r}")
                    values.append(int(result.stdout.strip()))
                uid, gid = values
        return uid, gid

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await self._env().agent.upload_file(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await self._env().agent.upload_dir(source_dir, target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await self.service_download_file(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await self.service_download_dir(source_dir, target_dir)

    async def service_download_file(
        self, source_path: str, target_path: Path | str, *, service: str | None = None
    ) -> None:
        # Verifier reward reads pass through here; a broken environment cannot be accepted as a normal reward.
        self._check_resources()
        await self._env().check()
        cached = self._cached(source_path, service)
        if cached is None:
            await self._service(service).download_file(source_path, target_path)
        else:
            await owned_io(shutil.copyfile, cached, target_path)
        await self._env().check()

    async def service_download_dir(
        self, source_dir: str, target_dir: Path | str, *, service: str | None = None
    ) -> None:
        self._check_resources()
        await self._env().check()
        cached = self._cached(source_dir, service)
        if cached is None:
            await self._service(service).download_dir(source_dir, target_dir)
        else:
            await owned_io(shutil.copytree, cached, target_dir, dirs_exist_ok=True)
        await self._env().check()

    async def stop_service(self, service: str) -> None:
        target = self._service(service)
        if target.name not in self._stopping:

            async def stop() -> None:
                try:
                    if self.is_main_service(service):
                        # Harbor syncs logs after stopping main to collect sidecars.
                        # Task artifacts have already been collected by Harbor.
                        paths = EnvironmentPaths.for_os(self.os)
                        for directory in (
                            paths.agent_dir,
                            paths.user_agent_dir,
                            paths.verifier_dir,
                        ):
                            destination = self._local_dir() / "stopped-main" / str(directory).lstrip("/")
                            await target.download_dir(str(directory), destination)
                    await target.terminate()
                    self._stopped.add(target.name)
                except BaseException as exc:
                    self._service_error = exc
                    raise

            self._stopping[target.name] = asyncio.create_task(stop())
            self._stopping[target.name].add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
        await asyncio.shield(self._stopping[target.name])
