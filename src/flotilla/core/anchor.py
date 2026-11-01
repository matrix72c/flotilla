"""Renewable launch ownership and confined shared-storage operations."""

from __future__ import annotations

import asyncio
import logging
import posixpath
from dataclasses import dataclass, field
from datetime import timedelta

from flotilla.core import labels
from flotilla.core.errors import at_stage
from flotilla.core.waiting import Aborted, Backoff, Expired, step_towards, wait_running, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    ProcessSpec,
    Resources,
    SharedVolume,
    Stage,
)

log = logging.getLogger(__name__)

# Environment for anchor commands: just enough to find the image's tools (process env is exactly ProcessSpec.env).
_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


@dataclass(frozen=True)
class FileStat:
    """Metadata of one entry on shared storage (`Anchor.stat`)."""

    mode: int
    uid: int
    gid: int
    size: int


@dataclass(frozen=True)
class AnchorSettings:
    """Anchor image and timing parameters (`[reaper]`)."""

    image: str  # small flotilla-provided image (repo@sha256:...) with mkdir, rm, ls, chown, tar, cp
    # The anchor does not mount share; its resident command comes from the image. Tools the backend's per-exec
    # wrapper needs (OpenSandbox: `env -i`) must also be in the image at the unit path (`/.flotilla/bin/busybox`):
    # the wrapper path is deployment-wide config shared by all instances.
    entrypoint: tuple[str, ...] = ("/bin/sleep", "2147483647")
    mount_path: str = "/flotilla-root"  # mount point of the shared root inside the anchor
    resources: Resources = field(default_factory=lambda: Resources(cpu="0.5", memory="512Mi"))
    ttl_s: int = 600
    renew_interval_s: float = 30.0
    grace_s: float = 90.0  # G
    retry_interval_s: float = 5.0  # retry interval after a failed renewal
    start_timeout_s: float = 600.0  # total for create, wait until exec-ready, wait until listed
    exec_timeout_s: float = 300.0  # per directory operation

    def __post_init__(self) -> None:
        if not 0 < self.retry_interval_s <= self.renew_interval_s < self.grace_s < self.ttl_s:
            raise ValueError("must satisfy 0 < retry_interval_s <= renew_interval_s < grace_s < ttl_s")
        if not posixpath.isabs(self.mount_path):
            raise ValueError(f"mount_path must be absolute: {self.mount_path!r}")


class Anchor:
    """A launch's anchor. Usable only after `start` returns: it can then run commands and appears in label listings."""

    def __init__(self, platform: Platform, clock: Clock, launch_id: str, settings: AnchorSettings) -> None:
        self.launch_id = launch_id
        self.deactivated = asyncio.Event()
        self._platform = platform
        self._clock = clock
        self._settings = settings
        self._handle: InstanceHandle | None = None
        self.creation_uncertain = False
        self._last_renewed = 0.0  # t0: send time of the last successful renewal (monotonic)
        self._renewer: asyncio.Task[None] | None = None
        self._started = False
        self._ready = False
        self._closed = False

    @property
    def iid(self) -> str | None:
        return self._handle.iid if self._handle else None

    async def start(self) -> None:
        """Create the anchor and wait until it can run commands and is listed (only then is the launch dir created).

        On failure or cancellation, stop renewal and delete the anchor; one that cannot be deleted expires by TTL and,
        having no launch directory, never becomes a gc candidate.
        """
        if self._started:
            raise RuntimeError("anchor can only be started once")
        self._started = True
        try:
            await within(self._clock, self._settings.start_timeout_s, self._start())
        except BaseException as exc:
            await self.close()
            if isinstance(exc, Expired):
                raise FlotillaError(
                    f"anchor {self.launch_id} not ready within {self._settings.start_timeout_s}s",
                    stage="prepare",
                    category=ErrorCategory.CAPACITY,
                    retryable=True,
                ) from exc
            raise
        self._ready = True

    async def close(self) -> None:
        """Stop renewal and delete the anchor. All later directory operations are refused. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._renewer is not None and not self._renewer.done():
            self._renewer.cancel()
            await asyncio.wait({self._renewer})
        if self._handle is not None:
            try:
                await self._platform.delete(self._handle.iid)
            except FlotillaError as exc:
                log.warning("Deleting anchor %s failed, leaving it to the platform TTL: %s", self._handle.iid, exc)

    def ensure_alive(self, stage: Stage) -> None:
        """Call before each create request and directory operation: raises `lost` if the launch is deactivated or the
        anchor is closed."""
        if not self._ready:
            raise RuntimeError("anchor has not finished starting")
        if not self._alive():
            raise self.lost(stage)

    def lost(self, stage: Stage) -> FlotillaError:
        """Error reported on `stage` once this launch is deactivated."""
        return FlotillaError(
            f"launch {self.launch_id} is deactivated (anchor renewal exceeded the grace period, or anchor closed)",
            stage=stage,
            category=ErrorCategory.LOST,
            retryable=True,
        )

    def _alive(self) -> bool:
        if self._closed or self.deactivated.is_set():
            return False
        if self._clock.monotonic() - self._last_renewed >= self._settings.grace_s:
            self._deactivate(f"no successful renewal for {self._settings.grace_s}s")
            return False
        return True

    def _deactivate(self, why: str) -> None:
        if self.deactivated.is_set():
            return
        log.error("launch %s permanently deactivated: %s", self.launch_id, why)
        self.deactivated.set()
        if self._renewer is not None and self._renewer is not asyncio.current_task():
            self._renewer.cancel()

    async def mkdir(self, key: str, *, stage: Stage) -> None:
        await self._run(("mkdir", "-p", "--", self._path(key)), stage)

    async def remove(self, key: str, *, stage: Stage) -> None:
        if not key:
            raise ValueError("refusing to remove the shared root itself")
        await self._run(("rm", "-rf", "--", self._path(key)), stage)

    async def chown(self, key: str, uid: int, gid: int, *, stage: Stage) -> None:
        """Change the directory's own owner to `uid:gid` (non-recursive; volumes without initial content use the
        manifest owner)."""
        await self._run(("chown", f"{uid}:{gid}", "--", self._path(key)), stage)

    async def unpack(self, tar_key: str, dest_key: str, *, stage: Stage) -> None:
        """Unpack a tar on shared storage into a directory, keeping owners and modes (volume initial content).
        `dest_key` must exist."""
        await self._run(("tar", "-xpf", self._path(tar_key), "--numeric-owner", "-C", self._path(dest_key)), stage)

    async def copy_tree(self, src_key: str, dest_key: str, *, stage: Stage) -> None:
        """Copy directory `src_key` to `dest_key`, keeping owners, modes, and symlinks (multi-reference bind sources
        with a writer).

        Creates the parent of `dest_key` first (`cp` does not create intermediate dirs). `dest_key` itself must not
        exist: `cp -a src dest` copies into dest/src when dest exists.
        """
        parent = posixpath.dirname(dest_key)
        if parent:
            await self.mkdir(parent, stage=stage)
        await self._run(("cp", "-a", "--", self._path(src_key), self._path(dest_key)), stage)

    async def upload(self, key: str, data: bytes, *, mode: int, uid: int, gid: int, stage: Stage) -> None:
        """Managed upload: write as root, then restore the target owner and mode. The parent must exist.

        The platform uid/gid is the actual writer identity; the local owner cannot write into root-created staging.
        chown may clear setuid/setgid, so chmod comes last.
        """
        self.ensure_alive(stage)
        assert self._handle is not None
        try:
            await self._platform.write_file(self._handle, self._path(key), data, mode=mode, uid=0, gid=0)
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        if (uid, gid) != (0, 0):
            await self.chown(key, uid, gid, stage=stage)
            await self.chmod(key, mode, stage=stage)

    async def download(self, key: str, *, stage: Stage) -> bytes:
        """Read back `key` from shared storage."""
        self.ensure_alive(stage)
        assert self._handle is not None
        try:
            return await self._platform.read_file(self._handle, self._path(key))
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc

    async def rename(self, src_key: str, dest_key: str, *, stage: Stage) -> None:
        """Atomic rename (staging -> final directory). Fails without overwriting if the target exists.

        `mv -n` **silently succeeds** when the target exists (exit 0, nothing moved; verified on BusyBox 1.36), so the
        exit code is not enough: check that the source is gone afterwards. Otherwise a concurrent second publisher
        would think the rename succeeded and write its own digest to the manifest while the final directory holds
        someone else's content.
        """
        if not src_key or not dest_key:
            raise ValueError("neither end of rename may be the shared root itself")
        # `mv -T` treats the target as a file, not "move into directory"; `-n` does not overwrite an existing target.
        await self._run(("mv", "-Tn", "--", self._path(src_key), self._path(dest_key)), stage)
        if await self.exists(src_key, stage=stage):
            raise FlotillaError(
                f"rename had no effect: {src_key} still exists (target {dest_key} already exists)",
                stage=stage,
                category=ErrorCategory.INVALID,
                retryable=False,
            )

    async def exists(self, key: str, *, stage: Stage) -> bool:
        """Whether `key` exists.

        Uses `ls -d` (an applet in the anchor image) rather than `test`: the latter is not in the image, and 127 would
        be misread as "missing". Only "command ran but reported No such file" counts as missing; any other non-zero
        exit raises as a fault instead of being taken as an answer.
        """
        self.ensure_alive(stage)
        assert self._handle is not None
        proc = ProcessSpec(
            argv=("ls", "-d", "--", self._path(key)),
            uid=0,
            gid=0,
            cwd="/",
            env=_ENV,
            timeout_s=self._settings.exec_timeout_s,
        )
        try:
            result = await self._platform.exec(self._handle, proc)
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        if result.exit_code == 0:
            return True
        stderr = result.stderr.decode(errors="replace")
        if "No such file" in stderr or ("not found" in stderr and self._path(key) in stderr):
            return False
        raise FlotillaError(
            f"anchor ls -d {self._path(key)} exited {result.exit_code}: {stderr.strip()[-300:]}",
            stage=stage,
            category=ErrorCategory.TRANSIENT,
            retryable=True,
        )

    async def chmod(self, key: str, mode: int, *, stage: Stage) -> None:
        """Set the permission bits of `key` (restores mode after publishing)."""
        await self._run(("chmod", f"{mode:04o}", "--", self._path(key)), stage)

    async def stat(self, key: str, *, stage: Stage) -> FileStat:
        """Read numeric metadata without losing special permission bits."""
        result = await self._run(("/.flotilla/bin/busybox", "stat", "-c", "%a %u %g %s", "--", self._path(key)), stage)
        try:
            mode, uid, gid, size = result.stdout.decode("ascii").split()
            values = FileStat(int(mode, 8), int(uid), int(gid), int(size))
            if not 0 <= values.mode <= 0o7777 or min(values.uid, values.gid, values.size) < 0:
                raise ValueError("invalid numeric metadata")
            return values
        except (UnicodeError, ValueError) as exc:
            raise FlotillaError(
                "anchor returned invalid stat metadata",
                stage=stage,
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            ) from exc

    async def listdir(self, key: str, *, stage: Stage) -> list[str]:
        result = await self._run(("ls", "-1A", "--", self._path(key)), stage)
        return result.stdout.decode().splitlines()

    def _path(self, key: str) -> str:
        """Volume key -> absolute path inside the anchor. Keys are relative to the shared root without `..`;
        the empty string is the shared root."""
        parts = key.split("/") if key else []
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"invalid volume key: {key!r}")
        return posixpath.join(self._settings.mount_path, *parts)

    async def _run(self, argv: tuple[str, ...], stage: Stage) -> ExecResult:
        self.ensure_alive(stage)
        assert self._handle is not None
        proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env=_ENV, timeout_s=self._settings.exec_timeout_s)
        try:
            # Cancel on deactivation: no directory op is in flight after t0 + G, and in-call backend retries stop too.
            result = await within(self._clock, None, self._platform.exec(self._handle, proc), abort=(self.deactivated,))
        except Aborted:
            raise self.lost(stage) from None
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        if result.exit_code != 0:
            stderr = result.stderr.decode(errors="replace").strip()[-500:]
            raise FlotillaError(
                f"anchor command {' '.join(argv)} exited {result.exit_code}: {stderr}",
                stage=stage,
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )
        return result

    async def _start(self) -> None:
        s = self._settings
        spec = InstanceSpec(
            image=s.image,
            entrypoint=s.entrypoint,
            labels=labels.anchor_labels(self.launch_id),
            timeout_seconds=s.ttl_s,
            resources=s.resources,
            volumes=(SharedVolume(key="", mount_path=s.mount_path, read_only=False),),
            external=ExternalPolicy(mode="none"),
        )
        sent = self._clock.monotonic()
        # Keep this at the request boundary: the outer startup deadline can
        # turn a cancelled, already-sent create into a capacity error.
        self.creation_uncertain = True
        try:
            self._handle = await self._platform.create(spec)
        except FlotillaError as exc:
            if exc.category is not ErrorCategory.TRANSIENT:
                self.creation_uncertain = False
            raise
        else:
            self.creation_uncertain = False
        # The platform accepts the request somewhere in [sent, returned]: TTL and grace use the early end (deactivation
        # can only be early); the listing deadline uses the late end (it counts from arrival; backends may queue or
        # retry, really sending after `sent`).
        returned = self._clock.monotonic()
        self._last_renewed = sent
        self._renewer = asyncio.create_task(self._renew_loop())
        await wait_running(self._platform, self._clock, self._handle.iid, stage="prepare")
        await self._wait_listed(returned)
        if not self._alive():  # create or waits took too long, grace expired during startup: launch is unusable
            raise FlotillaError(
                f"anchor {self.launch_id} exceeded the {self._settings.grace_s}s grace period during startup",
                stage="prepare",
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )

    async def _wait_listed(self, returned: float) -> None:
        """Wait for the anchor to appear in label listings. The platform guarantees it appears within
        `list_visibility_s` after the create request is sent."""
        assert self._handle is not None
        deadline = returned + self._platform.caps.list_visibility_s
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = self._clock.monotonic()
            try:
                listed = await self._platform.list(labels.anchor_labels(self.launch_id))
            except FlotillaError as exc:
                if not exc.retryable:
                    raise at_stage(exc, "prepare") from exc
                log.debug("Listing anchors failed, retrying: %s", exc)
            else:
                if any(st.iid == self._handle.iid for st in listed):
                    return
                if issued >= deadline:
                    raise FlotillaError(
                        f"anchor {self._handle.iid} not listed after list_visibility_s (list visibility violated)",
                        stage="prepare",
                        category=ErrorCategory.TRANSIENT,
                        retryable=True,
                    )
            await self._clock.sleep(step_towards(self._clock.monotonic(), deadline, backoff.next()))

    async def _renew_loop(self) -> None:
        assert self._handle is not None
        s = self._settings
        next_at = self._last_renewed + s.renew_interval_s
        try:
            while True:
                expire_at = self._last_renewed + s.grace_s
                await self._clock.sleep(max(0.0, min(next_at, expire_at) - self._clock.monotonic()))
                if not self._alive():
                    return
                sent = self._clock.monotonic()
                expires = self._clock.now() + timedelta(seconds=s.ttl_s)
                try:
                    # Wait for renewal at most until grace expires: a stuck request must not delay deactivation.
                    await within(self._clock, expire_at - sent, self._platform.renew(self._handle.iid, expires))
                except Expired:
                    self._deactivate("renewal request did not return within the grace period")
                    return
                except Exception as exc:
                    if isinstance(exc, FlotillaError) and exc.category is ErrorCategory.NOT_FOUND:
                        self._deactivate("anchor no longer exists")
                        return
                    # Other failures (incl. unmapped backend exceptions) only miss this renewal: retry within grace.
                    log.warning("Anchor renewal failed, retrying in %ss: %r", s.retry_interval_s, exc)
                    next_at = self._clock.monotonic() + s.retry_interval_s
                else:
                    self._last_renewed = sent
                    next_at = sent + s.renew_interval_s
        except Exception:  # only bugs in this loop itself; renewal must not stop silently
            log.exception("Anchor renewal loop exited unexpectedly")
            self._deactivate("renewal loop exited unexpectedly")
