"""内存假平台 `FakePlatform` 与可手动推进的 `ManualClock`（`docs/Architecture.md` 第 14 节）。

本模块是编排核心全部单元测试的地基：core 只依赖 `Platform` 协议，所以回收器时序、gc 两次观察、
取消安全、创建对账等都能对着内存实现跑——不碰网络、确定性、毫秒级。

忠实建模的平台语义（core 的正确性依赖它们）：

- **`get` 便宜且即时，`list` 有可见时限**（C1 第 2、3 条）：一个实例创建后，`get(iid)` 立即可见，
  但 `list(labels)` 要到 `请求到达平台的时刻 + caps.list_visibility_s` 才列出它（取 C1 允许的最晚时刻）。
  创建超时的对账与 gc 全靠这条。
- **create 可"已创建但响应丢失"**：`inject_create_then_fail` 让一次 create 真正建好实例、随后抛错，
  模拟平台已接受、响应未送达（§2.4、§5.5）。
- **进程 / 文件 / exec 可编程**：文件是每实例的内存字典；后台进程由 `finish_process` 推进退出；
  exec 由可替换的 handler 决定结果（默认退出 0）。
- **删除幂等**：`delete` 不存在视为成功。
- **TTL 上限**：create 的 `timeout_seconds` 与 renew 的新过期时间超过 `caps.max_ttl_seconds` 时报 `invalid`。
- **标签按 Kubernetes 规则校验**：值超过 `caps.max_label_value_len` 或含非法字符时 create 报 `invalid`
  （OpenSandbox 把 metadata 存为 K8s 标签，§5.2）。
- **共享目录**：`SharedTree` 把锚点在共享根目录上的 `mkdir` / `rm` / `ls` 作用到内存中的目录集合上。
- **错误注入**：`inject_fault` 对任意方法预置下一次（或多次）抛出的 `FlotillaError`（§2.4）。
- **慢请求 / 慢响应**：`set_latency` 让某个方法在虚拟时间上耗时，可放在产生效果之前（后端排队、退避重试：
  请求晚到平台）或之后（平台已生效、响应迟迟不回），用于取消、超时与对账测试；`create_status` 设定新实例的
  初始状态（默认 `RUNNING`，设为 `PENDING` 模拟非阻塞创建）。

时间一律取自注入的 `ManualClock`——与 core 共享同一实例，推进时钟时 core 的等待与 fake 的可见时限
一致前进。平台方法默认不挂起（`set_latency` 之外），也不自动过期实例（TTL 是最后一道保障，core 不靠它
保证正确性）。
"""

from __future__ import annotations

import asyncio
import heapq
import re
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from flotilla.platform.base import (
    Capabilities,
    ErrorCategory,
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

# Kubernetes 标签值：空，或以字母数字开头结尾、中间可含 -_.（长度另按能力报告检查）。
_LABEL_VALUE = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?)?")

# exec 的可编程行为：给定实例 iid 与进程规格，返回前台执行结果。
ExecHandler = Callable[[str, ProcessSpec], ExecResult]

# `Platform` 的 12 个方法名。故障注入与调用计数按它取键，方法名拼错由 mypy 拦下，而不是让故障静默不触发。
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
    return ExecResult(exit_code=0, stdout=b"", stderr=b"")


class ManualClock:
    """虚拟时钟：时间只在 `await advance()` 时前进，`sleep` 挂在虚拟时间上。

    满足 `flotilla.platform.base.Clock` 协议。`now()` 自固定纪元加已推进的秒数，`monotonic()` 返回
    已推进的秒数，`sleep(s)` 挂到虚拟时间推进 s 之后。

    **`advance` 是协程，会把被唤醒的协程驱动到它们各自的下一个挂起点再返回**——因此测试可以直接
    `await clock.advance(W)` 然后断言，不必手动 `await asyncio.sleep(0)` 去"排空"。推进分段进行：
    逐个到期时刻推进虚拟时间，被唤醒的协程在**正确的**虚拟时间 resume（周期性定时器因此能在一次
    `advance` 内多次触发，例如每 30s 续期在 `advance(90)` 内触发 3 次）。
    """

    # 每步最多让出事件循环的轮数。正常情况下事件循环的就绪队列一空就停（见 `_pump`），这个上限只防
    # 一直在 sleep(0) 上自旋的协程把推进卡死。
    _PUMP_LIMIT = 10_000

    def __init__(self, *, epoch: datetime | None = None) -> None:
        self._epoch = epoch or datetime(2026, 1, 1, tzinfo=UTC)
        self._elapsed = 0.0
        # 等待堆：(唤醒时刻, 序号, event)。序号保证同刻唤醒的确定顺序。
        self._waiters: list[tuple[float, int, asyncio.Event]] = []
        self._seq = 0

    def now(self) -> datetime:
        return self._epoch + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)  # 与 asyncio.sleep(0) 一致：让出一次，但不推进虚拟时间
            return
        event = asyncio.Event()
        deadline = self._elapsed + seconds
        heapq.heappush(self._waiters, (deadline, self._seq, event))
        self._seq += 1
        try:
            await event.wait()
        finally:
            event.set()  # 被取消时让堆中的条目失效：已置位的条目不再是等待者，推进时跳过

    async def advance(self, seconds: float) -> None:
        """把虚拟时间向前推进 `seconds`，分段唤醒到期的 `sleep` 并驱动被唤醒协程到下一个挂起点。"""
        if seconds < 0:
            raise ValueError("cannot advance clock backwards")
        target = self._elapsed + seconds
        while True:
            # 时间每前进一步之前，先让当前时刻已就绪的协程跑到各自的挂起点：刚 create_task 的任务
            # 在此登记 sleep（从当前时刻起算），被唤醒的协程在它们的到期时刻 resume、可能登记新的 sleep。
            await self._pump()
            deadline = self._next_deadline()
            if deadline is None or deadline > target:
                break
            self._elapsed = max(self._elapsed, deadline)  # jump 之后可能有早已过期的等待者：时间不倒退
            while self._waiters and self._waiters[0][0] <= deadline:
                _, _, event = heapq.heappop(self._waiters)
                event.set()
        self._elapsed = target

    async def run[T](self, aw: Awaitable[T], *, limit: float = 86400.0) -> T:
        """把 `aw` 跑完，虚拟时间每次直接跳到下一个到期的 `sleep`（不真实等待）。

        core 的操作常带退避与限速，测试用它代替手动 `advance`。`aw` 在等待虚拟时间以外的事件且没有任何
        `sleep` 能唤醒它时报 `RuntimeError`（死锁）；虚拟时间超过 `limit` 仍未完成报 `TimeoutError`。
        有常驻的周期任务（例如锚点续期）时总有 `sleep` 在等，死锁只会表现为后者。
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
                    raise RuntimeError("任务在等待虚拟时间以外的事件，且没有 sleep 能唤醒它")
                raise TimeoutError(f"虚拟时间推进 {limit}s 后任务仍未完成")
            await self.advance(deadline - self._elapsed)
        return task.result()

    def jump(self, seconds: float) -> None:
        """只拨表、不唤醒任何 `sleep`，也不让出事件循环：模拟"时间已过、等待者还没来得及醒"的瞬间。"""
        if seconds < 0:
            raise ValueError("cannot advance clock backwards")
        self._elapsed += seconds

    def _next_deadline(self) -> float | None:
        """最早一个仍在等待的 `sleep` 的到期时刻；先丢弃已失效（被取消）的条目。"""
        while self._waiters and self._waiters[0][2].is_set():
            heapq.heappop(self._waiters)
        return self._waiters[0][0] if self._waiters else None

    async def _pump(self) -> None:
        """让出事件循环，直到没有别的协程就绪（全都挂在虚拟时间或其他事件上）。

        就绪与否读事件循环的就绪队列 `_ready`——CPython `BaseEventLoop` 的内部属性，测试设施里用它换取
        "推进到真正静止"的确定性；取不到时退回固定 64 轮。
        """
        ready = getattr(asyncio.get_running_loop(), "_ready", None)
        limit = self._PUMP_LIMIT if ready is not None else 64
        for _ in range(limit):
            await asyncio.sleep(0)
            if ready is not None and not ready:
                return


@dataclass
class _Fault:
    """预置的一次故障。`after_effect=True` 表示先执行操作、再抛错（仅 create 有意义）。"""

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
    visible_at: float  # list 从这一刻起列出它（monotonic）
    expires_at: datetime | None
    address: str
    files: dict[str, bytes] = field(default_factory=dict)
    processes: dict[str, _Process] = field(default_factory=dict)
    topology: Topology | None = None  # 最近一次 link 施加的拓扑
    reason: str | None = None


class FakePlatform:
    """内存 `Platform` 实现。满足 `flotilla.platform.base.Platform` 协议。

    与注入的 `ManualClock` 共享虚拟时间。实例、文件、进程都在内存中；错误经 `inject_fault` 注入。
    每个方法调用都记入 `self.calls`（`Counter`），供 core 测试断言"某调用未发生"（§14：释放不调用
    list、存活确认不调用 list）。测试辅助：`instances()`、`finish_process()`、`set_status()`、
    `spec_of()`、`topology_of()`、`pids()`、`process_spec()`、`file()`、`lose_processes()`、`set_exec_handler()`、`set_latency()`、
    `clear_faults()`、`create_status`、`initial_files`、`on_create`、`calls`。
    """

    def __init__(
        self,
        caps: Capabilities,
        *,
        clock: ManualClock | None = None,
        exec_handler: ExecHandler | None = None,
    ) -> None:
        self.caps = caps
        self.clock = clock or ManualClock()
        self._exec_handler = exec_handler or _default_exec_handler
        self._instances: dict[str, _Instance] = {}
        self._faults: dict[PlatformMethod, deque[_Fault]] = defaultdict(deque)
        self._latency: dict[PlatformMethod, tuple[float, Literal["before", "after"]]] = {}
        self._iid_counter = 0
        self._pid_counter = 0
        #: 每个 Platform 方法被调用的次数，供测试断言调用量（§14）。
        self.calls: Counter[PlatformMethod] = Counter()
        #: 新实例的初始状态。
        self.create_status = InstanceStatus.RUNNING
        #: 新实例的初始文件（平台镜像里已有的内容，例如 `/etc/hosts`）。
        self.initial_files: dict[str, bytes] = {}
        #: 每个新实例创建后调用一次（测试用来按单元注入状态，例如让某个单元创建即失败）。
        self.on_create: Callable[[str, InstanceSpec], None] | None = None

    # ───────────────────────────── 测试辅助 ─────────────────────────────

    def inject_fault(self, method: PlatformMethod, error: FlotillaError, *, after_effect: bool = False) -> None:
        """预置下一次对 `method` 的调用抛出 `error`。多次调用按顺序消费。

        `after_effect` 只对 `create` 有意义（先建实例再抛错）；用在别的方法上抛 `ValueError`。
        """
        if after_effect and method != "create":
            raise ValueError(f"after_effect 只对 create 有意义，不能用于 {method!r}")
        self._faults[method].append(_Fault(error, after_effect))

    def clear_faults(self) -> None:
        """丢弃所有尚未消费的预置故障（模拟平台恢复）。"""
        self._faults.clear()

    def inject_create_then_fail(self, error: FlotillaError) -> None:
        """下一次 create 真正建好实例、随后抛 `error`——模拟平台已接受、响应丢失（§2.4、§5.5）。"""
        self.inject_fault("create", error, after_effect=True)

    def set_latency(
        self,
        method: PlatformMethod,
        seconds: float,
        *,
        phase: Literal["before", "after"] = "before",
    ) -> None:
        """让 `method` 的每次调用在虚拟时间上耗时 `seconds`。

        `phase="before"`：耗时在产生效果之前——请求晚到平台（后端排队、退避重试），期间取消则不产生效果。
        `phase="after"`：先产生效果、再耗时才返回——平台已生效、响应迟迟不回，期间取消则效果已在。
        预置的故障在耗时之后才决定（`after_effect` 的故障在效果之后抛出）。`phase="after"` 只对 `create` 有意义。
        """
        if phase == "after" and method != "create":
            raise ValueError(f"phase='after' 只对 create 有意义，不能用于 {method!r}")
        self._latency[method] = (seconds, phase)

    def set_exec_handler(self, handler: ExecHandler) -> None:
        self._exec_handler = handler

    @property
    def exec_handler(self) -> ExecHandler:
        return self._exec_handler

    def instances(self) -> list[InstanceState]:
        """当前所有存活实例的快照（不受 `list` 可见时限影响），供断言用。"""
        return [self._snapshot(inst) for inst in self._instances.values()]

    def finish_process(self, iid: str, pid: str, exit_code: int) -> None:
        """把一个后台进程推进为已退出。"""
        proc = self._instances[iid].processes[pid]
        proc.running = False
        proc.exit_code = exit_code

    def iids(self) -> list[str]:
        """当前存在（未删除）的实例 ID，按创建先后。"""
        return list(self._instances)

    def spec(self, iid: str) -> InstanceSpec:
        """实例创建时的 `InstanceSpec`。"""
        return self._instances[iid].spec

    def pids(self, iid: str) -> list[str]:
        """实例上启动过的后台进程 ID，按启动先后。"""
        return list(self._instances[iid].processes)

    def process_spec(self, iid: str, pid: str) -> ProcessSpec:
        """后台进程启动时的 `ProcessSpec`。"""
        return self._instances[iid].processes[pid].spec

    def lose_processes(self, iid: str) -> None:
        """丢弃实例上的全部进程记录（模拟执行守护进程重启：`process_status` 从此返回 `found=False`）。"""
        self._instances[iid].processes.clear()

    def file(self, iid: str, path: str) -> bytes | None:
        """实例上的文件内容（不经 `read_file`，不记调用、不受故障注入影响）。"""
        return self._instances[iid].files.get(path)

    def spec_of(self, iid: str) -> InstanceSpec:
        """实例创建时的 `InstanceSpec`。"""
        return self._instances[iid].spec

    def topology_of(self, iid: str) -> Topology | None:
        """该实例最近一次被 `link` 施加的拓扑；从未 link 过为 `None`。"""
        return self._instances[iid].topology

    def set_status(self, iid: str, status: InstanceStatus, *, reason: str | None = None) -> None:
        """改写实例状态（模拟平台侧失败 / 终止，用于 core 的 lost 检测测试）。"""
        inst = self._instances[iid]
        inst.status = status
        inst.reason = reason

    # ───────────────────────────── 内部 ─────────────────────────────

    async def _call(self, method: PlatformMethod) -> _Fault | None:
        """记一次 `method` 调用，按 `phase="before"` 的 `set_latency` 耗时，再取出为它预置的下一个故障（若有）。"""
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

    async def get(self, iid: str) -> InstanceState:
        fault = await self._call("get")
        if fault is not None:
            raise fault.error
        return self._snapshot(self._require(iid))

    async def delete(self, iid: str) -> None:
        fault = await self._call("delete")
        if fault is not None:
            raise fault.error
        self._instances.pop(iid, None)  # 不存在视为成功

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

    async def exec(self, handle: InstanceHandle, proc: ProcessSpec) -> ExecResult:
        fault = await self._call("exec")
        if fault is not None:
            raise fault.error
        self._require(handle.iid)
        return self._exec_handler(handle.iid, proc)

    async def start_process(self, handle: InstanceHandle, proc: ProcessSpec) -> str:
        fault = await self._call("start_process")
        if fault is not None:
            raise fault.error
        inst = self._require(handle.iid)
        pid = f"p{self._pid_counter}"
        self._pid_counter += 1
        inst.processes[pid] = _Process(pid=pid, spec=proc)
        return pid

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

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes:
        fault = await self._call("read_file")
        if fault is not None:
            raise fault.error
        inst = self._require(handle.iid)
        data = inst.files.get(path)
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
    """共享根目录的内存模型：把锚点的 `mkdir` / `rm` / `ls` / `chown` / `tar` / `cp` 作用在一组路径上。

    装上后成为 `platform` 的 exec handler（锚点的目录操作经 exec，§7.2）；`events` 记下每次删除，供检查先后。
    路径相对共享根目录。
    """

    def __init__(self, platform: FakePlatform, *, mount: str = "/flotilla-root") -> None:
        self.dirs: set[str] = set()
        self.files: set[str] = set()  # 普通文件（例如已发布的种子 tar）
        self.owners: dict[str, str] = {}  # 目录 → "uid:gid"
        self.events: list[str] = []
        self._mount = mount.rstrip("/") + "/"
        platform.set_exec_handler(self._exec)

    def _rel(self, path: str) -> str:
        assert path.startswith(self._mount), path
        return path[len(self._mount) :]

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        op = proc.argv[0]
        missing = ExecResult(exit_code=1, stdout=b"", stderr=b"No such file or directory")
        if op == "tar":  # tar -xpf <tar> --numeric-owner -C <dest>
            tar, dest = self._rel(proc.argv[2]), self._rel(proc.argv[-1])
            if dest not in self.dirs or tar not in self.files:
                return missing
            self.events.append(f"unpack {tar} {dest}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        if op == "cp":  # cp -a -- <src> <dest>：与真实 cp 一样，不建中间目录，dest 已存在时复制进去
            src, dest = self._rel(proc.argv[-2]), self._rel(proc.argv[-1])
            parent = dest.rsplit("/", 1)[0] if "/" in dest else ""
            if src not in self.dirs or (parent and parent not in self.dirs):
                return missing
            if dest in self.dirs:
                dest = f"{dest}/{src.rsplit('/', 1)[-1]}"
            self.dirs.add(dest)
            self.dirs.update(dest + d[len(src) :] for d in list(self.dirs) if d.startswith(src + "/"))
            self.events.append(f"cp {src} {dest}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        path = self._rel(proc.argv[-1])
        if op == "mkdir":
            parts = path.split("/")
            self.dirs.update("/".join(parts[: i + 1]) for i in range(len(parts)))
            self.events.append(f"mkdir {path}")
        elif op == "chown":
            if path not in self.dirs:
                return missing
            self.owners[path] = proc.argv[1]
        elif op == "rm":
            self.dirs = {d for d in self.dirs if d != path and not d.startswith(path + "/")}
            self.events.append(f"rm {path}")
        elif op == "ls":
            if path not in self.dirs:
                return missing
            children = sorted({d[len(path) + 1 :].split("/")[0] for d in self.dirs if d.startswith(path + "/")})
            return ExecResult(exit_code=0, stdout="".join(f"{c}\n" for c in children).encode(), stderr=b"")
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")
