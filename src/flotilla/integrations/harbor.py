"""Harbor environments over Flotilla: a borrowed view, and the task environment that owns one.

Task executions enter through `NativeAttempt`, which owns the workspace and both
roles. `FlotillaEnvironment.view(handle, ...)` borrows one live handle for
Harbor-agent I/O -- exec, uploads, downloads and service stops -- without taking
lifecycle ownership: resources and network policy were fixed when the owner
opened the handle, and releasing it is the owner's business.

`FlotillaTaskEnvironment` is that owner, driven by Harbor's own trial loop:
`harbor run --env flotilla.integrations.harbor:FlotillaTaskEnvironment --ek
deployment=<module:attribute> --path <task under a declared dataset>`. The two
environment instances Harbor creates for one trial -- the agent's and a separate
verifier's -- find the same attempt through a registry keyed by the trial
directory, and the handoff between them is the attempt's own agent-phase gate.
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import json
import logging
import shutil
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.options import EnvironmentOptions
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy, TpuSpec
from harbor.models.trial.config import ResourceMode, ServiceVolumeConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from pydantic import Field

from flotilla import Flotilla
from flotilla._io import owned_io
from flotilla.config import FlotillaConfig
from flotilla.models.plan import TaskSource, TrialPlan
from flotilla.native import NativeAttempt
from flotilla.runtime.environment import Environment, Service
from flotilla.runtime.types import OpenOptions


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


#: Harbor builds a separate verifier's session id as f"{trial}__verifier__{key}".
_VERIFIER_SESSION_INFIX = "__verifier__"

#: The parameters `FlotillaEnvironment.__init__` binds; everything else in the factory call is
#: a user option. Keep in step with that signature.
_VIEW_PARAMETERS = frozenset(
    {
        "environment_dir",
        "environment_name",
        "session_id",
        "trial_paths",
        "task_env_config",
        "logger",
        "override_cpus",
        "override_memory_mb",
        "override_storage_mb",
        "override_gpus",
        "override_tpu",
        "cpu_enforcement_policy",
        "memory_enforcement_policy",
        "persistent_env",
        "mounts",
        "network_policy",
        "phase_network_policies",
        "extra_docker_compose",
        "stream",
        "enable_environment_dir_upload",
        "auto_mounts",
        "auto_mounting",
    }
)

_module_logger = logging.getLogger(__name__)

#: One trial's attempt while any of its Harbor environment instances lives, keyed by trial dir.
_registrations: dict[Path, _TrialRegistration] = {}
_fleets: dict[str, Flotilla] = {}
_configs: dict[str, FlotillaConfig] = {}
_exit_registered = False


@dataclass
class _TrialRegistration:
    """The attempt a trial runs on, plus the fleet it was created under."""

    attempt: NativeAttempt
    fleet: Flotilla
    target: str


class FlotillaTaskOptions(EnvironmentOptions):  # type: ignore[misc]
    """User-facing `--ek` options of the owning task environment.

    `deployment` names the Flotilla deployment the way everything else in flotilla does, as
    `package.module:attribute`. Credentials live in that module's spec; they never pass
    through environment kwargs.
    """

    deployment: str = Field(min_length=1)


async def shutdown() -> None:
    """Close every attempt and fleet this process opened through the task environment.

    Harbor's CLI can rely on interpreter exit; a process embedding harbor calls this to
    release the fleet's platform resources deterministically.
    """
    for key, registration in list(_registrations.items()):
        try:
            await registration.attempt.close()
        except Exception:
            _module_logger.exception("closing the attempt for %s failed", key)
        _registrations.pop(key, None)
    for target, fleet in list(_fleets.items()):
        try:
            report = await fleet.close()
        except Exception:
            _module_logger.exception("closing the fleet for %s failed", target)
        else:
            if not report.clean:
                _module_logger.warning("fleet %s left debris: %s", target, report)
        _fleets.pop(target, None)


def _shutdown_at_exit() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(shutdown())
    else:
        # An interpreter exiting with a live loop cannot be driven from here; the platform
        # reclaims whatever the fleet leaves behind.
        _module_logger.warning("flotilla fleets were still open at interpreter exit")


def _config_for(target: str) -> FlotillaConfig:
    """Load a deployment once per process; both instances of a trial share the spec."""
    cfg = _configs.get(target)
    if cfg is None:
        cfg = FlotillaConfig.load(target)
        _configs[target] = cfg
    return cfg


def _fleet_for(target: str) -> Flotilla:
    """One fleet per deployment for the whole process, started by the first attempt."""
    global _exit_registered
    fleet = _fleets.get(target)
    if fleet is None:
        fleet = Flotilla(deployment=_config_for(target))
        _fleets[target] = fleet
        if not _exit_registered:
            atexit.register(_shutdown_at_exit)
            _exit_registered = True
    return fleet


def _task_source(cfg: FlotillaConfig, task_root: Path) -> TaskSource:
    """Match the directory Harbor resolved a task from against the declared datasets.

    Harbor hands the environment a local directory; the attempt needs the dataset-relative
    key the platform stages from, which only the deployment's declared mapping supplies.
    """
    root = task_root.resolve()
    for dataset in cfg.resolve_datasets().values():
        local = dataset.local.resolve()
        if root == local:
            continue
        try:
            relative = root.relative_to(local)
        except ValueError:
            continue
        return dataset.source(relative.as_posix())
    raise ValueError(
        f"task directory {task_root} is under no dataset the deployment declares: flotilla grades "
        "published datasets only. Run harbor with --path on a checkout of a declared dataset; "
        "hub and git tasks are copied out of one."
    )


class FlotillaTaskEnvironment(FlotillaEnvironment):
    """The environment a Harbor trial runs on, owning the attempt underneath it.

    One process holds one fleet per deployment and one attempt per trial. The agent instance
    creates the attempt and opens the main role; a shared verifier -- grading inside the agent
    environment -- enters grading through its tests upload, which staging supersedes; a separate
    verifier instance stages and opens the grading role itself. Whichever instance is last
    closes the attempt.
    """

    options_model = FlotillaTaskOptions
    options: FlotillaTaskOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The view requires a resolved config; this environment owns loading it from its options.
        if "deployment" not in kwargs:
            raise ValueError(
                "the deployment option is required: --ek deployment=<module:attribute> naming "
                "the Flotilla deployment this trial runs on"
            )
        deployment = str(kwargs.pop("deployment"))
        # The view's signature is closed: validate the keys it will not bind here, so a mistyped
        # --ek option fails as an unknown option instead of a TypeError. Keep `_VIEW_PARAMETERS`
        # in step with the signature of `FlotillaEnvironment.__init__` above.
        unknown = {key: value for key, value in kwargs.items() if key not in _VIEW_PARAMETERS}
        model_input: dict[str, Any] = {"deployment": deployment, **unknown}
        if "auto_mounting" in kwargs:
            model_input.setdefault("auto_mounting", kwargs["auto_mounting"])
        # Setting `options` first makes the base keep this instance instead of re-parsing.
        self.options = FlotillaTaskOptions.model_validate(model_input)
        kwargs.pop("suppress_override_warnings", None)  # deprecated option name; the base warns
        # Distinct names: the harbor base rebinds `_override_cpus`/`_override_memory_mb` in
        # `BaseEnvironment.__init__`, and that folding target (`task_env_config`) does not size a
        # flotilla trial -- the fold below carries the override into `OpenOptions` instead.
        self._fold_cpus, self._fold_memory_mb = self._split_resource_overrides(kwargs)
        self._declared_policy = kwargs.get("network_policy")
        self._registration: _TrialRegistration | None = None
        self._grading_entered = False
        self._verifier_instance = False
        super().__init__(*args, cfg=_config_for(deployment), **kwargs)
        self._verifier_instance = _VERIFIER_SESSION_INFIX in self.session_id

    @staticmethod
    def _split_resource_overrides(kwargs: dict[str, Any]) -> tuple[int | None, int | None]:
        """Take the CPU and memory overrides Harbor may pass; refuse the rest.

        Flotilla sizes a trial from its task's Compose, and a run may widen CPU and memory
        (`OpenOptions.resources`), but there is no storage, GPU, or TPU knob to fold at all.
        """
        cpus = kwargs.pop("override_cpus", None)
        memory_mb = kwargs.pop("override_memory_mb", None)
        for name in ("override_storage_mb", "override_gpus", "override_tpu"):
            if kwargs.pop(name, None) is not None:
                raise ValueError(f"{name} is unsupported: a flotilla trial sizes itself from its task's Compose")
        return cpus, memory_mb

    @classmethod
    def preflight(cls, kwargs: dict[str, Any] | None = None) -> None:
        super().preflight(kwargs)
        options = cls.parse_options(kwargs)
        if options is not None:
            # Fail a bad deployment name or an unusable dataset declaration before queueing.
            _config_for(options.deployment).resolve_datasets()

    def _open_options(self, plan: TrialPlan) -> OpenOptions:
        override: dict[str, str] = {}
        if self._fold_cpus is not None:
            override["cpu"] = str(self._fold_cpus)
        if self._fold_memory_mb is not None:
            override["memory"] = f"{self._fold_memory_mb}MiB"
        no_network = self._declared_policy is not None and (
            self._declared_policy.network_mode == NetworkMode.NO_NETWORK
        )
        return OpenOptions(resources={plan.agent: override} if override else {}, disable_internet=no_network)

    async def start(self, force_build: bool = False) -> None:
        self._check_resources()
        if force_build:
            raise ValueError("force_build is unsupported: images come from the platform workspace compiler")
        if self._verifier_instance:
            await self._start_verifier_instance()
        else:
            await self._start_agent_instance()
        if not self._initialized:
            await self.initialize()
            self._initialized = True

    async def _start_agent_instance(self) -> None:
        key = self.trial_paths.trial_dir
        if key in _registrations:
            raise RuntimeError(f"a flotilla attempt is already registered for trial {key}")
        target = self.options.deployment
        fleet = _fleet_for(target)
        source = _task_source(fleet.config, self.environment_dir.parent)
        task = NativeAttempt.resolve(fleet, source)
        attempt = await NativeAttempt.create(fleet, task)
        try:
            self._environment = await attempt.open("main", self._open_options(task.plans["main"]))
        except BaseException as primary:
            try:
                await attempt.close()
            except BaseException as cleanup:
                primary.add_note(f"attempt cleanup after a failed open failed: {cleanup!r}")
            raise
        _registrations[key] = self._registration = _TrialRegistration(attempt, fleet, target)

    async def _start_verifier_instance(self) -> None:
        registration = _registrations.get(self.trial_paths.trial_dir)
        if registration is None:
            raise RuntimeError(
                f"no flotilla attempt is registered for trial {self.trial_paths.trial_dir}: "
                "verification runs on a trial this process opened, so regrades and grading in "
                "another process are unsupported"
            )
        attempt = registration.attempt
        verification = attempt.task.verification
        if verification is None:
            raise ValueError("the task declares no verification interface, so there is nothing to grade")
        if verification.role != "verifier":
            raise ValueError(
                "the task grades in the agent environment: a separate Harbor verifier needs "
                "tests/docker-compose.yaml or [verifier].environment.docker_image, or declare "
                '[verifier] environment_mode = "shared"'
            )
        # Idempotent: the agent instance's stop ended the phase and released main already.
        await attempt.end_agent_phase()
        await attempt.stage_verification()
        self._environment = await attempt.open_verification_role(
            self._open_options(attempt.task.plans[verification.role])
        )
        self._registration = registration

    async def stop(self, delete: bool = True) -> None:
        registration, self._registration = self._registration, None
        self._environment = None
        if registration is None:
            return
        attempt = registration.attempt
        verification = attempt.task.verification
        if self._verifier_instance or verification is None or verification.role == "main" or self._grading_entered:
            # Grading ran in main, finished here, or never runs: this instance ends the attempt.
            await attempt.close()
            _registrations.pop(self.trial_paths.trial_dir, None)
        else:
            # A separate verifier instance follows this one: release the agent's environment
            # now and keep the workspace for grading to read.
            await attempt.end_agent_phase()

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if self._grading_upload(target_dir):
            # Harbor's shared verifier uploads the tests it is about to run; the same published
            # tests are already staged for grading, so this upload is the handoff itself.
            await self._enter_grading()
            return
        await super().upload_dir(source_dir, target_dir)

    def _grading_upload(self, target_dir: str) -> bool:
        registration = self._registration
        if self._verifier_instance or registration is None or self._grading_entered:
            return False
        if registration.attempt.task.verification is None:
            return False
        return target_dir == str(EnvironmentPaths.for_os(self.os).tests_dir)

    async def _enter_grading(self) -> None:
        registration = self._registration
        assert registration is not None
        attempt = registration.attempt
        verification = attempt.task.verification
        assert verification is not None
        await attempt.end_agent_phase()
        await attempt.stage_verification()
        # Role "main" returns the environment the agent used; role "verifier" opens the separate
        # grading environment the agent phase released its budget for.
        self._environment = await attempt.open_verification_role(
            self._open_options(attempt.task.plans[verification.role])
        )
        self._grading_entered = True


__all__ = ["FlotillaEnvironment", "FlotillaTaskEnvironment", "FlotillaTaskOptions", "shutdown"]
