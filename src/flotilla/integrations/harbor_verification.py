"""Platform-side artifact handoff and verification shared by Harbor and training."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.models.task.artifacts import effective_artifact_service, normalize_artifact_entries
from harbor.models.trial.artifact_manifest import ArtifactManifest, ArtifactManifestEntry
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.integrations.harbor_attempt import HANDOFF_MOUNT, HarborAttempt, WorkspaceVerifier
from flotilla.runtime.types import OpenOptions


class WorkspaceArtifacts:
    """Harbor destination rules with archives in per-service platform scratch."""

    def __init__(self, attempt: HarborAttempt, artifacts: Any, convention: Any) -> None:
        from harbor.trial.artifact_handler import ArtifactHandler

        self.attempt = attempt
        self.handler = ArtifactHandler(artifacts=artifacts, logger=logging.getLogger(__name__))
        self.handler.begin_collection()
        self.convention = str(convention)
        self.entries: list[Any] = []
        self.collected: list[tuple[Any, str, str, bool]] = []
        self.required = {
            (effective_artifact_service(entry), entry.source) for entry in normalize_artifact_entries(artifacts)
        }

    def sidecar_services(self) -> set[str]:
        return set(self.handler.sidecar_services())

    async def collect(self, source: Any, services: set[str]) -> Any:
        root = Path("artifacts")  # lexical destination, never traversed locally
        for artifact in self.handler._normalized_artifacts(None, self.convention):
            service = effective_artifact_service(artifact)
            if service not in services:
                continue
            target = self.handler._host_path(root, artifact, self.convention)
            conflict = self.handler._find_conflicting_claim(target, artifact.source)
            entry = ArtifactManifestEntry(
                source=artifact.source,
                destination=self.handler._manifest_destination(root, target),
                type="directory",
                status="skipped" if conflict else "failed",
                service=artifact.service,
                exclude=list(artifact.exclude),
            )
            if not conflict:
                self.handler._claimed_targets.append((target, artifact.source))
                is_dir = await source.service_is_dir(artifact.source, service=artifact.service, user="root")
                entry.type = "directory" if is_dir else "file"
                service_name = source.handle.agent.name if service == "main" else service
                archive = f"{len(self.entries)}.tar.gz"
                destination = f"{HANDOFF_MOUNT}/{archive}"
                path = PurePosixPath(artifact.source)
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("artifact source must be an absolute sandbox path")
                if is_dir:
                    arguments = ["tar", "czf", destination]
                    arguments += [f"--exclude={pattern}" for pattern in artifact.exclude]
                    arguments += ["-C", str(path), "."]
                else:
                    arguments = ["tar", "czf", destination, "-C", str(path.parent), path.name]
                result = await source.service_exec(
                    shlex.join(arguments), service=artifact.service, user="root", timeout_sec=120
                )
                if result.return_code == 0:
                    entry.status = "ok"
                    self.collected.append((artifact, service_name, archive, is_dir))
            self.entries.append(entry)
            if (service, artifact.source) in self.required and entry.status not in {"ok", "empty"}:
                raise RuntimeError(f"required artifact was not collected from {service}:{artifact.source}")
        manifest = ArtifactManifest(entries=self.entries)
        await self.attempt.workspace.write("artifact-manifest.json", manifest.model_dump_json().encode())
        return manifest

    async def restore(self, target: Any, target_convention: Any) -> None:
        for artifact, service, archive, is_dir in self.collected:
            source = self.handler._upload_target_source(
                artifact, source_convention=self.convention, target_convention=str(target_convention)
            )
            if is_dir:
                await target.empty_dirs([source], chmod=True)
                directory = source
            else:
                directory = str(PurePosixPath(source).parent)
                await target.ensure_dirs([directory], chmod=True)
            relative = self.attempt.scratch_relative(service).removeprefix("handoff/")
            result = await target.exec(
                shlex.join(["tar", "xzf", f"{HANDOFF_MOUNT}/{relative}/{archive}", "-C", directory]),
                user="root",
                timeout_sec=120,
            )
            if result.return_code:
                raise RuntimeError(f"failed to restore artifact at {source!r}")


class HarborVerification:
    """Own role transitions, while HarborAttempt owns the workspace lifetime."""

    def __init__(
        self,
        *,
        attempt: HarborAttempt,
        main: FlotillaEnvironment,
        paths: TrialPaths,
        deployment: str,
        run_timeout: float,
    ) -> None:
        self.attempt = attempt
        self.main = main
        self.paths = paths
        self.deployment = deployment
        self.run_timeout = run_timeout
        self.main_closed = False
        self.active_verifier: FlotillaEnvironment | None = None

    async def check(self) -> None:
        if not self.main_closed:
            await self.main.handle.check()
        if self.active_verifier is not None:
            await self.active_verifier.handle.check()

    async def release_main(self) -> None:
        if not self.main_closed:
            await self.attempt.release("main")
            self.main_closed = True

    @contextlib.asynccontextmanager
    async def verifier_environment(self, *, deadline: float) -> AsyncIterator[tuple[FlotillaEnvironment, bool]]:
        definition = self.attempt.roles.get("verifier")
        if definition is None:
            await self.attempt.stage_tests()
            yield self.main, False
            return
        # No still-running main/sidecar can observe hidden tests becoming available.
        await self.release_main()
        await self.attempt.stage_tests()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("verifier deadline expired before environment creation")
        primary: BaseException | None = None
        try:
            async with asyncio.timeout_at(deadline):
                handle = await self.attempt.open(
                    "verifier",
                    OpenOptions(
                        admission_timeout=remaining,
                        startup_timeout=remaining,
                        request_id=self.main.session_id + "-verifier",
                        lifetime=self.run_timeout,
                        runtime_params=self.attempt.runtime_params,
                    ),
                )
                verifier = FlotillaEnvironment.view(
                    handle,
                    environment_dir=Path(definition.directory),
                    environment_name=self.attempt.task.short_name,
                    session_id=self.main.session_id + "-verifier",
                    trial_paths=self.paths,
                    task_env_config=definition.config,
                    deployment=self.deployment,
                    fleet=self.attempt.fleet,
                )
                self.active_verifier = verifier
                await verifier.initialize()
            yield verifier, definition.bundled_tests
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                await self.attempt.release("verifier")
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"verifier cleanup also failed: {cleanup!r}")
            finally:
                self.active_verifier = None

    async def collect_artifacts(
        self,
        *,
        artifacts: Any = None,
        on_collect: Callable[[set[str]], Awaitable[None]] | None = None,
    ) -> WorkspaceArtifacts:
        task, main = self.attempt.task, self.main
        env_paths = EnvironmentPaths.for_os(main.os)
        handler = WorkspaceArtifacts(
            self.attempt, task.config.artifacts if artifacts is None else artifacts, env_paths.artifacts_dir
        )
        separate = "verifier" in self.attempt.roles
        hooks = task.config.verifier.collect
        sidecars = handler.sidecar_services() | {hook.service for hook in hooks if hook.service != "main"}

        async def collect(services: set[str]) -> None:
            for hook in hooks:
                if hook.service in services:
                    result = await main.service_exec(
                        hook.command, service=hook.service, timeout_sec=int(hook.timeout_sec), user=hook.user
                    )
                    if result.return_code != 0:
                        raise RuntimeError(f"artifact collection failed in service {hook.service!r}")
            await handler.collect(main, services)
            if on_collect is not None:
                await on_collect(services)

        await collect({"main"})
        if sidecars:
            if separate:
                await main.stop_service("main")
            await collect(sidecars)
        return handler

    async def verify(
        self,
        *,
        timeout: float,
        artifacts: Any = None,
        on_collect: Callable[[set[str]], Awaitable[None]] | None = None,
        on_ready: Callable[[FlotillaEnvironment], Awaitable[None]] | None = None,
        on_result: Callable[[Any], None] | None = None,
        log_context: Callable[[FlotillaEnvironment], Any] | None = None,
        verifier_kwargs: dict[str, Any] | None = None,
        verifier_factory: Callable[..., Any] | None = None,
    ) -> Any:
        task = self.attempt.task
        separate = "verifier" in self.attempt.roles
        deadline = asyncio.get_running_loop().time() + timeout
        async with asyncio.timeout_at(deadline):
            handler = await self.collect_artifacts(artifacts=artifacts, on_collect=on_collect)
            async with self.verifier_environment(deadline=deadline) as (target, bundled_tests):
                if on_ready is not None:
                    await on_ready(target)
                target_paths = EnvironmentPaths.for_os(target.os)
                await target.empty_dirs([target_paths.verifier_dir], chmod=True)
                if separate:
                    await handler.restore(target, target_paths.artifacts_dir)
                verifier = (verifier_factory or WorkspaceVerifier)(
                    attempt=self.attempt,
                    bundled_tests=bundled_tests,
                    task=task,
                    trial_paths=self.paths,
                    environment=target,
                    **(verifier_kwargs or {}),
                )
                with (
                    target.with_default_user(task.config.verifier.user),
                    log_context(target) if log_context is not None else contextlib.nullcontext(),
                ):
                    result = await verifier.verify()
                await target.handle.check()
                if on_result is not None:
                    on_result(result)
                return result
