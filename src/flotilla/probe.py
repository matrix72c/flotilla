"""`flotilla probe`：对一个部署运行契约测试，写出能力报告（Architecture 第 14 节；Platform_Requirements 第 3、5 节）。

只经 `Platform` 协议与锚点（`core.anchor`）访问部署，与后端无关。首版只**测**单服务 trial 依赖的项：

| 项 | 测法 |
|---|---|
| C1 生命周期 | 创建、按 ID 查询过期时间、按标签列出（测可见延迟）、续期后查询、删除并确认、删除不存在的 ID |
| C2 执行与文件 | 非 root 身份、工作目录、环境读回；前台超时被终止；`/etc/hosts` 写后读回且不被改写；非 root 写文件 |
| C2 第 4 条、C15 | 以 `repo@sha256:…` 与占位入口创建，之后经执行通道执行 |
| C3 后台进程 | 后台进程先为运行中，退出后状态与退出码正确 |
| C6 `none` | `none` 策略的实例连不到公网 IP（TCP） |
| C7 实例地址 | 可取得，两次读取一致 |
| C8 第 1、3、6 条 | 锚点建两个兄弟目录；实例只读挂载其一：root 写入失败、看不到兄弟目录；锚点删除目录 |
| C10 资源 | 实例内 cgroup 的内存上限等于请求值；CPU 上限不低于请求值，实际倍数照实记录 |

其余项（C4 互联、C5 入站隔离、C6 `any` / `allowlist`、C8 读写共享 / 单文件 / 缺失目录 / 不自动挂载、C9、C12、C13、
C14、C16）需要多个实例的网络配合或平台的书面说明，由部署者在**声明文件**（`Declared`）中给出，来源记为 `declared`，
probe 原样写入报告、不判断真伪；契约测试覆盖它们之后改为测得。

一项测得"不满足"不影响其他项；测试本身出错（平台 5xx、超时）时整个 probe 抛出，不写报告——"测试没跑成"不能
当作"能力不满足"写进报告。创建的实例都带本次运行的标签，返回或出错前全部删除并确认。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flotilla import __version__
from flotilla.capabilities import (
    CapabilityReport,
    Device,
    Exec,
    ExternalForm,
    Item,
    Lifecycle,
    Network,
    Runtime,
    Storage,
)
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.waiting import wait_running, within
from flotilla.platform.base import (
    Capabilities,
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

PROBE_LABEL = "flotilla/probe"  # 本次运行的 id；与 launch、unit 标签一起唯一确定被测实例
_EXEC_TIMEOUT_S = 30.0
_MEMORY = "512Mi"
_MEMORY_BYTES = 512 * 1024 * 1024
_PUBLIC_IP = "1.1.1.1"  # C6 none：一个不属于任何部署的公网地址
_PATH = "/usr/local/bin:/usr/bin:/bin"


class Declared(BaseModel):
    """部署者的声明文件：probe 不测的项（Platform_Requirements §5、后端文档第 1 节）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: str
    max_ttl_seconds: Annotated[int, Field(gt=0)] | None = None
    list_visibility_s: Annotated[float, Field(gt=0)]  # C1 第 3 条：部署声明的时限；测得的可见延迟须不超过它
    link: Item
    link_udp: Item
    link_max_members: Annotated[int, Field(ge=1)] = 1
    inbound_isolation: Item
    external_forms: frozenset[ExternalForm]
    implicit_egress: tuple[str, ...]
    implicit_egress_declared: Item
    shared_volume: Item
    missing_subpath: Item
    no_auto_mount: Item
    volume_file: Item
    exec_auth: Item
    diagnostics: Item
    privileged_runtime: Item
    devices: frozenset[Device] = frozenset()
    multi_container: Item
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Declared:
        """报告的一致性约束在跑 probe 之前就检查，免得测完（可能十多分钟）才因声明有误而丢掉结果。"""
        if "none" not in self.external_forms:
            raise ValueError("external_forms 须含 none：probe 总会测 none（C6 第 1 条为必需项）")
        if any(d.source != "declared" for d in self._declared_items()):
            raise ValueError("声明文件中的项 source 须为 declared")
        return self

    def _declared_items(self) -> tuple[Item, ...]:
        return (
            self.link,
            self.link_udp,
            self.inbound_isolation,
            self.implicit_egress_declared,
            self.shared_volume,
            self.missing_subpath,
            self.no_auto_mount,
            self.volume_file,
            self.exec_auth,
            self.diagnostics,
            self.privileged_runtime,
            self.multi_container,
        )

    def bootstrap_capabilities(self) -> Capabilities:
        """probe 运行期间组装后端所用的 `Capabilities`：报告还没有，取声明值，被测项一律按最保守的值。

        probe 本身只用到 `list_visibility_s`（锚点等自己出现在列表中）与标签长度；其余字段不影响 probe 的调用。
        """
        return Capabilities(
            max_label_value_len=63,
            list_visibility_s=self.list_visibility_s,
            max_ttl_seconds=self.max_ttl_seconds,
            link=False,
            link_udp=False,
            link_max_members=1,
            inbound_isolation=False,
            external_forms=frozenset({"none"}),
            implicit_egress=self.implicit_egress,
            internal_address="exec",
            shared_volume=False,
            volume_file=False,
            no_auto_mount=False,
            exec_auth=False,
            privileged_runtime=False,
            devices=frozenset(),
        )


@dataclass(frozen=True)
class ProbeSettings:
    #: 被测单元的镜像（repo@sha256:…），须有 /bin/sh、id、pwd、stat、cat、ls、touch、awk、timeout 与 nc（`-z`）。
    image: str
    share_release: str  # 每个被测单元只读挂载 share，与真实单元一致（执行包装依赖其中的 busybox）
    endpoint: str  # 写进报告；provider `start` 时与配置比对
    create_timeout_s: float = 300.0
    #: C2 第 3 条：写入 /etc/hosts 后隔这么久再读回。验收标准为 10 分钟，日常探测可调小。
    hosts_recheck_s: float = 30.0
    ttl_s: int = 900


async def probe(
    platform: Platform,
    clock: Clock,
    anchor: Anchor,
    declared: Declared,
    settings: ProbeSettings,
) -> CapabilityReport:
    """运行测得项、合并声明项，返回能力报告。`anchor` 须已 `start`；创建的实例在返回或出错前全部删除。"""
    run = _Run(platform, clock, anchor, settings, run_id=f"p{uuid.uuid4().hex[:10]}")
    try:
        lifecycle = await run.lifecycle(declared.list_visibility_s, declared.max_ttl_seconds)
        unit, placeholder = await run.create_unit()
        digest = _probed(
            placeholder.ok and "@sha256:" in settings.image,
            f"以 {settings.image} 创建"
            + ("成功" if placeholder.ok else "失败")
            + ("" if "@sha256:" in settings.image else "；镜像不是 repo@sha256:… 形式，C15 未验证"),
        )
        exec_items = await run.exec_items(unit)
        address = await run.address(unit)
        memory_limit, cpu_limit, cpu_ratio = await run.resources(unit)
        external_none = await run.external_none(unit)
        storage = await run.storage()
    finally:
        leaked = await run.cleanup()
    if leaked:  # 带 flotilla/probe 标签、TTL 有限，由平台 TTL 回收；报告照常写出，但标明
        notes_extra: tuple[str, ...] = (f"未能确认删除的被测实例（等平台 TTL 回收）：{leaked}",)
    else:
        notes_extra = ()

    return CapabilityReport(
        deployment=declared.deployment,
        endpoint=settings.endpoint,
        probed_at=clock.now(),
        probe_version=__version__,
        lifecycle=Lifecycle(
            create_get_delete=lifecycle["create_get_delete"],
            label_list=lifecycle["label_list"],
            renew=lifecycle["renew"],
            max_label_value_len=63,
            list_visibility_s=declared.list_visibility_s,
            max_ttl_seconds=declared.max_ttl_seconds,
        ),
        exec=Exec(
            exec_identity=exec_items["exec_identity"],
            files=exec_items["files"],
            placeholder_entry=placeholder,
            background=exec_items["background"],
        ),
        network=Network(
            link=declared.link,
            link_udp=declared.link_udp,
            link_max_members=declared.link_max_members,
            inbound_isolation=declared.inbound_isolation,
            external_forms=declared.external_forms,
            external_none=external_none,
            internal_address="exec" if address.ok else "none",  # 取不到地址时报告校验会拒绝 link = True
            implicit_egress=declared.implicit_egress,
            implicit_egress_declared=declared.implicit_egress_declared,
        ),
        storage=Storage(
            read_only=storage["read_only"],
            shared_volume=declared.shared_volume,
            subdir_isolation=storage["subdir_isolation"],
            missing_subpath=declared.missing_subpath,
            no_auto_mount=declared.no_auto_mount,
            dir_management=storage["dir_management"],
            volume_file=declared.volume_file,
        ),
        runtime=Runtime(
            exec_auth=declared.exec_auth,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            cpu_limit_ratio=cpu_ratio,
            diagnostics=declared.diagnostics,
            privileged_runtime=declared.privileged_runtime,
            devices=declared.devices,
            image_digest=digest,
            multi_container=declared.multi_container,
        ),
        notes=(*declared.notes, f"probe run {run.run_id}; internal address: {address.evidence}", *notes_extra),
    )


def _cpu_limit(fields: list[str]) -> float | None:
    """cgroup 的 CPU 上限（核数）：v2 `cpu.max` 为 "quota period"，v1 为 quota、period 两个文件；`max` / -1 为不限。"""
    if len(fields) != 2 or fields[0] in ("max", "-1"):
        return None
    try:
        return int(fields[0]) / int(fields[1])
    except (ValueError, ZeroDivisionError):
        return None


def _mounted_subdir(entry: str, subdir: str) -> bool:
    """mountinfo 的 "源|挂载根" 是否表示挂载了 `subdir`：源，或源拼上挂载根，以它结尾。"""
    source, _, root = entry.partition("|")
    joined = source.rstrip("/") + ("" if root in ("", "/") else root)
    return any(path.rstrip("/").endswith("/" + subdir) for path in (source, joined))


def _probed(ok: bool, evidence: str) -> Item:
    return Item(ok=ok, source="probed", evidence=evidence)


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

    def __post_init__(self) -> None:
        self._created: list[str] = []

    # ───────────────────────────── 实例 ─────────────────────────────

    def _spec(self, name: str, volumes: tuple[SharedVolume, ...] = ()) -> InstanceSpec:
        share = SharedVolume(
            key=f"share/releases/{self.settings.share_release}", mount_path="/.flotilla", read_only=True
        )
        return InstanceSpec(
            image=self.settings.image,
            entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
            labels={labels.LAUNCH: self.anchor.launch_id, PROBE_LABEL: self.run_id, labels.UNIT: name},
            timeout_seconds=self.settings.ttl_s,
            resources=Resources(cpu="1", memory=_MEMORY),
            volumes=(share, *volumes),
            external=ExternalPolicy(mode="none"),
        )

    async def _create(self, spec: InstanceSpec, *, wait: bool = True) -> InstanceHandle:
        handle = await self.platform.create(spec)
        self._created.append(handle.iid)
        if wait:
            await self._wait_running(handle)
        return handle

    async def _wait_running(self, handle: InstanceHandle) -> None:
        running = wait_running(self.platform, self.clock, handle.iid, stage="create")
        await within(self.clock, self.settings.create_timeout_s, running)

    async def _delete(self, iid: str, *, timeout_s: float = 120.0) -> bool:
        """删除并以 `get` 确认 `not_found`。返回是否在时限内确认。"""
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
        """删除全部被测实例：一个删除失败不影响其余，也不覆盖调用方的原始错误。返回未能确认删除的 iid。"""
        leaked = []
        for iid in list(self._created):
            try:
                if not await self._delete(iid):
                    leaked.append(iid)
            except FlotillaError:
                leaked.append(iid)
        return leaked

    # ───────────────────────────── C1 ─────────────────────────────

    async def lifecycle(self, declared_visibility_s: float, max_ttl_s: int | None) -> dict[str, Item]:
        # 可见时限从创建请求发出起算（C1 第 3 条）：create 返回后立刻开始列出，不等 Running。
        sent = self.clock.monotonic()
        handle = await self._create(self._spec("lifecycle"), wait=False)
        seen_after = await self._listed_after(handle.iid, sent, declared_visibility_s)
        await self._wait_running(handle)
        expires = (await self.platform.get(handle.iid)).expires_at
        label_item = _probed(
            seen_after is not None and seen_after <= declared_visibility_s,
            f"创建请求发出后 {seen_after:.1f}s 出现在按标签列出的结果中（声明时限 {declared_visibility_s}s）"
            if seen_after is not None
            else f"声明时限 {declared_visibility_s}s 内未出现在按标签列出的结果中",
        )

        extend = self.settings.ttl_s + 600 if max_ttl_s is None else min(self.settings.ttl_s + 600, max_ttl_s)
        target = self.clock.now() + timedelta(seconds=extend)
        await self.platform.renew(handle.iid, target)
        renewed = (await self.platform.get(handle.iid)).expires_at
        renew_ok = renewed is not None and abs((renewed - target).total_seconds()) < 5
        renew_item = _probed(renew_ok, f"续期到 {target.isoformat()}，查询得到 {renewed}")

        gone = await self._delete(handle.iid)
        await self.platform.delete(f"flotilla-probe-missing-{uuid.uuid4().hex[:8]}")  # 不存在视为成功：不抛即通过
        crud_item = _probed(
            expires is not None and gone,
            f"查询返回过期时间 {expires}；删除后{'确认 not_found' if gone else '仍可查询'}；删除不存在的 ID 成功",
        )
        return {"create_get_delete": crud_item, "label_list": label_item, "renew": renew_item}

    async def _listed_after(self, iid: str, sent: float, limit_s: float) -> float | None:
        """从 `sent` 起每秒按标签列出一次，返回首次列出时的延迟；超过 `limit_s` 仍未出现时返回 None。"""
        query = {PROBE_LABEL: self.run_id, labels.UNIT: "lifecycle"}
        while True:
            if any(s.iid == iid for s in await self.platform.list(query)):
                return self.clock.monotonic() - sent
            if self.clock.monotonic() - sent > limit_s:
                return None
            await self.clock.sleep(1.0)

    # ───────────────────────────── C2、C3、C7、C10、C6 ─────────────────────────────

    async def create_unit(self) -> tuple[InstanceHandle, Item]:
        """C2 第 4 条与 C15：以 digest 镜像、占位入口创建，可执行命令即满足。"""
        handle = await self._create(self._spec("unit"))
        result = await self.platform.exec(handle, _sh("true"))
        ok = result.exit_code == 0
        return handle, _probed(ok, f"{self.settings.image} 以占位入口创建，执行 true 退出码 {result.exit_code}")

    async def exec_items(self, unit: InstanceHandle) -> dict[str, Item]:
        p = self.platform
        ident = await p.exec(
            unit, _sh('id -u; id -g; pwd; echo "$PROBE_VAR"', uid=1000, gid=1000, cwd="/tmp", PROBE_VAR="x y")
        )
        got = ident.stdout.decode().splitlines()[:4]
        slow = await p.exec(unit, _sh("sleep 30", timeout_s=2.0))
        exec_item = _probed(
            ident.exit_code == 0 and got == ["1000", "1000", "/tmp", "x y"] and slow.timed_out,
            f"以 1000:1000、/tmp 执行读回 {got}；2s 超时的 sleep 30 "
            + ("被终止" if slow.timed_out else f"未被终止（退出码 {slow.exit_code}）"),
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
            f"/etc/hosts 写入后读回{'一致' if first == original + marker else '不一致'}，"
            f"{self.settings.hosts_recheck_s:.0f}s 后{'未被改写' if later == first else '被改写'}；"
            f"以 1000:1000 写入的文件读回 {owned.decode().split()}",
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
            f"刚启动时 running={seen_running}；退出后 found={status.found} running={status.running} "
            f"exit_code={status.exit_code}（期望 7）",
        )
        return {"exec_identity": exec_item, "files": files_item, "background": bg_item}

    async def address(self, unit: InstanceHandle) -> Item:
        """C7：可取得，两次读取一致（生命周期内不变的长时验收由部署者另测）。"""
        first = await self.platform.internal_address(unit)
        second = await self.platform.internal_address(unit)
        return _probed(bool(first) and first == second, first)

    async def resources(self, unit: InstanceHandle) -> tuple[Item, Item, float | None]:
        """C10（cgroup v2 或 v1）。请求 cpu=1 memory=512Mi。

        第 1 条：内存上限等于给定值（OOM 行为与 Compose 一致）。第 2 条：CPU 上限不低于给定值，实际倍数照实记录——
        CPU 给多了不破坏正确性，只影响不同部署之间的可比性。返回 (内存项, CPU 项, CPU 倍数)。
        """
        script = (
            "if [ -f /sys/fs/cgroup/memory.max ]; then cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max; "
            "else cat /sys/fs/cgroup/memory/memory.limit_in_bytes /sys/fs/cgroup/cpu/cpu.cfs_quota_us "
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us; fi"
        )
        out = (await self.platform.exec(unit, _sh(script))).stdout.decode().split()
        memory_ok = out[:1] == [str(_MEMORY_BYTES)]
        cpus = _cpu_limit(out[1:3])  # 请求为 1 核，所以核数即倍数；不限（max / -1）为 None
        verdict = "一致" if memory_ok else "不一致"
        memory = _probed(memory_ok, f"请求 memory={_MEMORY}；cgroup 读得 {out[:1]}（{verdict}）")
        if cpus is None:
            cpu = _probed(True, f"请求 cpu=1；cgroup 读得 {out[1:3]}，CPU 不限")
        else:
            cpu = _probed(cpus >= 1.0, f"请求 cpu=1；cgroup 读得 {out[1:3]}，即 {cpus:g} 核（{cpus:g} 倍）")
        return memory, cpu, cpus

    async def external_none(self, unit: InstanceHandle) -> Item:
        """C6 第 1 条：`none` 下 TCP 连不到公网 IP（5 秒超时）。隐式放行（DNS 等）见 C13 的声明。

        只认 `nc -z` 的连接结果：工具缺失或用法不被接受时是测试本身出错（抛出），不能当作"不可达"。
        `nc -z` 不带地址时以退出码 1 报"连不上"——用一个必然连不上的回环端口先确认这条判断路径可用。
        """
        script = (
            "command -v nc >/dev/null && command -v timeout >/dev/null || { echo NOTOOL; exit 0; }; "
            'timeout 5 nc -z 127.0.0.1 1 2>/dev/null; echo "self=$?"; '
            f'timeout 5 nc -z {_PUBLIC_IP} 443 2>/dev/null; echo "public=$?"'
        )
        out = (await self.platform.exec(unit, _sh(script, timeout_s=_EXEC_TIMEOUT_S))).stdout.decode().split()
        if out[:1] == ["NOTOOL"] or not (len(out) == 2 and out[0] == "self=1"):
            raise FlotillaError(
                f"C6 none 无法测：被测镜像须有可用的 nc -z 与 timeout（得到 {out}）",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        reachable = out[1] == "public=0"
        return _probed(
            not reachable, f"none 策略下 TCP {_PUBLIC_IP}:443 {'可达' if reachable else '不可达'}（{out[1]}）"
        )

    # ───────────────────────────── C8 ─────────────────────────────

    async def storage(self) -> dict[str, Item]:
        """第 1、3、6 条：锚点建兄弟目录与标记；实例只读挂载其一；锚点删除。"""
        base = f"launches/{self.anchor.launch_id}/{self.run_id}"
        await self.anchor.mkdir(f"{base}/visible/inner", stage="prepare")
        await self.anchor.mkdir(f"{base}/sibling", stage="prepare")
        try:
            vol = SharedVolume(key=f"{base}/visible", mount_path="/probe-ro", read_only=True)
            unit = await self._create(self._spec("storage", (vol,)))
            # 隔离：挂载点里只有所挂子目录的内容（`inner`），且挂载的就是这个子目录。mountinfo 里子目录可能体现在
            # 挂载根（第 4 列，bind / K8s subPath）或挂载源（"-" 之后第 2 列，如 NFS 直挂 server:/export/<子路径>），
            # 两者之一须以所挂子路径结尾——不能是共享根目录或它的上层（`_mounted_subdir`）。
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
                f"挂载可读（{'见' if 'inner' in out else '不见'}标记目录），root 写入"
                + ("失败" if "WROTE" not in out else "成功"),
            ),
            "subdir_isolation": _probed(
                isolated, f"挂载点内容 {listing}，挂载源 {root or '未知'}" + ("" if isolated else "（不是所挂子目录）")
            ),
            "dir_management": _probed(
                removed, "锚点创建与删除共享根目录下的子目录" + ("成功" if removed else "后目录仍在")
            ),
        }


__all__ = ["PROBE_LABEL", "Declared", "ProbeSettings", "probe"]
