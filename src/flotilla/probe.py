"""Bounded deployment probe orchestration and common behavioral checks."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from typing import Any

from flotilla import __version__
from flotilla.capabilities import CapabilityReport, Item, ProbeSettings
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.waiting import Expired, wait_running, within
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
    TmpfsMount,
)

PROBE_LABEL = "flotilla/probe"  # run id; with the launch and unit labels it identifies a probed instance
_EXEC_TIMEOUT_S = 30.0
_MEMORY = "512Mi"
_MEMORY_BYTES = 512 * 1024 * 1024
#: Differs from Docker's 64 MiB and from a memory-sized default, so a passing check reflects the request.
_SHM_PROBE_BYTES = 96 * 1024 * 1024
_NONE = ExternalPolicy(mode="none")
_PATH = "/usr/local/bin:/usr/bin:/bin"


async def probe(platform: Platform, clock: Clock, anchor: Anchor, settings: ProbeSettings) -> CapabilityReport:
    """Measure configured choices; never import historical/manual verdicts."""
    from flotilla.probe_checks import measure

    run = _Run(platform, clock, anchor, settings, run_id=f"p{uuid.uuid4().hex[:10]}")
    results: dict[str, Any] = {}
    notes = []
    cancelled = None
    try:
        async with asyncio.timeout(settings.total_timeout_s):
            await measure(run, results)
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
    return CapabilityReport(
        deployment=settings.deployment,
        endpoint=settings.endpoint,
        probed_at=clock.now(),
        probe_version=__version__,
        evidence_profile=settings.evidence_profile,
        inputs=asdict(settings),
        notes=tuple(notes),
        cleanup=Item(
            status="error" if leaked else "passed",
            evidence="deletion confirmation",
            measurements={"unconfirmed_instances": leaked},
        ),
        **results,
    )


def _cpu_limit(fields: list[str]) -> float | None:
    """cgroup CPU limit in cores: v2 `cpu.max` is "quota period", v1 has quota and period files; `max` / -1 = none."""
    if len(fields) != 2 or fields[0] in ("max", "-1"):
        return None
    try:
        return int(fields[0]) / int(fields[1])
    except (ValueError, ZeroDivisionError):
        return None


def _mounted_subdir(entry: str, subdir: str) -> bool:
    """Whether mountinfo "source|root" mounts `subdir`: the source, or source joined with root, ends with it."""
    source, _, root = entry.partition("|")
    joined = source.rstrip("/") + ("" if root in ("", "/") else root)
    return any(path.rstrip("/").endswith("/" + subdir) for path in (source, joined))


def _probed(ok: bool, evidence: str) -> Item:
    return Item(status="passed" if ok else "failed", evidence=evidence)


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

    async def filesystem(self, readonly: bool, tmpfs: bool) -> tuple[Item, Item]:
        skipped = Item(status="skipped", evidence="configured backend cannot express filesystem options")
        ro_result = await self._filesystem_case(True, False) if readonly else skipped
        tmp_result = await self._filesystem_case(False, True) if tmpfs else skipped
        if readonly and tmpfs and ro_result.ok and tmp_result.ok:
            combined = await self._filesystem_case(True, True)
            if not combined.ok:
                return combined, combined
        return ro_result, tmp_result

    async def _filesystem_case(self, readonly: bool, tmpfs: bool) -> Item:
        """Exercise actual mounts, exec, files and hosts; a JSON echo alone is insufficient evidence."""
        try:
            key = f"launches/{self.anchor.launch_id}/{self.run_id}/fs-{int(readonly)}-{int(tmpfs)}"
            await self.anchor.mkdir(key, stage="prepare")
            spec = replace(
                self._spec(f"fs-{int(readonly)}-{int(tmpfs)}", (SharedVolume(key, "/var/tmp", False),)),
                readonly_rootfs=readonly,
                tmpfs_mounts=(TmpfsMount("/tmp", 1024 * 1024, 0o750, 1000, 1000),) if tmpfs else (),
            )
            handle = await self._create(spec)
            if readonly:
                result = await self.platform.exec(
                    handle,
                    _sh(
                        "# flotilla filesystem rootfs probe\n"
                        "err=$(touch /.flotilla-rootfs-probe 2>&1); rc=$?; "
                        "test $rc -ne 0 && printf '%s' \"$err\" | grep -q 'Read-only file system'",
                        LC_ALL="C",
                    ),
                )
                if result.exit_code != 0 or result.timed_out:
                    return _probed(False, "root write was not rejected with EROFS")
                # The actual multi-service setup writes this runtime-managed file.
                hosts = await self.platform.read_file(handle, "/etc/hosts")
                await self.platform.write_file(
                    handle, "/etc/hosts", hosts + b"\n# flotilla filesystem probe\n", mode=0o644, uid=0, gid=0
                )
                if await self.platform.read_file(handle, "/etc/hosts") != hosts + b"\n# flotilla filesystem probe\n":
                    return _probed(False, "hosts write/read failed with readonly_rootfs")
            await self.platform.write_file(handle, "/var/tmp/flotilla-fs", b"probe", mode=0o600, uid=0, gid=0)
            if await self.platform.read_file(handle, "/var/tmp/flotilla-fs") != b"probe":
                return _probed(False, "writable volume upload/download failed")
            if tmpfs:
                result = await self.platform.exec(
                    handle,
                    _sh(
                        "# flotilla filesystem tmpfs probe\n"
                        'test "$(stat -f -c %T /tmp)" = tmpfs && '
                        'test "$(stat -c \'%u:%g %a\' /tmp)" = "1000:1000 750" && '
                        "test ! -e /tmp/flotilla-tmpfs-probe && "
                        "err=$(dd if=/dev/zero of=/tmp/flotilla-tmpfs-probe bs=65536 count=32 2>&1); rc=$?; "
                        'test $rc -ne 0 && printf "%s" "$err" | grep -q "No space left on device"',
                        uid=1000,
                        gid=1000,
                        LC_ALL="C",
                    ),
                )
                if result.exit_code != 0 or result.timed_out:
                    return _probed(False, "tmpfs type, ownership, mode, non-root write or capacity check failed")
                again = await self.platform.exec(handle, _sh("test -f /tmp/flotilla-tmpfs-probe", uid=1000, gid=1000))
                if again.exit_code != 0:
                    return _probed(False, "tmpfs contents did not survive another exec")
                output = await self.platform.exec(
                    handle,
                    _sh(
                        "# flotilla tmpfs output probe\n"
                        "printf 'flotilla-tmpfs-stdout\\n'; printf 'flotilla-tmpfs-stderr\\n' >&2",
                        uid=1000,
                        gid=1000,
                    ),
                )
                if (
                    output.exit_code != 0
                    or output.timed_out
                    or output.stdout != b"flotilla-tmpfs-stdout\n"
                    or output.stderr != b"flotilla-tmpfs-stderr\n"
                ):
                    return _probed(False, "tmpfs exhaustion broke command execution or stdout/stderr capture")
                # A second instance has the same mount declaration and independent contents.
                other = await self._create(spec)
                empty = await self.platform.exec(other, _sh("test ! -e /tmp/flotilla-tmpfs-probe"))
                if empty.exit_code != 0:
                    return _probed(False, "tmpfs data leaked to a second instance")
            return _probed(True, f"filesystem behavior verified (readonly_rootfs={readonly}, native_tmpfs={tmpfs})")
        except (FlotillaError, ValueError, Expired) as exc:
            # Do not copy remote output or configuration into persistent evidence.
            return Item(
                status="failed"
                if isinstance(exc, FlotillaError) and exc.category is ErrorCategory.INVALID
                else "error",
                evidence=f"filesystem probe failed ({type(exc).__name__})",
            )

    async def shm_size(self, requested: bool) -> Item:
        """Request a size distinct from common defaults; check the mounted capacity and its enforcement."""
        if not requested:
            return Item(status="skipped", evidence="configured backend cannot express shm_size")
        size = _SHM_PROBE_BYTES
        try:
            handle = await self._create(replace(self._spec("shm"), shm_size_bytes=size))
            result = await self.platform.exec(
                handle,
                _sh(
                    "# flotilla shm_size probe\n"
                    'test "$(stat -f -c %T /dev/shm)" = tmpfs && '
                    "set -- $(stat -f -c '%b %S' /dev/shm) && "
                    f"test $(($1 * $2)) -eq {size} && "
                    f"err=$(dd if=/dev/zero of=/dev/shm/flotilla-shm-probe bs=1048576 count={size // 2**20 + 1} 2>&1); "
                    "rc=$?; rm -f /dev/shm/flotilla-shm-probe; "
                    'test $rc -ne 0 && printf "%s" "$err" | grep -q "No space left on device"',
                    uid=1000,
                    gid=1000,
                    LC_ALL="C",
                ),
            )
            if result.exit_code != 0 or result.timed_out:
                return _probed(False, "/dev/shm type, requested capacity, non-root write or limit check failed")
            return _probed(True, f"/dev/shm mounted as tmpfs with the requested {size} bytes, enforced")
        except (FlotillaError, ValueError, Expired) as exc:
            return Item(
                status="failed"
                if isinstance(exc, FlotillaError) and exc.category is ErrorCategory.INVALID
                else "error",
                evidence=f"shm_size probe failed ({type(exc).__name__})",
            )

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

    async def lifecycle(self, visibility_s: float, ttl_s: int) -> dict[str, Item]:
        # Visibility is timed from the create request: list right after create returns, without waiting for Running.
        sent = self.clock.monotonic()
        created_at = self.clock.now()
        handle = await self._create(replace(self._spec("lifecycle"), timeout_seconds=ttl_s), wait=False)
        create_returned_at = self.clock.now()
        seen_after = await self._listed_after(handle.iid, sent, visibility_s)
        await self._wait_running(handle)
        running_observed_at = self.clock.now()
        expires = (await self.platform.get(handle.iid)).expires_at
        label_item = _probed(
            seen_after is not None and seen_after <= visibility_s,
            f"listed by label {seen_after:.1f}s after create request (configured budget {visibility_s}s)"
            if seen_after is not None
            else f"did not appear in label listing within configured budget {visibility_s}s",
        )

        extend = ttl_s
        await self.clock.sleep(2.0)
        target = self.clock.now() + timedelta(seconds=extend)
        await self.platform.renew(handle.iid, target)
        renewed = (await self.platform.get(handle.iid)).expires_at
        renew_ok = (
            renewed is not None
            and expires is not None
            and renewed > expires
            and abs((renewed - target).total_seconds()) < 5
        )
        renew_item = _probed(renew_ok, f"renewed to {target.isoformat()}, get returned {renewed}")

        gone = await self._delete(handle.iid)
        await self.platform.delete(f"flotilla-probe-missing-{uuid.uuid4().hex[:8]}")  # a missing id must not raise
        crud_item = _probed(
            expires is not None and gone,
            f"get returned expiry {expires}; after delete {'not_found confirmed' if gone else 'still visible'}; "
            "deleting a missing id succeeded",
        )
        return {
            "create_get_delete": crud_item,
            "label_list": label_item,
            "renew": renew_item,
            # Creation does not document an exact TTL origin. Bound that origin by the
            # request and first observed Running, then verify the explicit renewal target.
            # This checks reported deadlines, not natural reclamation (CRUD above deletes).
            "ttl": Item(
                status="passed"
                if (
                    expires is not None
                    and expires > running_observed_at
                    and created_at + timedelta(seconds=ttl_s - 10)
                    < expires
                    < running_observed_at + timedelta(seconds=ttl_s + 10)
                    and renew_ok
                )
                else "failed",
                evidence=f"requested {ttl_s}s; create request interval {created_at.isoformat()} to "
                f"{create_returned_at.isoformat()}; first Running observed {running_observed_at.isoformat()}; "
                f"reported expiry {expires}; explicit renewal matched={renew_ok}; natural expiration not measured",
                measurements={
                    "requested_seconds": ttl_s,
                    "create_requested_at": created_at.isoformat(),
                    "create_returned_at": create_returned_at.isoformat(),
                    "running_observed_at": running_observed_at.isoformat(),
                    "expires_at": expires.isoformat() if expires is not None else None,
                    "renew_target": target.isoformat(),
                    "renewed_expires_at": renewed.isoformat() if renewed is not None else None,
                    "natural_expiration_measured": False,
                },
            ),
        }

    async def _listed_after(self, iid: str, sent: float, limit_s: float) -> float | None:
        """List by label every second from `sent`; return the delay until first seen, or None after `limit_s`."""
        query = {PROBE_LABEL: self.run_id, labels.UNIT: "lifecycle"}
        while True:
            if any(s.iid == iid for s in await self.platform.list(query)):
                return self.clock.monotonic() - sent
            if self.clock.monotonic() - sent > limit_s:
                return None
            await self.clock.sleep(1.0)

    async def create_unit(self) -> tuple[InstanceHandle, Item]:
        """Create from a digest image with a placeholder entrypoint; passes if a command can be executed."""
        handle = await self._create(self._spec("unit"))
        result = await self.platform.exec(handle, _sh("true"))
        ok = result.exit_code == 0
        return handle, _probed(
            ok, f"{self.settings.image} created with placeholder entrypoint; true exited {result.exit_code}"
        )

    async def exec_items(self, unit: InstanceHandle) -> dict[str, Item]:
        p = self.platform
        ident = await p.exec(
            unit, _sh('id -u; id -g; pwd; echo "$PROBE_VAR"', uid=1000, gid=1000, cwd="/tmp", PROBE_VAR="x y")
        )
        got = ident.stdout.decode().splitlines()[:4]
        slow = await p.exec(unit, _sh("sleep 30", timeout_s=2.0))
        exec_item = _probed(
            ident.exit_code == 0 and got == ["1000", "1000", "/tmp", "x y"] and slow.timed_out,
            f"exec as 1000:1000 in /tmp read back {got}; sleep 30 with 2s timeout "
            + ("was killed" if slow.timed_out else f"was not killed (exit code {slow.exit_code})"),
        )

        original = await p.read_file(unit, "/etc/hosts")
        marker = f"# flotilla-probe {self.run_id}\n".encode()
        await p.write_file(unit, "/etc/hosts", original + marker, mode=0o644, uid=0, gid=0)
        first = await p.read_file(unit, "/etc/hosts")
        await self.clock.sleep(self.settings.hosts_recheck_s)
        later = await p.read_file(unit, "/etc/hosts")
        await p.write_file(unit, "/tmp/probe-owned", b"owned\n", mode=0o640, uid=1000, gid=1000)
        owned = (await p.exec(unit, _sh("stat -c '%u:%g %a' /tmp/probe-owned; cat /tmp/probe-owned"))).stdout
        owned_ok = owned.decode().split() == ["1000:1000", "640", "owned"]
        hosts_ok = first == original + marker and later == first
        files_item = _probed(
            hosts_ok and owned_ok,
            f"/etc/hosts read-back after write {'matched' if first == original + marker else 'mismatched'}, "
            f"{'not rewritten' if later == first else 'rewritten'} after {self.settings.hosts_recheck_s:.0f}s; "
            f"file written as 1000:1000 read back {owned.decode().split()}",
        )

        background = ProcessSpec(
            argv=("/bin/sh", "-c", "sleep 3; exit 7"), uid=0, gid=0, cwd="/", env={"PATH": _PATH}, timeout_s=None
        )
        pid = await p.start_process(unit, background)
        seen_running = (await p.process_status(unit, pid)).running
        status = await p.process_status(unit, pid)
        for _ in range(30):
            if not status.running:
                break
            await self.clock.sleep(1.0)
            status = await p.process_status(unit, pid)
        bg_item = _probed(
            seen_running and status.found and not status.running and status.exit_code == 7,
            f"at start running={seen_running}; after exit found={status.found} running={status.running} "
            f"exit_code={status.exit_code} (expected 7)",
        )
        return {"exec_identity": exec_item, "files": files_item, "background": bg_item}

    async def address(self, unit: InstanceHandle) -> Item:
        """Address is obtainable and stable across two reads (lifetime stability is tested by the deployer)."""
        first = await self.platform.internal_address(unit)
        second = await self.platform.internal_address(unit)
        return _probed(bool(first) and first == second, first)

    async def resources(self, unit: InstanceHandle) -> tuple[Item, Item, float | None]:
        """Resource limits (cgroup v2 or v1), requesting cpu=1 memory=512Mi.

        Memory limit must equal the request (OOM behavior matches Compose). CPU limit must be at least the request;
        the actual ratio is recorded, since extra CPU affects only cross-deployment comparability.
        Returns (memory item, CPU item, CPU ratio).
        """
        script = (
            "if [ -f /sys/fs/cgroup/memory.max ]; then cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max; "
            "else cat /sys/fs/cgroup/memory/memory.limit_in_bytes /sys/fs/cgroup/cpu/cpu.cfs_quota_us "
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us; fi"
        )
        out = (await self.platform.exec(unit, _sh(script))).stdout.decode().split()
        memory_ok = out[:1] == [str(_MEMORY_BYTES)]
        cpus = _cpu_limit(out[1:3])  # 1 core requested, so cores equal the ratio; unlimited (max / -1) is None
        verdict = "match" if memory_ok else "mismatch"
        memory = _probed(memory_ok, f"requested memory={_MEMORY}; cgroup reads {out[:1]} ({verdict})")
        if cpus is None:
            cpu = Item(
                status="passed" if len(out) >= 3 and out[1] in ("max", "-1") else "error",
                evidence=f"requested cpu=1; cgroup reads {out[1:3]}",
            )
        else:
            cpu = _probed(cpus >= 1.0, f"requested cpu=1; cgroup reads {out[1:3]}, i.e. {cpus:g} cores ({cpus:g}x)")
        return memory, cpu, cpus

    async def storage(self) -> dict[str, Item]:
        """Anchor creates sibling dirs and a marker; an instance mounts one read-only; the anchor deletes them."""
        base = f"launches/{self.anchor.launch_id}/{self.run_id}"
        await self.anchor.mkdir(f"{base}/visible/inner", stage="prepare")
        await self.anchor.mkdir(f"{base}/sibling", stage="prepare")
        try:
            vol = SharedVolume(key=f"{base}/visible", mount_path="/probe-ro", read_only=True)
            unit = await self._create(self._spec("storage", (vol,)))
            # Isolation: the mount shows only the subdirectory's content (`inner`) and mounts that subdirectory. In
            # mountinfo it appears as the mount root (field 4, bind / K8s subPath) or source (2nd field after "-",
            # e.g. NFS server:/export/<subpath>); one must end with the subpath, not the shared root (_mounted_subdir).
            script = (
                "ls -A /probe-ro; touch /probe-ro/w 2>/dev/null && echo WROTE; "
                'awk \'$5 == "/probe-ro" { for (i = 7; $i != "-"; i++); print "ROOT=" $(i + 2) "|" $4 }\' '
                "/proc/self/mountinfo"
            )
            out = (await self.platform.exec(unit, _sh(script))).stdout.decode().split()
            await self._delete(unit.iid)
        finally:
            await self.anchor.remove(base, stage="run")
        remaining = await self.anchor.listdir(f"launches/{self.anchor.launch_id}", stage="run")
        root = next((w.removeprefix("ROOT=") for w in out if w.startswith("ROOT=")), "")
        listing = [w for w in out if not w.startswith("ROOT=") and w != "WROTE"]
        read_only = "inner" in listing and "WROTE" not in out
        isolated = listing == ["inner"] and _mounted_subdir(root, f"{self.run_id}/visible")
        removed = self.run_id not in remaining
        return {
            "read_only": _probed(
                read_only,
                f"mount readable (marker dir {'seen' if 'inner' in out else 'not seen'}), root write "
                + ("failed" if "WROTE" not in out else "succeeded"),
            ),
            "subdir_isolation": _probed(
                isolated,
                f"mount contents {listing}, source {root or 'unknown'}"
                + ("" if isolated else " (not the mounted subdirectory)"),
            ),
            "dir_management": _probed(
                removed,
                "anchor create and delete of a shared-root subdirectory "
                + ("succeeded" if removed else "left it in place"),
            ),
        }


__all__ = ["PROBE_LABEL", "ProbeSettings", "probe"]
