"""One platform-owned Harbor attempt, shared by its agent and verifier roles.

Only metadata crosses to the client. Task inputs, tests and artifact handoffs stay
on platform storage; the private source tree is never mounted into a task pod.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.task.config import EnvironmentConfig, TaskConfig
from harbor.verifier.verifier import Verifier

from flotilla.manifest import Manifest
from flotilla.platform.base import SharedVolume
from flotilla.prepared import PreparedEnvironment
from flotilla.runtime.types import OpenOptions

INSTRUCTION_MOUNT = "/harbor-instruction"
HANDOFF_MOUNT = "/harbor-handoff"

if TYPE_CHECKING:
    from flotilla.runtime.client import Flotilla
    from flotilla.runtime.environment import Environment
    from flotilla.runtime.workspace import Workspace


def task_key(value: str) -> str:
    """Validate a platform-relative key without silently normalizing traversal."""
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("task_key must be a nonempty relative POSIX storage path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError("task_key must be relative to platform storage, without '.' or '..'")
    return value


@dataclass(frozen=True)
class RemoteTaskView:
    """Harbor config and remote script metadata, deliberately not a filesystem Task."""

    name: str
    config: TaskConfig
    instruction: str
    test_script: str | None

    @property
    def short_name(self) -> str:
        return self.name


@dataclass(frozen=True)
class Role:
    directory: str
    config: EnvironmentConfig
    bundled_tests: bool
    prepared: PreparedEnvironment


class WorkspaceVerifier(Verifier):  # type: ignore[misc]  # Harbor has no supported typing boundary.
    """Use native execution/reward handling with explicitly platform-staged tests.

    Native Verifier's local path discovery/upload is replaced, not faked with
    empty local directories. The returned paths are lexical script coordinates;
    no method traverses them on the training machine.
    """

    task: RemoteTaskView
    environment: BaseEnvironment

    def __init__(self, *, attempt: HarborAttempt, bundled_tests: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempt = attempt
        self.bundled_tests = bundled_tests
        if not bundled_tests:
            self.environment = _StagedTestsEnvironment(self.environment, self.task.test_script)

    def _resolve_tests(self) -> tuple[list[Path], Path, Path]:
        if not self.bundled_tests and not self.attempt.tests_ready:
            raise RuntimeError("workspace tests have not been staged for verification")
        script = "test.sh" if self.bundled_tests else self.task.test_script
        if not script:
            raise FileNotFoundError("platform task metadata has no verifier test script")
        relative = PurePosixPath(script)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid platform test script metadata")
        root = Path("/tests")
        return [], root, root / script


class _StagedTestsEnvironment:
    """Native verifier view: executable permissions were set before the RO mount."""

    def __init__(self, environment: BaseEnvironment, script: str | None) -> None:
        from harbor.utils.scripts import quote_shell_arg

        self._environment = environment
        self._chmod = "chmod +x " + quote_shell_arg(f"/tests/{script}", environment.os)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._environment, name)

    async def exec(self, command: str, **kwargs: Any) -> ExecResult:
        if command == self._chmod and kwargs == {"user": "root"}:
            return ExecResult(stdout="", stderr="", return_code=0)
        return await self._environment.exec(command, **kwargs)


class HarborAttempt:
    def __init__(
        self,
        fleet: Flotilla,
        workspace: Workspace,
        metadata: dict[str, Any],
        runtime_params: dict[str, str] | None,
    ) -> None:
        self.fleet = fleet
        self.workspace = workspace
        self.runtime_params = dict(runtime_params or {})
        config = TaskConfig.model_validate(metadata["task_config"])
        if config.steps:
            raise ValueError("training supports single-step Harbor tasks only")
        self.task = RemoteTaskView(
            name=workspace.task_id,
            config=config,
            instruction=metadata["instruction"],
            test_script=metadata.get("tests", {}).get("script"),
        )
        self.roles = {
            name: Role(
                directory=definition["directory"],
                config=EnvironmentConfig.model_validate(definition["config"]),
                bundled_tests=definition["bundled_tests"],
                prepared=PreparedEnvironment(Manifest.load(metadata["environments"][name])),
            )
            for name, definition in metadata["definitions"].items()
        }
        if set(self.roles) not in ({"main"}, {"main", "verifier"}):
            raise ValueError("platform task requires main and at most one verifier role")
        if set(metadata["environments"]) != set(self.roles):
            raise ValueError("platform task role definitions and environments disagree")
        if self.roles["main"].bundled_tests:
            raise ValueError("agent environment cannot contain bundled hidden tests")
        self.tests_directory = metadata.get("tests", {}).get("directory", "private-tests")
        self.tests_ready = False
        self._handles: dict[str, Environment] = {}
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def create(
        cls,
        fleet: Flotilla,
        key: str,
        *,
        runtime_params: dict[str, str] | None = None,
        expected_metadata_identity: str | None = None,
    ) -> HarborAttempt:
        workspace = await fleet.workspace(task_key(key))
        try:
            metadata = await workspace.prepare(runtime_params=runtime_params)
            if expected_metadata_identity is not None:
                from flotilla.integrations.harbor_source_reader import metadata_identity

                if metadata_identity(metadata) != expected_metadata_identity:
                    raise ValueError(f"host/platform instruction or task config differs for {key!r}")
            attempt = cls(fleet, workspace, metadata, runtime_params)
            await workspace.mkdir("tests")
            for service in attempt.roles["main"].prepared.manifest().units:
                await workspace.mkdir(attempt.scratch_relative(service))
            return attempt
        except BaseException as primary:
            try:
                await workspace.close()
            except BaseException as cleanup:
                primary.add_note(f"workspace cleanup also failed: {cleanup!r}")
            raise

    @staticmethod
    def scratch_relative(service: str) -> str:
        # Service names are metadata, not storage paths.
        from urllib.parse import quote

        return "handoff/" + quote(service, safe="")

    def _volume(self, relative: str, target: str, *, read_only: bool) -> SharedVolume:
        return SharedVolume(
            key=f"{self.workspace.key}/{relative}",
            mount_path=target,
            read_only=read_only,
        )

    def options(self, role: str, options: OpenOptions | None = None) -> OpenOptions:
        options = options or OpenOptions()
        if options.workspace_id is not None or options.workspace_mounts:
            raise ValueError("HarborAttempt owns workspace mounts")
        definition = self.roles[role]
        manifest = definition.prepared.manifest()
        mounts = {}
        if role == "main":
            for name in manifest.units:
                mounts[name] = [self._volume(self.scratch_relative(name), HANDOFF_MOUNT, read_only=False)]
            mounts[manifest.agent_service].append(self._volume("public", INSTRUCTION_MOUNT, read_only=True))
        else:
            mounts[manifest.agent_service] = [self._volume("handoff", HANDOFF_MOUNT, read_only=True)]
        if not definition.bundled_tests:
            mounts.setdefault(manifest.agent_service, []).append(self._volume("tests", "/tests", read_only=True))
        for name, owned in mounts.items():
            existing = [m.target for m in manifest.units[name].mounts]
            existing += [m.mount_path for m in manifest.units[name].tmpfs_mounts]
            existing += [m.mount_path for m in options.external_mounts.get(name, ())]
            for mount in owned:
                path = PurePosixPath(mount.mount_path)
                if any(
                    path == PurePosixPath(other)
                    or path in PurePosixPath(other).parents
                    or PurePosixPath(other) in path.parents
                    for other in existing
                ):
                    raise ValueError(f"Harbor workspace mount overlaps task content: {name}:{path}")
        # Bundled tests are image-owned. An explicit mount at/above/below /tests
        # would replace those tests and is never an acceptable reinterpretation.
        if definition.bundled_tests:
            paths = [m.target for m in manifest.units[manifest.agent_service].mounts]
            paths += [m.mount_path for m in manifest.units[manifest.agent_service].tmpfs_mounts]
            paths += [m.mount_path for m in options.external_mounts.get(manifest.agent_service, ())]
            if any(
                PurePosixPath(p) == PurePosixPath("/tests")
                or PurePosixPath(p) in PurePosixPath("/tests").parents
                or PurePosixPath("/tests") in PurePosixPath(p).parents
                for p in paths
            ):
                raise ValueError("bundled verifier tests conflict with a task mount")
        return replace(
            options,
            workspace_id=self.workspace.id,
            workspace_mounts={name: tuple(values) for name, values in mounts.items()},
            runtime_params=self.runtime_params,
        )

    async def open(self, role: str = "main", options: OpenOptions | None = None) -> Environment:
        if role in self._handles or self._close_task is not None:
            raise RuntimeError("Harbor role is already open or attempt is closing")
        if role == "verifier" and not self.roles[role].bundled_tests and not self.tests_ready:
            raise RuntimeError("stage verifier tests before creating the verifier")
        handle = await self.fleet.open(self.roles[role].prepared, self.options(role, options))
        self._handles[role] = handle
        return handle

    async def stage_tests(self) -> None:
        role = self.roles.get("verifier", self.roles["main"])
        if role.bundled_tests or self.tests_ready:
            return
        if not self.task.test_script:
            raise FileNotFoundError("task has no platform verifier test script")
        await self.workspace.copy_contents(self.tests_directory, "tests")
        self.tests_ready = True

    async def release(self, role: str) -> None:
        handle = self._handles.get(role)
        if handle is not None:
            await handle.release()
            del self._handles[role]

    async def close(self) -> None:
        if self._close_task is None:

            async def cleanup() -> None:
                errors = []
                for role in tuple(self._handles):
                    try:
                        await self.release(role)
                    except BaseException as exc:
                        errors.append(exc)
                try:
                    await self.workspace.close()
                except BaseException as exc:
                    errors.append(exc)
                if errors:
                    for error in errors[1:]:
                        errors[0].add_note(f"cleanup also failed: {error!r}")
                    raise errors[0]

            self._close_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError as primary:
            # Release waiters may be cancelled, but the attempt must not return
            # while its workspace cleanup is still racing its owner.
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
                primary.add_note(f"attempt cleanup also failed: {cleanup_error!r}")
            raise
