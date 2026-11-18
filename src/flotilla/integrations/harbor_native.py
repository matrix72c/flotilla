"""Native Harbor task-source and trial-runtime extension over a published dataset.

Requires Harbor's explicit task_source/runtime interfaces. No global registries,
local task uploads, source task keys, or patches to Harbor internals are used.
Selection names a dataset the deployment declares, optionally narrowed by a file
of task keys; Harbor presentation is each task's own ``task.toml``; execution and
verification go through :class:`flotilla.native.NativeAttempt`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import tomllib
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
from flotilla.core.plan import TaskSource
from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.native import Dataset, NativeAttempt
from flotilla.native.runtime import INSTRUCTION
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
    """Dataset selection: one Harbor trial task per task directory, in a stable order.

    `dataset` names a dataset the deployment declares. `selection` optionally narrows it to the
    task keys listed in a text file, one per line, in that file's order; without it every task
    in the dataset is selected. Nothing is copied: a selected task stays where it is published.
    """

    def __init__(
        self, *, dataset: str, deployment: str | Path, selection: str | Path | None = None, metadata_workers: int = 8
    ) -> None:
        if isinstance(metadata_workers, bool) or not isinstance(metadata_workers, int) or metadata_workers < 1:
            raise ValueError("metadata_workers must be a positive integer")
        if not hasattr(Task, "from_metadata"):
            raise RuntimeError("native Flotilla requires Harbor with task_source/runtime extension support")
        self.dataset = dataset
        self.deployment = Path(deployment).expanduser()
        self.selection = None if selection is None else Path(selection).expanduser()
        self.metadata_workers = metadata_workers
        self._resolved: Dataset | None = None
        # Absolute task directories of selected tasks, keyed by where they are on this host.
        self.sources: dict[Path, TaskSource] = {}

    def _resolve_input(self, value: Path, name: str, base_dirs: dict[str, Path | None] | None) -> Path:
        if value.is_absolute():
            return value
        base = Path.cwd() if base_dirs is None else base_dirs.get(name, Path.cwd())
        if base is None:
            raise ValueError(f"relative {name} requires a local config file; use absolute paths in remote configs")
        return base / value

    def _init_dataset(self, config: Any) -> None:
        if self._resolved is not None:
            return
        source_config = config.task_source
        base_dirs = None if source_config is None else source_config.kwargs_base_dirs
        deployment = self._resolve_input(self.deployment, "deployment", base_dirs)
        selection = None if self.selection is None else self._resolve_input(self.selection, "selection", base_dirs)
        declared = FlotillaConfig.load(deployment).resolve_datasets()
        if self.dataset not in declared:
            raise ValueError(f"the deployment declares no dataset {self.dataset!r}; declared: {sorted(declared)}")
        # Snapshots carry resolved external paths, not the invocation's CWD.
        self.deployment, self.selection, self._resolved = deployment, selection, declared[self.dataset]
        if source_config is not None:
            source_config.kwargs["deployment"] = str(deployment)
            if selection is not None:
                source_config.kwargs["selection"] = str(selection)

    @property
    def tasks(self) -> Dataset:
        if self._resolved is None:
            raise RuntimeError("task source must resolve or load tasks before accessing its dataset")
        return self._resolved

    async def resolve_task_configs(self, config: Any) -> list[Any]:
        self._init_dataset(config)
        if config.tasks or config.datasets:
            raise ValueError("a published dataset replaces Harbor task/dataset discovery; narrow it with `selection`")
        return await self._resolve_selection()

    def _keys(self) -> list[str]:
        if self.selection is None:
            return self.tasks.tasks()
        lines = self.selection.read_text(encoding="utf-8").splitlines()
        keys = [stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")]
        if not keys:
            raise ValueError(f"{self.selection} selects no tasks")
        return keys

    async def _resolve_selection(self) -> list[TrialTaskConfig]:
        keys = await asyncio.to_thread(self._keys)
        # Validate every selected key before the job starts: duplicates and order
        # are preserved verbatim, and a broken key is never silently dropped.
        semaphore = asyncio.Semaphore(self.metadata_workers)

        async def resolve(key: str) -> TaskSource:
            async with semaphore:
                return await asyncio.to_thread(self.tasks.source, key)

        sources = await asyncio.gather(*(resolve(key) for key in keys))
        configs = []
        for source in sources:
            directory = source.local.absolute()
            self.sources.setdefault(directory, source)
            configs.append(TrialTaskConfig(path=directory, source=self.dataset))
        return configs

    async def load_task(self, config: Any) -> tuple[Task, TaskDownloadResult]:
        self._init_dataset(config)
        task_config = config.task
        if task_config.path is None or task_config.is_git_task() or task_config.is_package_task():
            raise ValueError("native trials point at published task directories, not remote downloads")
        # A restored standalone Trial has no in-memory Job selection cache.
        if not self.sources:
            await self._resolve_selection()
        source = self._source_for(task_config.path)
        parsed = await asyncio.to_thread(_read_task_config, source.local)
        _supported(parsed)
        instruction = await asyncio.to_thread(_read_instruction, source.local)
        task = Task.from_metadata(
            Path(task_config.path),
            config=parsed,
            instruction=instruction,
            extra_instruction_paths=config.extra_instruction_paths,
            extra_instructions=config.extra_instructions,
        )
        return task, TaskDownloadResult(path=Path(task_config.path), download_time_sec=0, cached=True)

    def _source_for(self, path: Path) -> TaskSource:
        try:
            return self.sources[Path(path).absolute()]
        except KeyError:
            raise ValueError(f"{path} is not a task directory of this selection") from None

    def source_for(self, task: Task) -> TaskSource:
        """Where a loaded task's files are, on both sides, for its trial runtime."""
        return self._source_for(Path(task.task_dir))


def _read_task_config(path: Path) -> Any:
    from harbor.models.task.config import TaskConfig

    return TaskConfig.model_validate(tomllib.loads((path / "task.toml").read_text(encoding="utf-8")))


def _read_instruction(path: Path) -> str:
    return (path / INSTRUCTION).read_text(encoding="utf-8")


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
            raise ValueError("verification comes from the task's own [verifier] table; import_path cannot replace it")
        if config.verifier.env:
            raise ValueError("verifier.env comes from the task's own [verifier] table and cannot be overridden")
        if runtime_params:
            raise ValueError("runtime parameters belong to the deployment; set them in [runtime_params]")
        if not config.environment.delete:
            raise ValueError("native Flotilla always releases trial resources; environment.delete must be true")
        self.source = source.source_for(task)
        self.config = config
        self.task = task
        self.paths = paths
        self.context_id = context_id
        self.deployment = deployment
        deployment_config = FlotillaConfig.load(deployment)
        self.run_timeout = deployment_config.runtime.run_timeout_s if run_timeout is None else run_timeout
        if not 0 < self.run_timeout <= deployment_config.runtime.run_timeout_s:
            raise ValueError("run_timeout must be positive and within the deployment lifetime")
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
            self.attempt = await NativeAttempt.create(self.fleet, self.source)
            environment._environment = await self.attempt.open(
                "main",
                OpenOptions(
                    request_id=environment.session_id,
                    lifetime=self.run_timeout,
                    resources={"main": environment._resource_overrides} if environment._resource_overrides else {},
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
