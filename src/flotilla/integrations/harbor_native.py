"""Native Harbor task-source and trial-runtime extension for platform originals.

Requires Harbor's explicit task_source/runtime interfaces. No global registries,
prepared datasets, local task uploads, or patches to Harbor internals are used.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from harbor.models.task.config import TaskOS
from harbor.models.task.id import LocalTaskId
from harbor.models.task.task import Task
from harbor.models.trial.config import TaskConfig as TrialTaskConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.tasks.client import TaskDownloadResult

from flotilla.config import FlotillaConfig
from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.integrations.harbor_attempt import HarborAttempt, WorkspaceVerifier
from flotilla.integrations.harbor_source_reader import NativeTaskSource, SourceMapping, metadata_identity
from flotilla.integrations.harbor_verification import HarborVerification
from flotilla.runtime.client import Flotilla
from flotilla.runtime.types import CleanupError, OpenOptions


class FlotillaTaskSource:
    """Original local directory selection, with lazy instruction loading per trial."""

    def __init__(self, *, sources: list[SourceMapping | dict[str, Any]], metadata_workers: int = 8) -> None:
        if isinstance(metadata_workers, bool) or not isinstance(metadata_workers, int) or metadata_workers < 1:
            raise ValueError("metadata_workers must be a positive integer")
        if not hasattr(Task, "from_metadata"):
            raise RuntimeError("native Flotilla requires Harbor with task_source/runtime extension support")
        self.sources = tuple(SourceMapping.model_validate(source) for source in sources)
        self._reader: NativeTaskSource | None = None
        self.metadata_workers = metadata_workers
        self.identities: dict[str, str] = {}

    @property
    def reader(self) -> NativeTaskSource:
        if self._reader is None:
            raise RuntimeError("task source must resolve or load tasks before accessing its reader")
        return self._reader

    def _init_reader(self, config: Any) -> None:
        if self._reader is not None:
            return
        source_config = config.task_source
        base_dir = (
            source_config.kwargs_base_dirs.get("sources", Path.cwd()) if source_config is not None else Path.cwd()
        )
        sources = []
        for source in self.sources:
            root = source.local_root.expanduser()
            if not root.is_absolute():
                if base_dir is None:
                    raise ValueError(
                        "relative local_root requires a local config file; use absolute roots in remote configs"
                    )
                root = base_dir / root
            sources.append(source.model_copy(update={"local_root": root}))
        self._reader = NativeTaskSource(sources)
        if source_config is not None:
            # Snapshots carry resolved external roots, not the invocation's CWD.
            source_config.kwargs["sources"] = [source.model_dump(mode="json") for source in self.reader.sources]

    @staticmethod
    def _supported(config: Any) -> None:
        if config.steps:
            raise ValueError("Flotilla currently supports single-step Harbor tasks only")
        if config.environment.os != TaskOS.LINUX:
            raise ValueError("Flotilla currently supports Linux Harbor tasks only")

    def _key(self, config: Any) -> str:
        if config.path is None or config.is_git_task() or config.is_package_task():
            raise ValueError("Flotilla task source requires original local task paths, not remote downloads")
        return self.reader.key_for_path(config.path)

    async def resolve_task_configs(self, config: Any) -> list[Any]:
        self._init_reader(config)
        tasks = [task.model_copy(deep=True) for task in config.tasks]
        for dataset in config.datasets:
            if not dataset.is_local():
                raise ValueError("Flotilla datasets must name a local original Harbor directory")
            root = dataset.path.expanduser().resolve(strict=True)

            # No Task.is_valid_dir: only shallow candidate discovery. A selected
            # candidate with malformed config is an error, never a silently lost task.
            def discover(directory: Path) -> list[LocalTaskId]:
                return [
                    LocalTaskId(path=path)
                    for path in sorted(directory.iterdir())
                    if path.is_dir() and (path / "task.toml").exists()
                ]

            candidates = await asyncio.to_thread(discover, root)
            tasks.extend(
                TrialTaskConfig(path=task.path, source=root.name) for task in dataset._filter_task_ids(candidates)
            )
        if not tasks:
            raise ValueError("Either datasets or tasks must select at least one task")
        keys = [self._key(task) for task in tasks]
        configs = await self.reader.read_configs(keys, workers=self.metadata_workers)
        for key, parsed in configs.items():
            try:
                self._supported(parsed)
            except ValueError as exc:
                raise ValueError(f"unsupported task {key!r}: {exc}") from exc
        return tasks

    async def load_task(self, config: Any) -> tuple[Task, TaskDownloadResult]:
        self._init_reader(config)
        key = self._key(config.task)
        description = await asyncio.to_thread(self.reader.read_metadata, key)
        from harbor.models.task.config import TaskConfig

        parsed = TaskConfig.model_validate(description["task_config"])
        self._supported(parsed)
        path = self.reader.resolve_task(key)
        task = Task.from_metadata(
            path,
            config=parsed,
            instruction=description["instruction"],
            extra_instruction_paths=config.extra_instruction_paths,
            extra_instructions=config.extra_instructions,
        )
        self.identities[key] = metadata_identity(description)
        return task, TaskDownloadResult(path=path, download_time_sec=0, cached=True)


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
        if config.verifier.import_path not in (None, "flotilla.integrations.harbor_native:NativeWorkspaceVerifier"):
            raise ValueError("native Flotilla requires its platform-staged verifier adapter")
        if not config.environment.delete:
            raise ValueError("native Flotilla always releases trial resources; environment.delete must be true")
        self.key = source.reader.key_for_path(task.task_dir)
        self.expected_identity = source.identities[self.key]
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
        self.attempt: HarborAttempt | None = None
        self.verification: HarborVerification | None = None
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
            self.attempt = await HarborAttempt.create(
                self.fleet,
                self.key,
                runtime_params=self.runtime_params,
                expected_metadata_identity=self.expected_identity,
            )
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
            self.verification = HarborVerification(
                attempt=self.attempt,
                main=environment,
                paths=self.paths,
                deployment=self.deployment,
                run_timeout=self.run_timeout,
            )
            await environment.initialize()
            self._main_ready = True

    async def _save_artifacts(self, services: set[str]) -> None:
        if self.verification is None:
            raise RuntimeError("artifact collection requires a started main environment")
        main = self.verification.main
        # Result persistence only; verifier handoff stays entirely on the platform.
        await self._artifact_handler.download_artifacts(
            main,
            self.paths.artifacts_dir,
            source_artifacts_dir=EnvironmentPaths.for_os(main.os).artifacts_dir,
            services=services,
        )

    async def verify(
        self, main_environment: Any, timeout_sec: float | None, *, logger: logging.Logger, log_context: Any
    ) -> Any:
        if self.verification is None or self.verification.main is not main_environment:
            raise RuntimeError("verification requires this trial's started main environment")
        from harbor.verifier.factory import VerifierFactory

        verifier_config = self.config.verifier.model_copy(
            update={"import_path": "flotilla.integrations.harbor_native:NativeWorkspaceVerifier"}
        )

        def create_verifier(**kwargs: Any) -> Any:
            return VerifierFactory.create_verifier_from_config(
                verifier_config,
                task=self.task,
                trial_paths=self.paths,
                environment=kwargs["environment"],
                override_env=self.config.verifier.env or None,
                logger=logger,
                skip_tests_upload=kwargs["bundled_tests"],
                runtime_context=self,
                role="verifier",
            )

        self._collection_started = True
        return await self.verification.verify(
            timeout=self.run_timeout if timeout_sec is None else timeout_sec,
            artifacts=self.artifacts,
            on_collect=self._save_artifacts,
            log_context=log_context,
            verifier_factory=create_verifier,
        )

    async def close(self) -> None:
        if self._close_task is None:

            async def cleanup() -> None:
                primary: BaseException | None = None
                try:
                    # Disabled verification, install-only, and agent failures still
                    # retain results before deleting the attempt. Never replay hooks
                    # after an interrupted verification collection.
                    if self._main_ready and not self._collection_started and self.verification is not None:
                        self._collection_started = True
                        async with asyncio.timeout(self.run_timeout):
                            await self.verification.collect_artifacts(
                                artifacts=self.artifacts, on_collect=self._save_artifacts
                            )
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

    async def start(self, force_build: bool = False) -> None:
        if force_build:
            raise ValueError("force_build is unsupported by the platform workspace compiler")
        await self.runtime_context.open_main(self)


class NativeWorkspaceVerifier(WorkspaceVerifier):
    """Bridge Harbor's native verifier factory to its trial's platform-staged tests."""

    def __init__(self, *, runtime_context: NativeFlotillaRuntime, role: str, **kwargs: Any) -> None:
        if role != "verifier" or runtime_context.attempt is None:
            raise ValueError("native verifier requires an active trial attempt")
        attempt = runtime_context.attempt
        definition = attempt.roles.get("verifier", attempt.roles["main"])
        # The native Trial retains its real Task; only this platform file view
        # uses script coordinates supplied by the already-prepared attempt.
        kwargs["task"] = attempt.task
        super().__init__(attempt=attempt, bundled_tests=definition.bundled_tests, **kwargs)
