"""组装 `OpenSandboxPlatform`：实现 `platform.base.Platform`（backends/opensandbox.md 第 3、4、8 节）。

把编排核心的 `Platform` 调用落到标准接口上：生命周期走 `Lifecycle`（控制面 `Http`），执行/进程/文件走
`Execd`（按 iid 解析到该实例的 execd），`link` 用 `cidr` 策略经 `networkPolicy` 整体替换（§4.5）。

**execd 寻址**由注入的 `execd_http(iid) -> Http` 决定，访问模式（server 代理 / gateway / 直连 pod IP，§3.1）
留在工厂里、不写死在这里；`build()` 给出默认的 server 代理工厂（全部实例共用一个 `httpx.AsyncClient`）。
handle 只携带 `iid`（base.py `InstanceHandle` 契约），其余按 iid 在后端内部维护：

- execd 地址经 iid 解析，每次现用现建（只是路径前缀，不持有资源）；
- 每实例的 `ExternalPolicy` 与 execd 访问 token（§5）在 `create` **发请求之前**按标签记下：创建响应丢失、经对账
  找回的实例没有 iid 记录，用到时 `get(iid)` 取标签再对上（每个单元的 launch / trial / unit 标签唯一，§5.5）；
- 内部地址在实例存活期间不变，读到后按 iid 缓存，`link` 不再重读。

**execd 访问 token**：每个实例创建时生成一个随机 token，经创建请求的 `env.EXECD_ACCESS_TOKEN` 交给实例内的
execd，之后每个 execd 请求带 `X-EXECD-ACCESS-TOKEN`（上游 execd 的同名约定）。token 只在本进程内存中，不落盘、
不进日志；进程重启后，上一次运行的实例只会被删除（控制面），不再需要它的 token。不是本进程创建的实例不能执行。

`delete` 清掉该 iid 的全部记录；`aclose()` 关闭共用的 client。

创建请求（§3.3）由三部分组成：flotilla 管理的字段（镜像、入口、标签、TTL、资源、卷、`networkPolicy`、
只含 execd 访问 token 的 `env`）、`extensions`（原样；配置了 `group_extension` 时再写入放置组）、`create_fields`
（部署配置给出的其余顶层字段，原样合并，例如附加的 `ports`）。
`create_fields` 不能覆盖 flotilla 管理的字段与 `extensions`——否则部署配置能绕过初始隔离、TTL 等保证，构造时即拒绝。

资源同时写 `resourceLimits` 与 `resourceRequests`（等值，上游 spec 两个字段都有；等值即 Guaranteed QoS）：只认
`resourceRequests` 的兼容部署也拿到原值（§3.4）。

不支持 pool 模式；默认 server 代理。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from flotilla.platform.base import (
    Capabilities,
    Clock,
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    InstanceState,
    ProcessSpec,
    ProcessStatus,
    Stage,
    Topology,
)
from flotilla.platform.opensandbox import address
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings
from flotilla.platform.opensandbox.http import DEFAULT_RETRY, Http, RetryPolicy
from flotilla.platform.opensandbox.lifecycle import Lifecycle
from flotilla.platform.opensandbox.network import NetworkSettings, compile_policy
from flotilla.platform.opensandbox.storage import StorageSettings

CREDENTIAL_HEADER = "OPEN-SANDBOX-API-KEY"  # 凭证只放请求头（§1、原则 8）
EXECD_PORT = 44772  # 执行通道端口（§3.1）
EXECD_TOKEN_ENV = "EXECD_ACCESS_TOKEN"  # execd 读取访问 token 的环境变量（§5）
EXECD_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
API_VERSION = "v1"
MIN_TTL_SECONDS = 60  # timeout 下限（§3.4）
#: 单次请求的默认超时（命令流另有总时限，见 `http.stream_post`）。读超时 30s 远大于 execd 的 ping 间隔（3s）。
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_MAX_CONNECTIONS = 100  # httpx 默认值；每个进行中的前台 exec 独占一条连接
#: flotilla 管理的创建请求字段（§3.3），`create_fields` 不能出现。`env` 也在内：只放 execd 访问 token，业务环境只随
#: 执行传入（§3.3）。
MANAGED_CREATE_FIELDS = frozenset(
    {
        "image",
        "entrypoint",
        "metadata",
        "timeout",
        "resourceLimits",
        "resourceRequests",
        "volumes",
        "networkPolicy",
        "extensions",
        "env",
    }
)


def _label_key(labels: Mapping[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


@dataclass(frozen=True)
class _Unit:
    """本进程创建的实例在后端里的记录。"""

    external: ExternalPolicy  # 创建时的外部策略（link 重编译用，§4.5）
    token: str  # execd 访问 token（§5）


class OpenSandboxPlatform:
    """OpenSandbox 后端。满足 `flotilla.platform.base.Platform` 协议。"""

    def __init__(
        self,
        lifecycle: Lifecycle,
        execd_http: Callable[[str], Http],
        *,
        settings: NetworkSettings,
        storage: StorageSettings,
        caps: Capabilities,
        execd: ExecdSettings | None = None,
        extensions: Mapping[str, Any] | None = None,
        privileged_extensions: Mapping[str, Any] | None = None,
        create_fields: Mapping[str, Any] | None = None,
        group_extension: str | None = None,
        create_timeout_s: float | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        clash = sorted(MANAGED_CREATE_FIELDS & set(create_fields or {}))
        if clash:
            raise ValueError(f"create_fields 不能覆盖 flotilla 管理的创建字段：{clash}")
        if group_extension is not None and (
            group_extension in (extensions or {}) or group_extension in (privileged_extensions or {})
        ):
            raise ValueError(f"extensions 不能设置放置组的键 {group_extension!r}：它由 flotilla 按 trial 写入")
        self.caps = caps
        self._lifecycle = lifecycle
        self._execd_http = execd_http
        self._settings = settings
        self._storage = storage
        self._execd = execd or ExecdSettings()
        self._create_fields = dict(create_fields or {})
        self._extensions = dict(extensions or {})
        self._privileged_extensions = dict(privileged_extensions or {})
        self._group_extension = group_extension
        self._create_timeout_s = create_timeout_s
        self._close = close
        self._units: dict[str, _Unit] = {}  # iid → 本进程创建的实例
        self._pending: dict[frozenset[tuple[str, str]], _Unit] = {}  # 标签 → 已发出创建请求的实例（对账找回用）
        self._addresses: dict[str, str] = {}  # iid → 内部地址（存活期间不变）

    # ───────────────────────────── 生命周期（C1）─────────────────────────────

    async def create(self, spec: InstanceSpec) -> InstanceHandle:
        unit = _Unit(external=spec.external, token=secrets.token_urlsafe(32))
        body = self._create_body(spec, unit.token)
        key = _label_key(spec.labels)
        self._pending[key] = unit  # 先记：响应丢失时凭标签找回（§5.5）
        try:
            iid = await self._lifecycle.create(body, timeout=self._create_timeout_s)
        except FlotillaError as exc:
            if exc.category is not ErrorCategory.TRANSIENT:
                self._pending.pop(key, None)  # 平台明确拒绝：没有实例，不会被对账找回
            raise
        self._units[iid] = self._pending.pop(key)
        return InstanceHandle(iid=iid)

    async def get(self, iid: str) -> InstanceState:
        return await self._lifecycle.get(iid)

    async def delete(self, iid: str) -> None:
        await self._lifecycle.delete(iid)
        self._forget(iid)

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        return await self._lifecycle.list(labels)

    async def renew(self, iid: str, expires_at: datetime) -> None:
        await self._lifecycle.renew(iid, expires_at)

    # ───────────────────────────── 执行 / 进程 / 文件（C2、C3）─────────────────────────────

    async def exec(self, handle: InstanceHandle, proc: ProcessSpec) -> ExecResult:
        return await (await self._execd_of(handle.iid, stage="run")).exec(proc)

    async def start_process(self, handle: InstanceHandle, proc: ProcessSpec) -> str:
        return await (await self._execd_of(handle.iid, stage="start")).start_process(proc)

    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus:
        return await (await self._execd_of(handle.iid, stage="run")).process_status(pid)

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
        execd = await self._execd_of(handle.iid, stage="run")
        await execd.write_file(path, data, mode=mode, uid=uid, gid=gid)

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes:
        return await (await self._execd_of(handle.iid, stage="run")).read_file(path)

    # ───────────────────────────── 地址（C7）─────────────────────────────

    async def internal_address(self, handle: InstanceHandle) -> str:
        cached = self._addresses.get(handle.iid)
        if cached is None:
            execd = await self._execd_of(handle.iid, stage="address")
            cached = await address.read_internal_address(execd, handle.iid)
            self._addresses[handle.iid] = cached
        return cached

    # ───────────────────────────── 网络（C4、C5）─────────────────────────────

    async def link(self, members: Mapping[str, InstanceHandle], topology: Topology) -> None:
        """每个成员以“外部策略 + 对端 IP”整体替换 `networkPolicy`（§4.5）。PUT 同步生效，返回即生效。"""
        # address 阶段已读过并缓存；这里通常不再发请求。
        addresses = {name: await self.internal_address(handle) for name, handle in members.items()}
        for name, handle in members.items():
            peers = _peers_of(name, topology, addresses)
            external = (await self._unit_of(handle.iid, stage="wire")).external
            policy = compile_policy(external, peers, self._settings)
            await self._lifecycle.put_policy(handle.iid, policy, stage="wire")

    async def aclose(self) -> None:
        """关闭共用的 HTTP client（由创建平台的一方在用完后调用）。"""
        if self._close is not None:
            await self._close()

    # ───────────────────────────── 内部 ─────────────────────────────

    def _create_body(self, spec: InstanceSpec, token: str) -> dict[str, Any]:
        """按 §3.3 组创建请求：`env` 只有 execd 访问 token（业务环境随执行传入）；每个实例都带 `networkPolicy`。"""
        extensions = dict(self._extensions)
        if spec.privileged:
            extensions.update(self._privileged_extensions)
        if spec.group is not None and self._group_extension is not None:
            extensions[self._group_extension] = spec.group
        resources = {"cpu": spec.resources.cpu, "memory": spec.resources.memory}
        body: dict[str, Any] = {
            **self._create_fields,
            "image": {"uri": spec.image},
            "entrypoint": list(spec.entrypoint),
            "metadata": dict(spec.labels),
            "timeout": max(int(spec.timeout_seconds), MIN_TTL_SECONDS),
            "resourceLimits": resources,
            "resourceRequests": dict(resources),
            "volumes": self._storage.compile(spec.volumes),
            "networkPolicy": compile_policy(spec.external, [], self._settings),
            "env": {EXECD_TOKEN_ENV: token},
        }
        if extensions:
            body["extensions"] = extensions
        return body

    async def _execd_of(self, iid: str, *, stage: Stage) -> Execd:
        token = (await self._unit_of(iid, stage=stage)).token
        return Execd(self._execd_http(iid).with_headers({EXECD_TOKEN_HEADER: token}), self._execd)

    async def _unit_of(self, iid: str, *, stage: Stage) -> _Unit:
        """该实例创建前记下的外部策略与 token。没有 iid 记录（对账找回）时按 `get` 到的标签对上。"""
        unit = self._units.get(iid)
        if unit is None:
            state = await self._lifecycle.get(iid)
            unit = self._pending.pop(_label_key(state.labels), None)
            if unit is None:
                raise FlotillaError(
                    f"实例 {iid} 不是本进程创建的，没有它的外部策略与 execd 访问 token",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                )
            self._units[iid] = unit
        return unit

    def _forget(self, iid: str) -> None:
        self._units.pop(iid, None)
        self._addresses.pop(iid, None)


def _peers_of(name: str, topology: Topology, addresses: Mapping[str, str]) -> list[str]:
    """与 `name` 至少共享一个网络的其他成员的内部地址（去重、排序，§4.2）。"""
    peers: set[str] = set()
    for net_members in topology.networks.values():
        if name in net_members:
            peers.update(addresses[other] for other in net_members if other != name and other in addresses)
    return sorted(peers)


def build(
    *,
    endpoint: str,
    credential: str,
    clock: Clock,
    settings: NetworkSettings,
    storage: StorageSettings,
    caps: Capabilities,
    execd: ExecdSettings | None = None,
    extensions: Mapping[str, Any] | None = None,
    privileged_extensions: Mapping[str, Any] | None = None,
    create_fields: Mapping[str, Any] | None = None,
    group_extension: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
    execd_port: int = EXECD_PORT,
    create_timeout_s: float | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
) -> OpenSandboxPlatform:
    """默认 server 代理模式的组装（§3.1）：控制面与 execd 共用一个 client（凭证、超时、连接池），
    execd 经 `/sandboxes/{id}/proxy/{port}` 前缀。用完调 `aclose()`。

    `transport=None` 用 httpx 默认传输（真实部署）；测试注入 `httpx.MockTransport` 按路径路由。
    `max_connections` 是全部实例共用的连接上限：每个进行中的前台 exec 占一条，按并发量调。
    """
    client = httpx.AsyncClient(
        base_url=f"{endpoint.rstrip('/')}/{API_VERSION}",
        transport=transport,
        headers={CREDENTIAL_HEADER: credential},
        timeout=timeout,
        limits=httpx.Limits(max_connections=max_connections),
        trust_env=False,
    )
    control = Http(client, clock, retry)

    def execd_http(iid: str) -> Http:
        return control.with_prefix(f"/sandboxes/{quote(iid, safe='')}/proxy/{execd_port}")

    return OpenSandboxPlatform(
        Lifecycle(control),
        execd_http,
        settings=settings,
        storage=storage,
        caps=caps,
        execd=execd,
        extensions=extensions,
        privileged_extensions=privileged_extensions,
        create_fields=create_fields,
        group_extension=group_extension,
        create_timeout_s=create_timeout_s,
        close=client.aclose,
    )
