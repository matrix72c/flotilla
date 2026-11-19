"""Consume-only attempts on the existing Flotilla scheduler and Anchor lifecycle."""

from __future__ import annotations

import asyncio
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from harbor.models.task.config import TaskConfig

from flotilla.compose.task import TASK_FILE
from flotilla.compose.verification import artifacts_of, verification_of
from flotilla.core.plan import TaskSource, TrialPlan
from flotilla.integrations.harbor_source import task_environments
from flotilla.native.models import Artifact, Verification, check_against_plans
from flotilla.platform.base import SharedVolume
from flotilla.runtime.types import OpenOptions
from flotilla.runtime.workspace import Workspace

if TYPE_CHECKING:
    from flotilla.runtime.client import Flotilla
    from flotilla.runtime.environment import Environment
    from flotilla.runtime.manager import Manager
    from flotilla.runtime.workspace import WorkspaceInfo


@dataclass(frozen=True)
class VerificationResult:
    exit_code: int
    stdout: str
    stderr: str
    outputs: dict[str, bytes]


#: Read verbatim from the task directory: the agent's brief and the grading script's home.
INSTRUCTION = "instruction.md"
TESTS_DIR = "tests"


def _task_config(path: Path) -> TaskConfig:
    """The task's own `task.toml`, as Harbor's model, for the `[verifier]` table."""
    return TaskConfig.model_validate(tomllib.loads((path / TASK_FILE).read_text()))


@dataclass(frozen=True)
class NativeTask:
    """A task resolved into everything a graded attempt needs: one plan per role, plus grading.

    `main` is the environment the agent works in. `verifier` exists only when the task declares a
    separate verifier environment; otherwise grading runs in `main`, in the sandbox the agent just
    used, which is what a task without a second environment is asking for.
    """

    key: str
    source: TaskSource
    plans: dict[str, TrialPlan]
    verification: Verification | None
    artifacts: tuple[Artifact, ...]
    instruction: str


async def _create(manager: Manager, task: NativeTask) -> WorkspaceInfo:
    """Create the attempt's private workspace and the directories its roles hand each other."""
    info = await manager.workspaces.create_empty(task.key)
    try:
        for artifact in task.artifacts:
            plan = task.plans[artifact.role]
            context = plan.units[artifact.service or plan.agent].context
            key = f"{info.key}/artifacts/{artifact.name}"
            await manager.anchor.mkdir(key, stage="prepare")
            await manager.anchor.chown(key, context.uid, context.gid, stage="prepare")
            await manager.anchor.chmod(key, 0o755, stage="prepare")
        verification = task.verification
        if verification:
            plan = task.plans[verification.role]
            context = plan.units[verification.service or plan.agent].context
            for name in ("tests", "verification"):
                key = f"{info.key}/{name}"
                await manager.anchor.mkdir(key, stage="prepare")
                await manager.anchor.chmod(key, 0o755, stage="prepare")
            await manager.anchor.chown(
                f"{info.key}/verification",
                verification.uid if verification.uid is not None else context.uid,
                verification.gid if verification.gid is not None else context.gid,
                stage="prepare",
            )
        return info
    except BaseException as primary:
        try:
            await manager.release_workspace(info.id)
        except BaseException as cleanup:
            primary.add_note(f"native workspace cleanup failed: {cleanup!r}")
        raise


class NativeAttempt:
    """One private attempt at a graded task. The caller must stop its agent before calling verify.

    The task directory is never copied: each role's plan mounts it read-only, straight from
    shared storage. What this class owns is the attempt's own workspace -- the artifact
    directories, the staged tests, the verifier's output -- and the handoff between roles.
    """

    def __init__(self, fleet: Flotilla, task: NativeTask, workspace: Workspace) -> None:
        self.fleet, self.task = fleet, task
        self.workspace = workspace
        self.environments: dict[str, Environment] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._verification_started = False
        self._handoff = False

    @classmethod
    def resolve(cls, fleet: Flotilla, source: TaskSource, options: OpenOptions | None = None) -> NativeTask:
        """Read a task directory into one plan per role, plus how it is graded.

        Pure and synchronous, so a caller can resolve a whole dataset and find the tasks that do
        not plan before spending a single sandbox on any of them.
        """
        parameters = {**fleet.config.runtime_params, **(options.parameters if options else {})}
        environments = task_environments(source.local, compose_environment=parameters)
        if len(environments) > 2:
            raise ValueError("a task has at most two environments: the agent's and a separate verifier's")
        roles = ["main"] if len(environments) == 1 else ["main", "verifier"]
        plans = {
            role: fleet.plan_environment(environment, source, options)
            for role, environment in zip(roles, environments, strict=True)
        }
        config = _task_config(source.local)
        has_tests = (source.local / TESTS_DIR).is_dir()
        verification = verification_of(config, roles=plans, has_tests=has_tests)
        artifacts = artifacts_of(has_tests=has_tests)
        check_against_plans(verification, artifacts, plans=plans)
        instruction = (source.local / INSTRUCTION).read_text(encoding="utf-8")
        return NativeTask(
            key=source.key or source.local.name,
            source=source,
            plans=plans,
            verification=verification,
            artifacts=artifacts,
            instruction=instruction,
        )

    @classmethod
    async def create(
        cls, fleet: Flotilla, source: TaskSource | NativeTask, options: OpenOptions | None = None
    ) -> NativeAttempt:
        task = source if isinstance(source, NativeTask) else cls.resolve(fleet, source, options)
        await fleet.start()
        info = await fleet._call(
            lambda manager: _create(manager, task),
            settle_cancel=True,
            cancel_result=lambda manager, value: manager.release_workspace(value.id),
        )
        return cls(fleet, task, Workspace(fleet, info))

    def _options(self, role: str, options: OpenOptions | None) -> OpenOptions:
        options = options or OpenOptions()
        if options.workspace_id is not None or options.workspace_mounts:
            raise ValueError("native attempt owns workspace mounts")
        mounts: dict[str, list[SharedVolume]] = {}
        plan = self.task.plans[role]
        for artifact in self.task.artifacts:
            if artifact.role == role:
                service = artifact.service or plan.agent
                mounts.setdefault(service, []).append(
                    SharedVolume(
                        key=f"{self.workspace.key}/artifacts/{artifact.name}",
                        mount_path=artifact.target,
                        read_only=False,
                    )
                )
            elif role == "verifier" and self._handoff:
                verifier_service = self.task.verification.service if self.task.verification else None
                mounts.setdefault(verifier_service or plan.agent, []).append(
                    SharedVolume(
                        key=f"{self.workspace.key}/handoff/{artifact.name}",
                        mount_path=artifact.verifier_target or artifact.target,
                        read_only=True,
                    )
                )
        verification = self.task.verification
        if verification and verification.role == role:
            service = verification.service or plan.agent
            mounts.setdefault(service, []).append(
                SharedVolume(
                    key=f"{self.workspace.key}/verification", mount_path=verification.output_dir, read_only=False
                )
            )
            if verification.tests:
                mounts.setdefault(service, []).append(
                    SharedVolume(
                        key=f"{self.workspace.key}/tests", mount_path=verification.tests_target, read_only=True
                    )
                )
        return replace(
            options,
            workspace_id=self.workspace.id,
            workspace_root=self.workspace.key,
            workspace_mounts={service: tuple(items) for service, items in mounts.items()},
        )

    async def open(self, role: str = "main", options: OpenOptions | None = None) -> Environment:
        async with self._lock:
            return await self._open(role, options)

    async def _open(self, role: str, options: OpenOptions | None) -> Environment:
        if self._closed:
            raise RuntimeError("native attempt is closed")
        if role not in self.task.plans:
            raise ValueError(f"unknown native role: {role}")
        if role == "verifier" and not self._verification_started:
            raise ValueError("open the isolated verifier through verify() after agent completion")
        if (
            role == "main"
            and self._verification_started
            and self.task.verification
            and self.task.verification.role != "main"
        ):
            raise RuntimeError("cannot reopen main after verifier handoff")
        if role not in self.environments:
            self.environments[role] = await self.fleet.open(self.task.plans[role], self._options(role, options))
        return self.environments[role]

    async def _stage_verification(self) -> None:
        verification = self.task.verification
        assert verification is not None

        async def stage(manager: Manager) -> None:
            async def action(info: WorkspaceInfo) -> None:
                # Clear pre-existing outputs without replacing the mounted directory inode.
                result = await manager.anchor.execute(
                    (
                        "python3",
                        "-I",
                        "-m",
                        "flotilla.native.platform_verify",
                        "--clear-directory",
                        manager.anchor.path(f"{info.key}/verification"),
                    ),
                    timeout_s=manager.cfg.prepare.workspace_timeout,
                    stage="prepare",
                )
                if result.exit_code != 0 or result.timed_out:
                    raise RuntimeError("could not clear verification output directory")
                if verification.tests:
                    await manager.anchor.copy_contents(
                        f"{self.task.source.key}/{verification.tests}",
                        f"{info.key}/tests",
                        stage="prepare",
                        src_root=self.task.source.root,
                    )
                if verification.role == "verifier":
                    for artifact in self.task.artifacts:
                        if artifact.role != "verifier":
                            await manager.anchor.copy_tree(
                                f"{info.key}/artifacts/{artifact.name}",
                                f"{info.key}/handoff/{artifact.name}",
                                stage="prepare",
                            )

            await manager.workspaces.operation(self.workspace.id, action)

        await self.fleet._call(stage, settle_cancel=True)
        self._handoff = True

    async def verify(self, options: OpenOptions | None = None, *, timeout: float | None = None) -> VerificationResult:
        async with self._lock:
            if self._closed or self._verification_started:
                raise RuntimeError("verification may run only once on an open attempt")
            verification = self.task.verification
            if verification is None:
                raise ValueError("task has no verification interface")
            self._verification_started = True
            if verification.role == "verifier":
                await self._release("main")
            await self._stage_verification()
            environment = await self._open(verification.role, options)
            plan = self.task.plans[verification.role]
            service = environment.service(verification.service or plan.agent)
            result = await service.exec(
                verification.command,
                timeout=timeout or verification.timeout_s,
                env=verification.env,
                uid=verification.uid,
                gid=verification.gid,
            )
            if result.timed_out:
                raise TimeoutError("native verification timed out")
            outputs: dict[str, bytes] = {}
            for name, path in verification.output_paths.items():
                outputs[name] = await service.read_file(path)
            for name, path in verification.optional_output_paths.items():
                # An explicit existence probe distinguishes missing optional outputs
                # from file-channel/infrastructure errors, which must propagate.
                exists = await service.exec(("test", "-f", path), timeout=30)
                if exists.timed_out or exists.exit_code not in (0, 1):
                    raise RuntimeError("verification output existence check failed")
                if exists.exit_code == 0:
                    outputs[name] = await service.read_file(path)
            if verification.require_output and not outputs:
                raise ValueError("verification produced no declared output")
            return VerificationResult(
                result.exit_code,
                result.stdout.decode(errors="replace"),
                result.stderr.decode(errors="replace"),
                outputs,
            )

    async def check(self) -> None:
        for environment in tuple(self.environments.values()):
            await environment.check()

    async def download_artifact(self, name: str, target: str | Path) -> None:
        artifact = next((a for a in self.task.artifacts if a.name == name), None)
        if artifact is None:
            raise ValueError("unknown artifact")
        role = artifact.role
        path = artifact.target
        if role not in self.environments and "verifier" in self.environments and self._handoff:
            role = "verifier"
            path = artifact.verifier_target or path
        plan = self.task.plans[role]
        service = artifact.service if role == artifact.role else None
        await self.environments[role].service(service or plan.agent).download_dir(path, target)

    async def _release(self, role: str) -> None:
        environment = self.environments.get(role)
        if environment is not None:
            await environment.release()
            del self.environments[role]

    async def release(self, role: str) -> None:
        async with self._lock:
            await self._release(role)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            # Workspace.close releases all registered dependent environments and
            # retains ownership on failure for launch cleanup/GC retries.
            await self.workspace.close()
            self.environments.clear()
            self._closed = True

    async def __aenter__(self) -> NativeAttempt:
        return self

    async def __aexit__(self, typ: object, exc: BaseException | None, tb: object) -> None:
        try:
            await self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc.add_note(f"native attempt cleanup failed: {cleanup!r}")
