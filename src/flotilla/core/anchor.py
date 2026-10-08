"""锚点：进程存活的心跳，共享存储上的目录操作（Architecture §5.6、§7.2）。

每个 launch 一个锚点 sandbox，挂载共享根目录。进程活着 ⇔ 锚点存在：本进程按 `renew_interval_s` 续期，
gc 按"有没有锚点"判断一个 launch 能否清理。

**停用按时间判定**（§5.6"锚点丢失"）：设最后一次续期成功的请求在 `t0` 发出（创建请求算第一次），
`t0 + grace_s` 起本 launch 永久停用——`ensure_alive` 拒绝之后的创建请求与目录操作，续期停止，锚点随 TTL 过期。
`deactivated` 事件最晚在 `t0 + grace_s` 置位（续期请求卡住也一样），供上层把进行中的 trial 标为 `lost`、
换新的 `launch_id`。`t0` 取请求的发出时刻而不是响应时刻，判定只会偏早，不会偏晚。
"""

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

# 锚点命令的环境：只够找到镜像自带的工具（§2.3：进程环境恰好是 ProcessSpec.env）。
_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


@dataclass(frozen=True)
class AnchorSettings:
    """锚点的镜像与时序参数（§5.6、§16.3 `[reaper]`）。"""

    image: str  # flotilla 自带的小镜像（repo@sha256:…），带 mkdir、rm、ls、chown、tar、cp
    # 锚点不挂 share，常驻命令取自镜像本身。后端对每次执行的包装（OpenSandbox：`env -i`，§3.6）所需的工具
    # 也要在镜像里与单元相同的路径上提供（`/.flotilla/bin/busybox`）：包装路径是部署级配置，所有实例共用。
    entrypoint: tuple[str, ...] = ("/bin/sleep", "2147483647")
    mount_path: str = "/flotilla-root"  # 共享根目录在锚点内的挂载点
    resources: Resources = field(default_factory=lambda: Resources(cpu="0.5", memory="512Mi"))
    ttl_s: int = 600
    renew_interval_s: float = 30.0
    grace_s: float = 90.0  # G
    retry_interval_s: float = 5.0  # 续期失败后的重试间隔
    start_timeout_s: float = 600.0  # 创建、等待可执行命令、等待出现在列表中，合计
    exec_timeout_s: float = 300.0  # 单个目录操作

    def __post_init__(self) -> None:
        if not 0 < self.retry_interval_s <= self.renew_interval_s < self.grace_s < self.ttl_s:
            raise ValueError("须满足 0 < retry_interval_s <= renew_interval_s < grace_s < ttl_s")
        if not posixpath.isabs(self.mount_path):
            raise ValueError(f"mount_path 须为绝对路径：{self.mount_path!r}")


class Anchor:
    """一个 launch 的锚点。`start` 返回后才可用：此时锚点已可执行命令、已出现在按标签列出的结果中。"""

    def __init__(self, platform: Platform, clock: Clock, launch_id: str, settings: AnchorSettings) -> None:
        self.launch_id = launch_id
        self.deactivated = asyncio.Event()
        self._platform = platform
        self._clock = clock
        self._settings = settings
        self._handle: InstanceHandle | None = None
        self._last_renewed = 0.0  # t0：最后一次续期成功的请求发出时刻（monotonic）
        self._renewer: asyncio.Task[None] | None = None
        self._started = False
        self._ready = False
        self._closed = False

    @property
    def iid(self) -> str | None:
        return self._handle.iid if self._handle else None

    # ───────────────────────────── 生命周期 ─────────────────────────────

    async def start(self) -> None:
        """创建锚点，等它可执行命令且出现在列表中（§5.6：此后本进程才创建 launch 目录）。

        失败或被取消时停止续期并删除已建的锚点；删不掉的随 TTL 过期，它没有 launch 目录，不成为 gc 候选。
        """
        if self._started:
            raise RuntimeError("anchor 只能 start 一次")
        self._started = True
        try:
            await within(self._clock, self._settings.start_timeout_s, self._start())
        except BaseException as exc:
            await self.close()
            if isinstance(exc, Expired):
                raise FlotillaError(
                    f"锚点 {self.launch_id} 在 {self._settings.start_timeout_s}s 内未就绪",
                    stage="prepare",
                    category=ErrorCategory.CAPACITY,
                    retryable=True,
                ) from exc
            raise
        self._ready = True

    async def close(self) -> None:
        """停止续期并删除锚点。之后一切目录操作被拒绝。可重复调用。"""
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
                log.warning("删除锚点 %s 失败，留给平台 TTL：%s", self._handle.iid, exc)

    # ───────────────────────────── 存活 ─────────────────────────────

    def ensure_alive(self, stage: Stage) -> None:
        """每个创建请求与目录操作发出前调用（§5.6）：本 launch 已停用或锚点已关闭时抛 `lost`。"""
        if not self._ready:
            raise RuntimeError("anchor 尚未 start 完成")
        if not self._alive():
            raise self.lost(stage)

    def lost(self, stage: Stage) -> FlotillaError:
        """本 launch 已停用时，在 `stage` 上报告的错误（§5.6、§5.7）。"""
        return FlotillaError(
            f"launch {self.launch_id} 已停用（锚点续期超过宽限期，或已关闭）",
            stage=stage,
            category=ErrorCategory.LOST,
            retryable=True,
        )

    def _alive(self) -> bool:
        if self._closed or self.deactivated.is_set():
            return False
        if self._clock.monotonic() - self._last_renewed >= self._settings.grace_s:
            self._deactivate(f"超过 {self._settings.grace_s}s 未续期成功")
            return False
        return True

    def _deactivate(self, why: str) -> None:
        if self.deactivated.is_set():
            return
        log.error("launch %s 永久停用：%s", self.launch_id, why)
        self.deactivated.set()
        if self._renewer is not None and self._renewer is not asyncio.current_task():
            self._renewer.cancel()

    # ───────────────────────────── 目录操作（§7.2）─────────────────────────────

    async def mkdir(self, key: str, *, stage: Stage) -> None:
        await self._run(("mkdir", "-p", "--", self._path(key)), stage)

    async def remove(self, key: str, *, stage: Stage) -> None:
        if not key:
            raise ValueError("拒绝删除共享根目录本身")
        await self._run(("rm", "-rf", "--", self._path(key)), stage)

    async def chown(self, key: str, uid: int, gid: int, *, stage: Stage) -> None:
        """把目录本身的属主改为 `uid:gid`（不递归；§7.2：没有初始内容的卷按清单的 owner）。"""
        await self._run(("chown", f"{uid}:{gid}", "--", self._path(key)), stage)

    async def unpack(self, tar_key: str, dest_key: str, *, stage: Stage) -> None:
        """把共享存储上的 tar 解包到目录中，保留属主与权限（§7.3：卷的初始内容）。`dest_key` 须已存在。"""
        await self._run(("tar", "-xpf", self._path(tar_key), "--numeric-owner", "-C", self._path(dest_key)), stage)

    async def copy_tree(self, src_key: str, dest_key: str, *, stage: Stage) -> None:
        """把目录 `src_key` 复制为 `dest_key`（保留属主、权限与符号链接；§4.4 有写者的多引用 bind 源组）。

        先建好 `dest_key` 的父目录（`cp` 不建中间目录）。`dest_key` 本身不能已存在：`cp -a src dest` 在 dest
        存在时会复制成 dest/src。
        """
        parent = posixpath.dirname(dest_key)
        if parent:
            await self.mkdir(parent, stage=stage)
        await self._run(("cp", "-a", "--", self._path(src_key), self._path(dest_key)), stage)

    async def listdir(self, key: str, *, stage: Stage) -> list[str]:
        result = await self._run(("ls", "-1A", "--", self._path(key)), stage)
        return result.stdout.decode().splitlines()

    def _path(self, key: str) -> str:
        """卷键 → 锚点内的绝对路径。卷键相对共享根目录、不含 `..`（§7.2），空串即共享根目录。"""
        parts = key.split("/") if key else []
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"非法的卷键：{key!r}")
        return posixpath.join(self._settings.mount_path, *parts)

    async def _run(self, argv: tuple[str, ...], stage: Stage) -> ExecResult:
        self.ensure_alive(stage)
        assert self._handle is not None
        proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env=_ENV, timeout_s=self._settings.exec_timeout_s)
        try:
            # 停用时取消：t0 + G 之后不再有目录操作在途（§5.6），后端在调用内的重试也随之停止。
            result = await within(self._clock, None, self._platform.exec(self._handle, proc), abort=(self.deactivated,))
        except Aborted:
            raise self.lost(stage) from None
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        if result.exit_code != 0:
            stderr = result.stderr.decode(errors="replace").strip()[-500:]
            raise FlotillaError(
                f"锚点命令 {' '.join(argv)} 退出码 {result.exit_code}：{stderr}",
                stage=stage,
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )
        return result

    # ───────────────────────────── 内部 ─────────────────────────────

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
        self._handle = await self._platform.create(spec)
        # 平台在 [sent, returned] 之间某一刻接受请求：TTL 与宽限期取早的一端（停用只会偏早），
        # 列出的时限取晚的一端（C1 的时限从请求到达起算，后端可能排队或重试，晚于 sent 才真正发出）。
        returned = self._clock.monotonic()
        self._last_renewed = sent
        self._renewer = asyncio.create_task(self._renew_loop())
        await wait_running(self._platform, self._clock, self._handle.iid, stage="prepare")
        await self._wait_listed(returned)
        if not self._alive():  # 创建或等待太久，启动期间已过宽限期：本 launch 一开始就不可用
            raise FlotillaError(
                f"锚点 {self.launch_id} 在启动期间已超过宽限期 {self._settings.grace_s}s",
                stage="prepare",
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )

    async def _wait_listed(self, returned: float) -> None:
        """等锚点出现在按标签列出的结果中。C1 第 3 条保证它在创建请求发出后 `list_visibility_s` 内出现。"""
        assert self._handle is not None
        deadline = returned + self._platform.caps.list_visibility_s
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = self._clock.monotonic()
            try:
                listed = await self._platform.list(labels.anchor_labels(self.launch_id))
            except FlotillaError as exc:
                log.debug("列出锚点失败，重试：%s", exc)
            else:
                if any(st.iid == self._handle.iid for st in listed):
                    return
                if issued >= deadline:
                    raise FlotillaError(
                        f"锚点 {self._handle.iid} 超过 list_visibility_s 仍未出现在列表中（违反 C1 第 3 条）",
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
                    # 续期请求最多等到宽限期满：卡住的请求不能推迟停用。
                    await within(self._clock, expire_at - sent, self._platform.renew(self._handle.iid, expires))
                except Expired:
                    self._deactivate("续期请求在宽限期内未返回")
                    return
                except Exception as exc:
                    if isinstance(exc, FlotillaError) and exc.category is ErrorCategory.NOT_FOUND:
                        self._deactivate("锚点已不存在")
                        return
                    # 其余失败（含后端漏映射的异常）都只是这一次没续上：宽限期内重试，期满按时间停用。
                    log.warning("锚点续期失败，%ss 后重试：%r", s.retry_interval_s, exc)
                    next_at = self._clock.monotonic() + s.retry_interval_s
                else:
                    self._last_renewed = sent
                    next_at = sent + s.renew_interval_s
        except Exception:  # 只剩本循环自身的缺陷；不能让续期悄悄停掉
            log.exception("锚点续期循环意外退出")
            self._deactivate("续期循环意外退出")
