"""Owned in-memory resources and injected clocks; unknown shell operations fail."""

from __future__ import annotations

import asyncio
import heapq
import io
import re
import tarfile
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal

from flotilla.platform.base import (
    Capabilities,
    Clock,
    ErrorCategory,
    ExecAttempt,
    ExecResult,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    InstanceState,
    InstanceStatus,
    ProcessSpec,
    ProcessStatus,
    Topology,
)

# Kubernetes label value: empty, or starts and ends with an alphanumeric, with -_. allowed in between (length is checked
# separately against the capability report).
_LABEL_VALUE = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?)?")

# Programmable exec behavior: given instance iid and process spec, return the foreground execution result.
ExecHandler = Callable[[str, ProcessSpec], ExecResult]

# The 12 method names of `Platform`. Fault injection and call counting are keyed by them, so mypy catches a misspelled
# method name instead of the fault silently never firing.
PlatformMethod = Literal[
    "create",
    "get",
    "delete",
    "list",
    "renew",
    "exec",
    "start_process",
    "process_status",
    "write_file",
    "read_file",
    "internal_address",
    "link",
]


def _default_exec_handler(iid: str, proc: ProcessSpec) -> ExecResult:
    if proc.argv in (("true",), ("/.flotilla/bin/busybox", "true")):
        return ExecResult(0, b"", b"")
    raise AssertionError(f"unconfigured fake execution: {proc.argv!r}")


def _has_short_flag(argv: tuple[str, ...], flag: str) -> bool:
    """Whether argv contains the short option `flag` (`-l`, `-dl` and `-ln` all count).

    Real tools bundle short options into clusters (`ls -ln`, `mv -Tn`), so a plain `"-l" in argv` check is not enough.
    """
    return any(arg.startswith("-") and not arg.startswith("--") and flag in arg[1:] for arg in argv)


class ManualClock:
    """Virtual clock: time only moves on `await advance()`, and `sleep` waits on virtual time.

    Satisfies the `flotilla.platform.base.Clock` protocol. `now()` is a fixed epoch plus the elapsed seconds,
    `monotonic()` returns the elapsed seconds, and `sleep(s)` waits until virtual time has advanced by s.

    **`advance` is a coroutine that drives woken coroutines to their next suspension point before returning**, so tests
    can simply `await clock.advance(W)` and then assert, without manually draining via `await asyncio.sleep(0)`.
    Advancing happens in steps: virtual time moves deadline by deadline, and woken coroutines resume at the **correct**
    virtual time (so a periodic timer can fire several times within one `advance`, e.g. a 30s renewal fires 3 times
    within `advance(90)`).
    """

    # Maximum event loop yields per step. Normally pumping stops as soon as the ready queue is empty (see `_pump`); this
    # cap only guards against a coroutine spinning on sleep(0) and stalling the advance.
    _PUMP_LIMIT = 10_000

    def __init__(self, *, epoch: datetime | None = None) -> None:
        self._epoch = epoch or datetime(2026, 1, 1, tzinfo=UTC)
        self._elapsed = 0.0
        # Waiter heap: (wake time, sequence number, event). The sequence number makes wake order deterministic for equal
        # times.
        self._waiters: list[tuple[float, int, asyncio.Event]] = []
        self._seq = 0

    def now(self) -> datetime:
        return self._epoch + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)  # Same as asyncio.sleep(0): yield once without advancing virtual time
            return
        event = asyncio.Event()
        deadline = self._elapsed + seconds
        heapq.heappush(self._waiters, (deadline, self._seq, event))
        self._seq += 1
        try:
            await event.wait()
        finally:
            event.set()  # Invalidate the heap entry on cancellation: set entries are not waiters and are skipped

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by `seconds`, waking due `sleep`s and driving woken coroutines to their next await."""
        if seconds < 0:
            raise ValueError("cannot advance clock backwards")
        target = self._elapsed + seconds
        while True:
            # Before each time step, let coroutines ready at the current time run to their suspension points: freshly
            # created tasks register their sleeps here (counted from now), and woken coroutines resume at their
            # deadlines and may register new sleeps.
            await self._pump()
            deadline = self._next_deadline()
            if deadline is None or deadline > target:
                break
            self._elapsed = max(self._elapsed, deadline)  # long-expired waiters may follow a jump: never go back
            while self._waiters and self._waiters[0][0] <= deadline:
                _, _, event = heapq.heappop(self._waiters)
                event.set()
        self._elapsed = target

    async def run[T](self, aw: Awaitable[T], *, limit: float = 86400.0) -> T:
        """Run `aw` to completion, jumping virtual time straight to the next due `sleep` each time (no real waiting).

        Core operations often involve backoff and rate limiting; tests use this instead of manual `advance`. Raises
        `RuntimeError` (deadlock) when `aw` waits on something other than virtual time and no `sleep` can wake it;
        raises `TimeoutError` if it is still unfinished after `limit` virtual seconds. With a resident periodic task
        (e.g. anchor renewal) there is always a pending `sleep`, so a deadlock only shows up as the latter.
        """
        task = asyncio.ensure_future(aw)
        stop_at = self._elapsed + limit
        await self._pump()
        while not task.done():
            deadline = self._next_deadline()
            if deadline is None or deadline > stop_at:
                task.cancel()
                await asyncio.wait({task})
                if deadline is None:
                    raise RuntimeError("task is waiting on something other than virtual time, and no sleep can wake it")
                raise TimeoutError(f"task still unfinished after advancing virtual time by {limit}s")
            await self.advance(deadline - self._elapsed)
        return task.result()

    def jump(self, seconds: float) -> None:
        """Move the clock without waking any `sleep` or yielding: "time has passed, waiters not yet woken"."""
        if seconds < 0:
            raise ValueError("cannot advance clock backwards")
        self._elapsed += seconds

    def _next_deadline(self) -> float | None:
        """Deadline of the earliest still-pending `sleep`; invalidated (cancelled) entries are discarded first."""
        while self._waiters and self._waiters[0][2].is_set():
            heapq.heappop(self._waiters)
        return self._waiters[0][0] if self._waiters else None

    async def _pump(self) -> None:
        """Yield the event loop until no other coroutine is ready (all are waiting on virtual time or other events).

        Readiness is read from the event loop's ready queue `_ready`, an internal attribute of CPython's
        `BaseEventLoop`; test infrastructure uses it for deterministic "advance until truly quiescent" behavior, falling
        back to a fixed 64 rounds when unavailable.
        """
        ready = getattr(asyncio.get_running_loop(), "_ready", None)
        limit = self._PUMP_LIMIT if ready is not None else 64
        for _ in range(limit):
            await asyncio.sleep(0)
            if ready is not None and not ready:
                return


@dataclass
class _Fault:
    """One preset fault. `after_effect=True` means run the operation first, then raise (only meaningful for create)."""

    error: FlotillaError
    after_effect: bool = False


@dataclass
class _Process:
    pid: str
    spec: ProcessSpec
    running: bool = True
    exit_code: int | None = None


@dataclass
class _Instance:
    iid: str
    spec: InstanceSpec
    status: InstanceStatus
    visible_at: float  # list shows it from this moment on (monotonic)
    expires_at: datetime | None
    address: str
    files: dict[str, bytes] = field(default_factory=dict)
    processes: dict[str, _Process] = field(default_factory=dict)
    topology: Topology | None = None  # topology applied by the most recent link
    reason: str | None = None


class FakePlatform:
    """In-memory `Platform` implementation. Satisfies the `flotilla.platform.base.Platform` protocol.

    Shares virtual time with the injected `ManualClock`. Instances, files and processes live in memory; errors are
    injected via `inject_fault`. Every method call is recorded in `self.calls` (a `Counter`) so core tests can assert "a
    call did not happen" (release does not call list, liveness confirmation does not call list). Test helpers:
    `instances()`, `finish_process()`, `set_status()`, `spec_of()`, `topology_of()`, `pids()`, `process_spec()`,
    `file()`, `lose_processes()`, `set_exec_handler()`, `set_latency()`, `clear_faults()`, `create_status`,
    `initial_files`, `on_create`, `calls`.
    """

    def __init__(
        self,
        caps: Capabilities,
        *,
        clock: Clock | None = None,
        exec_handler: ExecHandler | None = None,
    ) -> None:
        self.caps = caps
        self.clock = clock or ManualClock()
        self._exec_handler = exec_handler or _default_exec_handler
        self.shared_tree: SharedTree | None = None  # once a SharedTree is installed, anchor file reads/writes go to it
        self.tree_contents: dict[str, bytes] = {}  # file contents on the shared root (relative path -> content)
        self._instances: dict[str, _Instance] = {}
        self._faults: dict[PlatformMethod, deque[_Fault]] = defaultdict(deque)
        self._latency: dict[PlatformMethod, tuple[float, Literal["before", "after"]]] = {}
        self._iid_counter = 0
        self._pid_counter = 0
        #: Number of calls to each Platform method, for asserting call volume in tests.
        self.calls: Counter[PlatformMethod] = Counter()
        #: Initial status of new instances.
        self.create_status = InstanceStatus.RUNNING
        #: Initial files of new instances (content already in the platform image, e.g. `/etc/hosts`).
        self.initial_files: dict[str, bytes] = {}
        #: Called once after each new instance is created (tests use it to inject per-unit state, e.g. make a unit fail
        #: on creation).
        self.on_create: Callable[[str, InstanceSpec], None] | None = None

    # ───────────────────────────── Test helpers ─────────────────────────────

    def inject_fault(self, method: PlatformMethod, error: FlotillaError, *, after_effect: bool = False) -> None:
        """Make the next call to `method` raise `error`. Multiple injections are consumed in order.

        `after_effect` is only meaningful for `create` (create the instance, then raise); using it on other methods
        raises `ValueError`.
        """
        if after_effect and method != "create":
            raise ValueError(f"after_effect is only meaningful for create, not {method!r}")
        self._faults[method].append(_Fault(error, after_effect))

    def clear_faults(self) -> None:
        """Discard all unconsumed preset faults (simulates platform recovery)."""
        self._faults.clear()

    def inject_create_then_fail(self, error: FlotillaError) -> None:
        """Next create really creates the instance, then raises `error`: accepted, response lost."""
        self.inject_fault("create", error, after_effect=True)

    def set_latency(
        self,
        method: PlatformMethod,
        seconds: float,
        *,
        phase: Literal["before", "after"] = "before",
    ) -> None:
        """Make each call to `method` take `seconds` of virtual time.

        `phase="before"`: the delay precedes the effect; the request reaches the platform late (backend queueing,
        backoff retries), and cancelling during it has no effect. `phase="after"`: the effect happens first, then the
        delay before returning; the platform has applied it but the response is slow, and cancelling during it leaves
        the effect in place. Preset faults are decided after the delay (`after_effect` faults are raised after the
        effect). `phase="after"` is only meaningful for `create`.
        """
        if phase == "after" and method != "create":
            raise ValueError(f"phase='after' is only meaningful for create, not {method!r}")
        self._latency[method] = (seconds, phase)

    def set_exec_handler(self, handler: ExecHandler) -> None:
        self._exec_handler = handler

    @property
    def exec_handler(self) -> ExecHandler:
        return self._exec_handler

    def instances(self) -> list[InstanceState]:
        """Snapshot of all live instances (not subject to the `list` visibility delay), for assertions."""
        return [self._snapshot(inst) for inst in self._instances.values()]

    def finish_process(self, iid: str, pid: str, exit_code: int) -> None:
        """Move a background process to exited."""
        proc = self._instances[iid].processes[pid]
        proc.running = False
        proc.exit_code = exit_code

    def iids(self) -> list[str]:
        """IDs of currently existing (not deleted) instances, in creation order."""
        return list(self._instances)

    def pids(self, iid: str) -> list[str]:
        """IDs of background processes started on the instance, in start order."""
        return list(self._instances[iid].processes)

    def process_spec(self, iid: str, pid: str) -> ProcessSpec:
        """The `ProcessSpec` a background process was started with."""
        return self._instances[iid].processes[pid].spec

    def lose_processes(self, iid: str) -> None:
        """Drop all process records of the instance (after an exec daemon restart: `process_status` then finds none)."""
        self._instances[iid].processes.clear()

    def file(self, iid: str, path: str) -> bytes | None:
        """File content on the instance (bypasses `read_file`; no call recorded, no fault injection)."""
        return self._instances[iid].files.get(path)

    def spec_of(self, iid: str) -> InstanceSpec:
        """The `InstanceSpec` the instance was created with."""
        return self._instances[iid].spec

    def topology_of(self, iid: str) -> Topology | None:
        """Topology most recently applied to the instance by `link`; `None` if never linked."""
        return self._instances[iid].topology

    def set_status(self, iid: str, status: InstanceStatus, *, reason: str | None = None) -> None:
        """Overwrite instance status (simulates platform-side failure / termination, for core lost-detection tests)."""
        inst = self._instances[iid]
        inst.status = status
        inst.reason = reason

    # ───────────────────────────── Internals ─────────────────────────────

    async def _call(self, method: PlatformMethod) -> _Fault | None:
        """Record one `method` call, apply its `phase="before"` latency, then pop its next preset fault (if any)."""
        self.calls[method] += 1
        await self._delay(method, "before")
        queue = self._faults.get(method)
        if queue:
            return queue.popleft()
        return None

    async def _delay(self, method: PlatformMethod, phase: Literal["before", "after"]) -> None:
        latency = self._latency.get(method)
        if latency is not None and latency[1] == phase:
            await self.clock.sleep(latency[0])

    def _require(self, iid: str) -> _Instance:
        inst = self._instances.get(iid)
        if inst is None:
            raise FlotillaError(
                f"instance {iid} not found",
                stage="run",
                category=ErrorCategory.NOT_FOUND,
                retryable=False,
            )
        return inst

    def _visible(self, inst: _Instance) -> bool:
        return self.clock.monotonic() >= inst.visible_at

    def _snapshot(self, inst: _Instance) -> InstanceState:
        return InstanceState(
            iid=inst.iid,
            status=inst.status,
            expires_at=inst.expires_at,
            labels=dict(inst.spec.labels),
            reason=inst.reason,
        )

    def _do_create(self, spec: InstanceSpec, *, arrived_at: float) -> InstanceHandle:
        iid = f"i{self._iid_counter}"
        address = f"10.0.{self._iid_counter // 256}.{self._iid_counter % 256}"
        self._iid_counter += 1
        expires_at = self.clock.now() + timedelta(seconds=spec.timeout_seconds)
        self._instances[iid] = _Instance(
            iid=iid,
            spec=spec,
            status=self.create_status,
            visible_at=arrived_at + self.caps.list_visibility_s,
            expires_at=expires_at,
            address=address,
            files=dict(self.initial_files),
        )
        if self.on_create is not None:
            self.on_create(iid, spec)
        return InstanceHandle(iid=iid)

    # ───────────────────────────── Platform ─────────────────────────────

    async def create(self, spec: InstanceSpec) -> InstanceHandle:
        max_ttl = self.caps.max_ttl_seconds
        if spec.timeout_seconds <= 0 or (max_ttl is not None and spec.timeout_seconds > max_ttl):
            raise FlotillaError(
                f"timeout {spec.timeout_seconds} out of range (max {max_ttl})",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        for key, value in spec.labels.items():
            if len(value) > self.caps.max_label_value_len or not _LABEL_VALUE.fullmatch(value):
                raise FlotillaError(
                    f"label {key}={value!r} is invalid",
                    stage="run",
                    category=ErrorCategory.INVALID,
                    retryable=False,
                )
        fault = await self._call("create")
        if fault is not None and not fault.after_effect:
            raise fault.error
        handle = self._do_create(spec, arrived_at=self.clock.monotonic())
        await self._delay("create", "after")
        if fault is not None and fault.after_effect:
            raise fault.error
        return handle

    def forget_creates(self, labels: Mapping[str, str]) -> None:
        pass  # FakePlatform has no unresolved authentication metadata.

    async def get(self, iid: str) -> InstanceState:
        fault = await self._call("get")
        if fault is not None:
            raise fault.error
        return self._snapshot(self._require(iid))

    async def aclose(self) -> None:
        pass

    async def delete(self, iid: str) -> None:
        fault = await self._call("delete")
        if fault is not None:
            raise fault.error
        self._instances.pop(iid, None)  # missing counts as success

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        fault = await self._call("list")
        if fault is not None:
            raise fault.error
        out: list[InstanceState] = []
        for inst in self._instances.values():
            if not self._visible(inst):
                continue
            if all(inst.spec.labels.get(k) == v for k, v in labels.items()):
                out.append(self._snapshot(inst))
        return out

    async def renew(self, iid: str, expires_at: datetime) -> None:
        fault = await self._call("renew")
        if fault is not None:
            raise fault.error
        inst = self._require(iid)
        max_ttl = self.caps.max_ttl_seconds
        if max_ttl is not None and expires_at > self.clock.now() + timedelta(seconds=max_ttl):
            raise FlotillaError(
                f"expiresAt beyond max ttl {max_ttl}s",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        inst.expires_at = expires_at

    async def exec(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> ExecResult:
        fault = await self._call("exec")
        if fault is not None:
            raise fault.error
        self._require(handle.iid)
        return self._exec_handler(handle.iid, proc)

    async def start_process(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> str:
        if attempt is not None:
            attempt.sent = True
        fault = await self._call("start_process")
        if fault is not None:
            raise fault.error
        inst = self._require(handle.iid)
        pid = f"p{self._pid_counter}"
        self._pid_counter += 1
        inst.processes[pid] = _Process(pid=pid, spec=proc)
        if attempt is not None:
            attempt.command_id = pid
        return pid

    async def interrupt(self, handle: InstanceHandle, pid: str) -> None:
        state = await self.process_status(handle, pid)
        if state.found and state.running:
            self.finish_process(handle.iid, pid, -1)

    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus:
        fault = await self._call("process_status")
        if fault is not None:
            raise fault.error
        inst = self._require(handle.iid)
        proc = inst.processes.get(pid)
        if proc is None:
            return ProcessStatus(found=False, running=False, exit_code=None)
        return ProcessStatus(found=True, running=proc.running, exit_code=proc.exit_code)

    async def write_file(
        self,
        handle: InstanceHandle,
        path: str,
        data: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        fault = await self._call("write_file")
        if fault is not None:
            raise fault.error
        self._require(handle.iid).files[path] = data
        tree = self.shared_tree
        if tree is not None and path.startswith(tree.mount):
            key = path[len(tree.mount) :]
            self.tree_contents[key] = data
            tree.files.add(key)
            tree.modes[key] = f"{mode:04o}"
            tree.owners[key] = f"{uid}:{gid}"

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes:
        fault = await self._call("read_file")
        if fault is not None:
            raise fault.error
        inst = self._require(handle.iid)
        data = inst.files.get(path)
        tree = self.shared_tree
        if data is None and tree is not None and path.startswith(tree.mount):
            data = self.tree_contents.get(path[len(tree.mount) :])
        if data is None:
            raise FlotillaError(
                f"no such file: {path}",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        return data

    async def internal_address(self, handle: InstanceHandle) -> str:
        fault = await self._call("internal_address")
        if fault is not None:
            raise fault.error
        return self._require(handle.iid).address

    async def link(self, members: Mapping[str, InstanceHandle], topology: Topology) -> None:
        fault = await self._call("link")
        if fault is not None:
            raise fault.error
        for handle in members.values():
            self._require(handle.iid).topology = topology


class SharedTree:
    """In-memory model of the shared root: applies the anchor's directory and file operations to a set of paths.

    Covers `mkdir` / `rm` / `ls` (including `ls -d` for existence) / `chown` / `chmod` / `tar` / `cp` / `mv` (matching
    the busybox applets actually shipped in the anchor image); file contents go through `write_file` / `read_file`
    into `tree_contents`. Once installed it becomes the `platform`'s exec handler; `events` records each operation for
    ordering checks. Paths are relative to the shared root.
    """

    def __init__(self, platform: FakePlatform, *, mount: str = "/flotilla-root") -> None:
        self.dirs: set[str] = set()
        self.files: set[str] = set()  # regular files (e.g. published seed tars)
        self.owners: dict[str, str] = {}  # path -> "uid:gid"
        self.modes: dict[str, str] = {}  # path -> mode (octal string)
        self.links: dict[str, str] = {}  # symlink -> target (verbatim)
        self.events: list[str] = []
        self.mount = mount.rstrip("/") + "/"
        self._platform = platform
        platform.set_exec_handler(self._exec)
        platform.shared_tree = self  # route write_file / read_file to this tree

    def _rel(self, path: str) -> str:
        if path == self.mount.rstrip("/"):
            return ""
        assert path.startswith(self.mount), path
        return path[len(self.mount) :]

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        op = proc.argv[0]
        missing = ExecResult(exit_code=1, stdout=b"", stderr=b"No such file or directory")
        if op == "tar":  # tar -xpf <tar> --numeric-owner -C <dest>
            tar, dest = self._rel(proc.argv[2]), self._rel(proc.argv[-1])
            if dest not in self.dirs or tar not in self.files:
                return missing
            data = self._platform.tree_contents[tar]
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    assert not PurePosixPath(member.name).is_absolute() and ".." not in PurePosixPath(member.name).parts
                    target = f"{dest}/{member.name}"
                    if member.isdir():
                        self.dirs.add(target)
                    elif member.isfile():
                        source = archive.extractfile(member)
                        assert source is not None
                        self.files.add(target)
                        self._platform.tree_contents[target] = source.read()
                    else:
                        raise AssertionError("unsupported archive member")
                    self.owners[target] = f"{member.uid}:{member.gid}"
                    self.modes[target] = f"{member.mode:04o}"
            self.events.append(f"unpack {tar} {dest}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        if op == "cp":  # cp -a -- <src> <dest>: like real cp, no intermediate dirs; copies into an existing dest
            src, dest = self._rel(proc.argv[-2]), self._rel(proc.argv[-1])
            parent = dest.rsplit("/", 1)[0] if "/" in dest else ""
            if (src not in self.dirs and src not in self.files) or (parent and parent not in self.dirs):
                return missing
            if dest in self.dirs:
                dest = f"{dest}/{src.rsplit('/', 1)[-1]}"
            if src in self.dirs:
                self.dirs.add(dest)
            else:
                self.files.add(dest)
            self.dirs.update(dest + d[len(src) :] for d in list(self.dirs) if d.startswith(src + "/"))
            self.files.update(dest + f[len(src) :] for f in list(self.files) if f.startswith(src + "/"))
            for values in (self.owners, self.modes, self.links):
                values.update(
                    {dest + k[len(src) :]: v for k, v in list(values.items()) if k == src or k.startswith(src + "/")}
                )
            contents = self._platform.tree_contents
            contents.update(
                {dest + k[len(src) :]: v for k, v in list(contents.items()) if k == src or k.startswith(src + "/")}
            )
            self.events.append(f"cp {src} {dest}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        if op == "mv":  # mv -Tn -- <src> <dest>
            src, dest = self._rel(proc.argv[-2]), self._rel(proc.argv[-1])
            if src not in self.dirs and src not in self.files:
                return missing
            if dest in self.dirs or dest in self.files:
                # Real BusyBox 1.36 `-n`: when the target exists it **exits 0 and moves nothing** (verified), rather
                # than failing. `Anchor.rename` detects this via "source still present after the move", so it must be
                # simulated faithfully here, otherwise that check is never reached.
                if _has_short_flag(proc.argv, "n"):
                    return ExecResult(exit_code=0, stdout=b"", stderr=b"")
                return ExecResult(exit_code=1, stdout=b"", stderr=b"File exists")
            self._move(src, dest)
            self.events.append(f"mv {src} {dest}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        if op == "/.flotilla/bin/busybox" and proc.argv[1] == "stat":
            path = self._rel(proc.argv[-1])
            if path not in self.files and path not in self.dirs:
                return missing
            mode = self.modes.get(path, "0755" if path in self.dirs else "0644")
            uid, gid = self.owners.get(path, "0:0").split(":")
            size = len(self._platform.tree_contents.get(path, b""))
            return ExecResult(0, f"{mode} {uid} {gid} {size}\n".encode(), b"")
        if op not in {"chmod", "mkdir", "chown", "rm", "ls"}:
            raise AssertionError(f"unsupported SharedTree command: {proc.argv!r}")
        path = self._rel(proc.argv[-1])
        if op == "chmod":
            if path not in self.dirs and path not in self.files:
                return missing
            self.modes[path] = proc.argv[1]
        elif op == "mkdir":
            parts = path.split("/")
            self.dirs.update("/".join(parts[: i + 1]) for i in range(len(parts)))
            self.events.append(f"mkdir {path}")
        elif op == "chown":
            if path not in self.dirs and path not in self.files:
                return missing
            self.owners[path] = proc.argv[1]
        elif op == "rm":
            self.dirs = {d for d in self.dirs if d != path and not d.startswith(path + "/")}
            self.files = {f for f in self.files if f != path and not f.startswith(path + "/")}
            for records in (self.owners, self.modes, self.links, self._platform.tree_contents):
                for key in list(records):
                    if key == path or key.startswith(path + "/"):
                        del records[key]
            self.events.append(f"rm {path}")
        elif op == "ls":
            if _has_short_flag(proc.argv, "d"):
                found = path in self.dirs or path in self.files
                if not found:
                    return ExecResult(exit_code=1, stdout=b"", stderr=missing.stderr)
                if _has_short_flag(proc.argv, "l"):
                    return ExecResult(exit_code=0, stdout=f"{self._ls_line(path)}\n".encode(), stderr=b"")
                # ls -d -- <path>: existence check only (real busybox behavior)
                return ExecResult(exit_code=0, stdout=b"", stderr=b"")
            if path not in self.dirs:
                return missing
            entries = {*self.dirs, *self.files}
            children = sorted({e[len(path) + 1 :].split("/")[0] for e in entries if e.startswith(path + "/")})
            return ExecResult(exit_code=0, stdout="".join(f"{c}\n" for c in children).encode(), stderr=b"")
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    def _ls_line(self, path: str) -> str:
        """One line of `ls -ln -d`: mode, links, uid, gid, size.

        Same column order as real busybox, so `Anchor.stat` parsing takes the same path on the fake and on real hosts.
        """
        mode = self.modes.get(path, "0755" if path in self.dirs else "0644")
        numeric = int(mode, 8)
        kind = "d" if path in self.dirs else "-"
        chars = []
        for shift in (6, 3, 0):  # owner / group / other
            bits = (numeric >> shift) & 0o7
            chars.append("r" if bits & 4 else "-")
            chars.append("w" if bits & 2 else "-")
            chars.append("x" if bits & 1 else "-")
        uid, _, gid = self.owners.get(path, "0:0").partition(":")
        size = len(self._platform.tree_contents.get(path, b""))
        return f"{kind}{''.join(chars)} 1 {uid or 0} {gid or 0} {size}"

    def _move(self, src: str, dest: str) -> None:
        """Rename `src` (and its subtree) to `dest`, together with owners, modes, links and file contents."""

        def moved(path: str) -> str:
            return dest + path[len(src) :] if path == src or path.startswith(src + "/") else path

        self.dirs = {moved(d) for d in self.dirs}
        self.files = {moved(f) for f in self.files}
        self.owners = {moved(k): v for k, v in self.owners.items()}
        self.modes = {moved(k): v for k, v in self.modes.items()}
        self.links = {moved(k): v for k, v in self.links.items()}
        contents = self._platform.tree_contents
        self._platform.tree_contents = {moved(k): v for k, v in contents.items()}

    def content(self, key: str) -> bytes:
        """Content of an uploaded file (for test assertions)."""
        return self._platform.tree_contents[key]
