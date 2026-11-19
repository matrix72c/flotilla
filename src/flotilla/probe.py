"""Measure the isolation a deployment provides. Everything else is assumed until it refuses.

A functional capability announces its own absence: ask for a device the platform has no runtime
for, a TTL beyond its limit, or storage it cannot mount, and the create fails. Measuring those in
advance only bought a nicer error message, at the price of a report to keep current.

Isolation does not announce anything. A deployment that ignores an egress policy, mounts storage
the sandbox was never given, or serves its exec channel to anyone who connects returns success and
simply does not have the restriction. Only measurement distinguishes that from a working one, so
that is all this probe does.

The result is an `IsolationEvidence` in memory and this process's exit status. There is no report
file: a client in another process cannot re-derive these values, so checking isolation is a step an
operator takes against a deployment, not something a training run rediscovers at startup.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flotilla.config import FlotillaConfig

from flotilla.capabilities import Evidence, IsolationEvidence, ProbeSettings
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.waiting import wait_running, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    ProcessSpec,
    Resources,
    SharedVolume,
)
from flotilla.share.release import ensure_builtin_release

PROBE_LABEL = "flotilla/probe"  # run id; with the launch and unit labels it identifies a probed instance
_EXEC_TIMEOUT_S = 30.0
_MEMORY = "512Mi"
_NONE = ExternalPolicy(mode="none")
_PATH = "/usr/local/bin:/usr/bin:/bin"


async def probe_deployment(deployment: str | FlotillaConfig, *, image: str | None = None) -> IsolationEvidence:
    """Measure isolation and enforce what this deployment accepts missing.

    Cleanup is part of the verdict, not best-effort work afterwards: a deployment that cannot be
    cleaned up leaks every run.
    """
    from flotilla.config import FlotillaConfig
    from flotilla.platform.clock import SystemClock

    cfg = deployment if isinstance(deployment, FlotillaConfig) else FlotillaConfig.load(deployment)
    settings = cfg.probe_settings()
    if image is not None:
        settings = replace(settings, image=image)
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=cfg.management_capabilities())
    anchor = Anchor(platform, clock, f"probe-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    evidence = IsolationEvidence()
    cleanup_errors: list[str] = []
    directory = f"launches/{anchor.launch_id}"
    try:
        await anchor.start()
        if cfg.share.builtin:
            # Every probe sandbox mounts the share tools, so the release must exist first.
            await ensure_builtin_release(anchor)
        await anchor.mkdir(directory, stage="prepare")
        evidence = await probe(platform, clock, anchor, settings)
    finally:

        async def remove_launch() -> None:
            await anchor.remove(directory, stage="run")

        for operation in (remove_launch, anchor.close, platform.aclose):
            try:
                await operation()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
    if cleanup_errors:
        evidence = replace(
            evidence,
            cleanup=Evidence(False, "anchor/storage cleanup unconfirmed: " + ", ".join(cleanup_errors)),
        )
    cfg.check_isolation(evidence)
    return evidence


async def probe(platform: Platform, clock: Clock, anchor: Anchor, settings: ProbeSettings) -> IsolationEvidence:
    """Measure this deployment's isolation. Never import a historical or hand-written verdict."""
    from flotilla.probe_checks import measure

    run = _Run(platform, clock, anchor, settings, run_id=f"p{uuid.uuid4().hex[:10]}")
    evidence = IsolationEvidence()
    notes: list[str] = []
    cancelled = None
    try:
        async with asyncio.timeout(settings.total_timeout_s):
            evidence = await measure(run)
    except asyncio.CancelledError as exc:
        cancelled = exc
        notes.append("probe cancelled")
    except Exception as exc:
        # Exception text and remote responses may contain tokens; retain only the category.
        notes.append(f"probe interrupted ({type(exc).__name__})")
    finally:
        task = asyncio.create_task(run.cleanup())
        try:
            leaked = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
            leaked = await task
    if leaked:
        notes.append(f"instances not confirmed deleted: {', '.join(leaked)}")
    if cancelled is not None:
        if leaked:
            cancelled.add_note(notes[-1])
        raise cancelled
    return replace(
        evidence,
        cleanup=Evidence(not leaked, "deletion confirmed" if not leaked else notes[-1]),
        notes=(*evidence.notes, *notes),
    )


def _mounted_subdir(entry: str, subdir: str) -> bool:
    """Whether mountinfo "source|root" mounts `subdir`: the source, or source joined with root, ends with it."""
    source, _, root = entry.partition("|")
    joined = source.rstrip("/") + ("" if root in ("", "/") else root)
    return any(path.rstrip("/").endswith("/" + subdir) for path in (source, joined))


def _sh(
    script: str, *, uid: int = 0, gid: int = 0, cwd: str = "/", timeout_s: float = _EXEC_TIMEOUT_S, **env: str
) -> ProcessSpec:
    return ProcessSpec(
        argv=("/bin/sh", "-c", script), uid=uid, gid=gid, cwd=cwd, env={"PATH": _PATH, **env}, timeout_s=timeout_s
    )


@dataclass
class _Run:
    platform: Platform
    clock: Clock
    anchor: Anchor
    settings: ProbeSettings
    run_id: str
    privileged: bool = False

    def __post_init__(self) -> None:
        self._created: list[str] = []
        self._uncertain_create = False

    def _spec(
        self,
        name: str,
        volumes: tuple[SharedVolume, ...] = (),
        *,
        external: ExternalPolicy = _NONE,
        group: str | None = None,
    ) -> InstanceSpec:
        share = SharedVolume(
            key=f"share/releases/{self.settings.share_release}", mount_path="/.flotilla", read_only=True
        )
        return InstanceSpec(
            image=self.settings.privileged_image if self.privileged else self.settings.image,
            privileged=self.privileged,
            entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
            labels={labels.LAUNCH: self.anchor.launch_id, PROBE_LABEL: self.run_id, labels.UNIT: name},
            timeout_seconds=self.settings.ttl_s,
            resources=Resources(cpu="1", memory=_MEMORY),
            volumes=(share, *volumes),
            external=external,
            group=group,
        )

    async def _create(self, spec: InstanceSpec, *, wait: bool = True) -> InstanceHandle:
        create = getattr(self.platform, "probe_create", self.platform.create)
        try:
            handle = await create(spec)
        except BaseException:
            self._uncertain_create = True
            raise
        self._created.append(handle.iid)
        if wait:
            await self._wait_running(handle)
            ready = getattr(self.platform, "probe_ready", None)
            if ready is not None:
                await ready(handle)
        return handle

    async def _wait_running(self, handle: InstanceHandle) -> None:
        running = wait_running(self.platform, self.clock, handle.iid, stage="create")
        await within(self.clock, self.settings.create_timeout_s, running)

    async def _delete(self, iid: str, *, timeout_s: float = 120.0) -> bool:
        """Delete and confirm `not_found` via `get`. Returns whether it was confirmed in time."""
        await self.platform.delete(iid)
        began = self.clock.monotonic()
        while self.clock.monotonic() - began < timeout_s:
            try:
                await self.platform.get(iid)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND:
                    if iid in self._created:
                        self._created.remove(iid)
                    return True
                raise
            await self.clock.sleep(2.0)
        return False

    async def cleanup(self) -> list[str]:
        """Delete all probed instances; one failure neither stops the rest nor masks the caller's error.

        Returns the iids not confirmed deleted.
        """
        unresolved = []
        try:
            async with asyncio.timeout(self.settings.cleanup_timeout_s):
                # A create response can be lost after allocation. Reconcile by this run's label, not by guesses.
                try:
                    began = self.clock.monotonic()
                    while True:
                        for state in await self.platform.list({PROBE_LABEL: self.run_id}):
                            if state.iid not in self._created:
                                self._created.append(state.iid)
                        if (
                            not self._uncertain_create
                            or self.clock.monotonic() - began >= self.settings.list_visibility_s
                        ):
                            break
                        await self.clock.sleep(1.0)
                except Exception:
                    unresolved.append(f"unknown allocations with {PROBE_LABEL}={self.run_id}")
                for iid in list(self._created):
                    with suppress(Exception):
                        await self._delete(iid, timeout_s=min(30.0, self.settings.cleanup_timeout_s))
                if not self._created and not unresolved:
                    try:
                        await self.anchor.remove(f"launches/{self.anchor.launch_id}/{self.run_id}", stage="run")
                    except Exception:
                        unresolved.append(f"shared directory not confirmed removed for probe {self.run_id}")
        except TimeoutError:
            unresolved.append(f"cleanup timeout for {PROBE_LABEL}={self.run_id}")
        return [*self._created, *unresolved]


__all__ = ["PROBE_LABEL", "ProbeSettings", "probe", "probe_deployment"]
