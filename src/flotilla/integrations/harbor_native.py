"""Native Harbor task-source and trial-runtime extension for task releases.

Requires Harbor's explicit task_source/runtime interfaces. No global registries,
local task uploads, source task keys, or patches to Harbor internals are used.
Selection is a portable release ``dataset.jsonl``; Harbor presentation is rebuilt
from each task's ``provenance.harbor.config``; execution and verification follow
the release through :class:`flotilla.native.NativeAttempt`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from harbor.models.task.config import TaskOS
from harbor.models.task.task import Task
from harbor.models.trial.config import TaskConfig as TrialTaskConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.models.verifier.result import VerifierResult
from harbor.tasks.client import TaskDownloadResult
from harbor.verifier.verifier import RewardFileEmptyError, RewardFileNotFoundError, VerifierOutputParseError

from flotilla.config import FlotillaConfig
from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.native import BoundTask, NativeAttempt, ReleaseBindings
from flotilla.runtime.client import Flotilla
from flotilla.runtime.types import CleanupError, OpenOptions

REWARD_JSON = "reward.json"
REWARD_TEXT = "reward.txt"


def _supported(config: Any) -> None:
    if config.steps:
        raise ValueError("Flotilla currently supports single-step Harbor tasks only")
    if config.environment.os != TaskOS.LINUX:
        raise ValueError("Flotilla currently supports Linux Harbor tasks only")


class FlotillaTaskSource:
    """Release selection: one Harbor trial task per dataset row, in row order."""

    def __init__(self, *, dataset: str | Path, deployment: str | Path, metadata_workers: int = 8) -> None:
        if isinstance(metadata_workers, bool) or not isinstance(metadata_workers, int) or metadata_workers < 1:
            raise ValueError("metadata_workers must be a positive integer")
        if not hasattr(Task, "from_metadata"):
            raise RuntimeError("native Flotilla requires Harbor with task_source/runtime extension support")
        self.dataset = Path(dataset).expanduser()
        self.deployment = Path(deployment).expanduser()
        self.metadata_workers = metadata_workers
        self._bindings: ReleaseBindings | None = None
        # Absolute release task payload directories of loaded tasks.
        self.bounds: dict[Path, BoundTask] = {}

    def _resolve_input(self, value: Path, name: str, base_dirs: dict[str, Path | None] | None) -> Path:
        if value.is_absolute():
            return value
        base = Path.cwd() if base_dirs is None else base_dirs.get(name, Path.cwd())
        if base is None:
            raise ValueError(f"relative {name} requires a local config file; use absolute paths in remote configs")
        return base / value

    def _init_bindings(self, config: Any) -> None:
        if self._bindings is not None:
            return
        source_config = config.task_source
        base_dirs = None if source_config is None else source_config.kwargs_base_dirs
        dataset = self._resolve_input(self.dataset, "dataset", base_dirs)
        deployment = self._resolve_input(self.deployment, "deployment", base_dirs)
        bindings = FlotillaConfig.load(deployment).release_bindings()
        # Snapshots carry resolved external paths, not the invocation's CWD.
        self.dataset, self.deployment, self._bindings = dataset, deployment, bindings
        if source_config is not None:
            source_config.kwargs["dataset"] = str(dataset)
            source_config.kwargs["deployment"] = str(deployment)

    @property
    def bindings(self) -> ReleaseBindings:
        if self._bindings is None:
            raise RuntimeError("task source must resolve or load tasks before accessing its bindings")
        return self._bindings

    async def resolve_task_configs(self, config: Any) -> list[Any]:
        self._init_bindings(config)
        if config.tasks or config.datasets:
            raise ValueError(
                "native releases replace Harbor task/dataset discovery; select rows in the release dataset.jsonl"
            )
        return await self._resolve_selection()

    async def _resolve_selection(self) -> list[TrialTaskConfig]:
        rows = await asyncio.to_thread(self.bindings.read_dataset, self.dataset)
        if not rows:
            raise ValueError("the release dataset selects no tasks")
        # Validate every selected row before the job starts: duplicates and order
        # are preserved verbatim, and a broken row is never silently dropped.
        semaphore = asyncio.Semaphore(self.metadata_workers)

        async def resolve(row: Any) -> BoundTask:
            async with semaphore:
                return await asyncio.to_thread(self.bindings.resolve, row)

        bound_tasks = await asyncio.gather(*(resolve(row) for row in rows))
        tasks = []
        for bound in bound_tasks:
            directory = (bound.reader.root / PurePosixPath(bound.row.task_ref).parent).absolute()
            self.bounds.setdefault(directory, bound)
            tasks.append(TrialTaskConfig(path=directory, source=bound.row.release_id))
        return tasks

    async def load_task(self, config: Any) -> tuple[Task, TaskDownloadResult]:
        self._init_bindings(config)
        task_config = config.task
        if task_config.path is None or task_config.is_git_task() or task_config.is_package_task():
            raise ValueError("native trials point at release task payloads, not remote downloads")
        # A restored standalone Trial has no in-memory Job selection cache.
        if not self.bounds:
            await self._resolve_selection()
        bound = self._bound_for(task_config.path)
        provenance = bound.task.provenance or {}
        harbor = provenance.get("harbor") if isinstance(provenance, dict) else None
        presentation = harbor.get("config") if isinstance(harbor, dict) else None
        if provenance.get("importer") != "harbor-v1" or not isinstance(presentation, dict):
            raise ValueError("release task lacks harbor-v1 presentation provenance")
        from harbor.models.task.config import TaskConfig

        parsed = TaskConfig.model_validate(presentation)
        _supported(parsed)
        instruction = await asyncio.to_thread(bound.instruction)
        task = Task.from_metadata(
            Path(task_config.path),
            config=parsed,
            instruction=instruction,
            extra_instruction_paths=config.extra_instruction_paths,
            extra_instructions=config.extra_instructions,
        )
        return task, TaskDownloadResult(path=Path(task_config.path), download_time_sec=0, cached=True)

    def _bound_for(self, path: Path) -> BoundTask:
        try:
            return self.bounds[Path(path).absolute()]
        except KeyError:
            raise ValueError(f"{path} is not a task payload of this release selection") from None

    def bound_for(self, task: Task) -> BoundTask:
        """The release binding a loaded task came from, for its trial runtime."""
        return self._bound_for(Path(task.task_dir))


def _safe_output(name: str) -> PurePosixPath:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
        raise ValueError(f"verification output name escapes the verifier directory: {name!r}")
    return relative


def _parse_rewards(outputs: dict[str, bytes], paths: TrialPaths) -> dict[str, float | int]:
    """Harbor reward semantics over the release's declared verification outputs."""
    if REWARD_JSON in outputs:
        payload = outputs[REWARD_JSON]
        if not payload:
            raise RewardFileEmptyError(f"Reward file is empty at {paths.reward_json_path}")
        try:
            rewards = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise VerifierOutputParseError(f"Failed to parse rewards from JSON file {paths.reward_json_path}") from exc
        if not isinstance(rewards, dict):
            raise VerifierOutputParseError(f"Rewards must be a JSON object in {paths.reward_json_path}")
        for value in rewards.values():
            if not isinstance(value, (int, float)):
                raise VerifierOutputParseError(f"Non-numeric reward {value!r} in JSON file {paths.reward_json_path}")
            if isinstance(value, float) and not math.isfinite(value):
                raise VerifierOutputParseError(f"Non-finite reward {value!r} in JSON file {paths.reward_json_path}")
        return rewards
    if REWARD_TEXT in outputs:
        payload = outputs[REWARD_TEXT]
        if not payload:
            raise RewardFileEmptyError(f"Reward file is empty at {paths.reward_text_path}")
        try:
            value = float(payload.decode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise VerifierOutputParseError(f"Failed to parse rewards from text file {paths.reward_text_path}") from exc
        if not math.isfinite(value):
            raise VerifierOutputParseError(f"Non-finite reward {value!r} in text file {paths.reward_text_path}")
        return {"reward": value}
    raise RewardFileNotFoundError(f"No reward file found at {paths.reward_text_path} or {paths.reward_json_path}")


class NativeFlotillaRuntime:
    """One explicit owner per native Trial, shared by its environment and verifier."""

    def __init__(
        self,
        *,
        task: Task,
        config: Any,
        paths: TrialPaths,
        context_id: UUID,
        deployment: str,
        run_timeout: float | None = None,
        runtime_params: dict[str, str] | None = None,
    ) -> None:
        if config.task_source is None:
            raise ValueError("native Flotilla requires a configured FlotillaTaskSource")
        source = config.task_source.resolve()
        if not isinstance(source, FlotillaTaskSource):
            raise ValueError("native Flotilla requires FlotillaTaskSource")
        if config.verifier.import_path is not None:
            raise ValueError(
                "native releases bake verification at prepare time; verifier.import_path cannot replace it"
            )
        if config.verifier.env:
            raise ValueError("verifier.env is baked into the release; prepare a new release to change it")
        if not config.environment.delete:
            raise ValueError("native Flotilla always releases trial resources; environment.delete must be true")
        self.bound = source.bound_for(task)
        self.config = config
        self.task = task
        self.paths = paths
        self.context_id = context_id
        self.deployment = deployment
        deployment_config = FlotillaConfig.load(deployment)
        self.run_timeout = deployment_config.runtime.run_timeout_s if run_timeout is None else run_timeout
        if not 0 < self.run_timeout <= deployment_config.runtime.run_timeout_s:
            raise ValueError("run_timeout must be positive and within the deployment lifetime")
        self.runtime_params = dict(runtime_params or {})
        self.fleet = Flotilla(deployment=deployment_config)
        self.attempt: NativeAttempt | None = None
        self._main: NativeFlotillaEnvironment | None = None
        self._creation_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._main_ready = False
        self._collection_started = False
        from harbor.trial.artifact_handler import ArtifactHandler

        self.artifacts = [*task.config.artifacts, *config.artifacts]
        self._artifact_handler = ArtifactHandler(artifacts=self.artifacts, logger=logging.getLogger(__name__))
        self._artifact_handler.begin_collection()

    async def open_main(self, environment: NativeFlotillaEnvironment) -> None:
        async with self._creation_lock:
            if self._close_task is not None:
                raise RuntimeError("trial runtime is closing")
            if environment._environment is not None:
                return
            if self.attempt is not None:
                raise RuntimeError("native Trial already has a main environment")
            self.attempt = await NativeAttempt.create(self.fleet, self.bound)
            environment._environment = await self.attempt.open(
                "main",
                OpenOptions(
                    request_id=environment.session_id,
                    lifetime=self.run_timeout,
                    resources={"main": environment._resource_overrides} if environment._resource_overrides else {},
                    runtime_params=self.runtime_params,
                    disable_internet=environment._network_policy.network_mode == "no-network",
                ),
            )
            self._main = environment
            await environment.initialize()
            self._main_ready = True

    async def _collect_artifacts(self) -> None:
        if self._main is None:
            raise RuntimeError("artifact collection requires a started main environment")
        main = self._main
        hooks = list(self.task.config.verifier.collect)
        sidecars = set(self._artifact_handler.sidecar_services())
        sidecars |= {hook.service for hook in hooks if hook.service != "main"}

        async def collect(services: set[str]) -> None:
            for hook in hooks:
                if hook.service in services:
                    result = await main.service_exec(
                        hook.command, service=hook.service, timeout_sec=int(hook.timeout_sec), user=hook.user
                    )
                    if result.return_code != 0:
                        raise RuntimeError(f"artifact collection failed in service {hook.service!r}")
            await self._artifact_handler.download_artifacts(
                main,
                self.paths.artifacts_dir,
                source_artifacts_dir=EnvironmentPaths.for_os(main.os).artifacts_dir,
                services=services,
            )

        await collect({"main"})
        if sidecars:
            await collect(sidecars)

    async def verify(
        self, main_environment: Any, timeout_sec: float | None, *, logger: logging.Logger, log_context: Any
    ) -> VerifierResult:
        if self.attempt is None or self._main is not main_environment:
            raise RuntimeError("verification requires this trial's started main environment")
        timeout = self.run_timeout if timeout_sec is None else timeout_sec
        self._collection_started = True
        # Persist agent results first: an isolated verifier releases main inside
        # the attempt, and a failing verification must not lose them.
        await self._collect_artifacts()
        result = await self.attempt.verify(timeout=timeout)
        logger.info("native verification exited with %d", result.exit_code)
        verifier_dir = self.paths.verifier_dir
        verifier_dir.mkdir(parents=True, exist_ok=True)
        (verifier_dir / "test-stdout.txt").write_text(result.stdout)
        (verifier_dir / "test-stderr.txt").write_text(result.stderr)
        for name, payload in result.outputs.items():
            target = verifier_dir / str(_safe_output(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return VerifierResult(rewards=_parse_rewards(result.outputs, self.paths))

    async def close(self) -> None:
        if self._close_task is None:

            async def cleanup() -> None:
                primary: BaseException | None = None
                try:
                    # Disabled verification, install-only, and agent failures still
                    # retain results before deleting the attempt.
                    if self._main_ready and not self._collection_started and self._main is not None:
                        self._collection_started = True
                        async with asyncio.timeout(self.run_timeout):
                            await self._collect_artifacts()
                except BaseException as exc:
                    primary = exc
                try:
                    if self.attempt is not None:
                        await self.attempt.close()
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note(f"attempt cleanup also failed: {exc!r}")
                try:
                    report = await self.fleet.close()
                    if not report.clean:
                        raise CleanupError(report)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note(f"fleet cleanup also failed: {exc!r}")
                if primary is not None:
                    raise primary

            self._close_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError as primary:
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                self._close_task.result()
            except Exception as cleanup_error:
                primary.add_note(f"runtime cleanup also failed: {cleanup_error!r}")
            raise


class NativeFlotillaEnvironment(FlotillaEnvironment):
    """Native factory entry point; the explicit Trial runtime owns its resources."""

    def __init__(self, *, runtime_context: NativeFlotillaRuntime, role: str, **kwargs: Any) -> None:
        if not isinstance(runtime_context, NativeFlotillaRuntime) or role != "main":
            raise ValueError("native environment requires its trial runtime and main role")
        self.runtime_context = runtime_context
        super().__init__(deployment=runtime_context.deployment, fleet=runtime_context.fleet, **kwargs)
        self._owns_environment = False
        self._owns_fleet = False

    async def initialize(self) -> None:
        # NativeAttempt owns declared artifact/output directories and their modes.
        # The legacy adapter creates every Harbor log directory in main, including
        # an isolated verifier's output. That both bypasses the role contract and
        # fails when main has a readonly rootfs. Do not mutate undeclared paths.
        self._save_metadata("environment", asdict(await self._env().info()))

    async def start(self, force_build: bool = False) -> None:
        if force_build:
            raise ValueError("force_build is unsupported by consume-only native releases")
        await self.runtime_context.open_main(self)
