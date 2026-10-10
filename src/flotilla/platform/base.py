"""flotilla 的平台抽象接口 `Platform` 及其数据类型。

对应 `docs/Architecture.md` 第 2 节。编排核心（`flotilla.core`）对平台的全部调用都经本模块的
`Platform` 协议；接口按 flotilla 需要的**语义**定义，不按某个平台的原语定义。本模块不 import
任何后端，也不出现任何平台概念（规则、CIDR、网段、字段名）。

本模块还定义 `Clock` 协议（§2.3"时钟"）：编排核心的时序不变量——`G + list_visibility_s < W`、
gc 两次观察、续期宽限——要求时间可注入、可在测试中手动推进，因此 core 一律经注入的 `Clock`，
不直接使用挂钟（由 ruff banned-api 强制，见 `pyproject.toml`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

# ─────────────────────────────────────────────────────────────────
# 错误分类（§2.4、§5.4）
# ─────────────────────────────────────────────────────────────────


class ErrorCategory(StrEnum):
    """错误类别，编排核心据此决定重试与向使用方报告的 `retryable`（§2.4、§5.4）。

    前 6 个是后端把平台错误映射到的类别（§2.4）；`LOST` 与 `SERVICE` 由编排核心在运行期判定（§5.4）：
    `LOST` 是就绪后的基础设施故障（可重试），`SERVICE` 是任务自身的错——服务启动失败、健康检查失败、
    一次性服务退出非 0、带 restart 策略的服务退出（不可重试，样本作废）。`SERVICE`（类别）与
    `FlotillaError.service`（服务名）是两回事。
    """

    RATE_LIMITED = "rate_limited"
    CAPACITY = "capacity"
    TRANSIENT = "transient"
    IMAGE = "image"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    LOST = "lost"
    SERVICE = "service"


# trial 的阶段标签（§5.1、§5.4）。
Stage = Literal["prepare", "create", "address", "wire", "hosts", "start", "run"]


class InstanceStatus(StrEnum):
    """归一后的实例状态（§2.3"状态归一"）。后端把平台原生状态映射到这几个值，原生串放 `InstanceState.reason`。

    编排核心只认这几个语义状态，不依赖任何平台的原始拼写（§5.1 等 `RUNNING`、§5.7 判 `lost`）：
    `PENDING` 尚未可执行命令；`RUNNING` 可执行命令；`TERMINAL` 已终止（失败、退出或正在删除），不会再回到
    `RUNNING`；`UNKNOWN` 后端无法归一（含平台日后新增的状态），编排核心按不健康处理。
    """

    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class FlotillaError(Exception):
    """flotilla 的错误基类，带 `stage` / `category` / `retryable`（§5.4）。

    xtuner 按属性（而非类型）识别可重试性（`XTuner_Environment_Design.md` 5.2 节），
    所以这三个属性是对外契约的一部分。
    """

    def __init__(
        self,
        message: str,
        *,
        stage: Stage,
        category: ErrorCategory,
        retryable: bool,
        service: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.retryable = retryable
        self.service = service


# ─────────────────────────────────────────────────────────────────
# 能力声明（§2.2）
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capabilities:
    """来自部署的能力报告（`flotilla probe` 的产物），provider `start` 时加载（§2.2）。

    字段与 `Platform_Requirements.md` 附录 A一一对应，不对应任何平台字段。
    只收首版接口会用到的能力；能力报告可多记录其他项供评估，但不进入这里。
    """

    # C1 生命周期
    max_label_value_len: int
    list_visibility_s: float
    max_ttl_seconds: int | None
    # C4 / C5 组内互联与入站隔离
    link: bool
    link_udp: bool
    link_max_members: int
    inbound_isolation: bool  # 安全类
    # C6 / C13 外部出站
    external_forms: frozenset[str]  # {"none", "any", "allowlist"} 的子集
    implicit_egress: tuple[str, ...]
    # C7 实例地址
    internal_address: Literal["api", "exec", "none"]
    # C8 共享存储
    shared_volume: bool
    volume_file: bool
    no_auto_mount: bool  # 安全类
    # C9 执行通道鉴权
    exec_auth: bool  # 安全类
    # C14 可选运行时
    privileged_runtime: bool
    devices: frozenset[str]  # {"fuse", "kvm"} 的子集


# ─────────────────────────────────────────────────────────────────
# 创建参数与值对象（§2.3）
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resources:
    """CPU / 内存限制值（C10），原值交给平台，不做换算。"""

    cpu: str  # 例 "2"、"0.5"
    memory: str  # 例 "4Gi"


@dataclass(frozen=True)
class SharedVolume:
    """共享存储上的一个子目录挂载（§2.3、§7.2）。"""

    key: str  # 相对共享根目录的路径，不含 ".."；锚点的 key 为空串（共享根目录本身）
    mount_path: str
    read_only: bool


@dataclass(frozen=True)
class ExternalPolicy:
    """单个实例对组外目标的出站策略（§6.4），trial 中不再改变。"""

    mode: Literal["none", "any", "allowlist"]
    hosts: tuple[str, ...] = ()  # 域名
    cidrs: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstanceSpec:
    """创建一个实例的参数。只含 flotilla 的概念（§2.3）。

    没有环境变量字段：业务环境变量可能含口令，而平台查询接口可能原样返回创建参数，
    所以只随执行传入（§3.5）。
    """

    image: str  # repo@sha256:…（C15）
    entrypoint: tuple[str, ...]  # 占位入口（§3.4），不是业务命令
    labels: Mapping[str, str]
    timeout_seconds: int  # 必须有限
    resources: Resources
    volumes: tuple[SharedVolume, ...]
    external: ExternalPolicy
    privileged: bool = False
    devices: frozenset[str] = frozenset()
    #: 放置组（C4 第 3 条）：值相同的实例由部署放到彼此可以互联的位置。flotilla 只表达"同组"，不指定放在哪里；
    #: 不需要的部署忽略它。trial 给需要互联的单元同一个值，每个 trial 一个。
    group: str | None = None


@dataclass(frozen=True)
class ProcessSpec:
    """一次执行（前台或后台）的进程规格（§2.3）。

    进程环境**恰好**是 `env`，不混入平台组件的环境（后端负责补齐，见后端文档 3.6 节）。
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    env: Mapping[str, str]  # 进程的完整环境
    timeout_s: float | None  # exec 必填；后台进程为 None


@dataclass(frozen=True)
class Topology:
    """组内互联的拓扑：网络名 → 成员名（members 的键）（§2.3、§6.2）。"""

    networks: Mapping[str, frozenset[str]]


# ─────────────────────────────────────────────────────────────────
# 实例句柄与状态（§2.3）
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstanceHandle:
    """创建返回的实例句柄。

    约定：handle 永远可从 `iid` 单独重建——即 handle 不携带任何无法从 `iid` 导出的信息。
    §5.5 的创建对账路径里，core 只能从 `list(labels)` 拿到 `iid`（响应丢失、实例已建的情形），
    之后要对它 `internal_address` / `link` / `exec`，必须凭 `iid` 造出 handle。后端若需要 execd 地址
    等细节，自行维护 `iid → 细节` 的内部映射（OpenSandbox 的 execd 地址本就经 iid 解析），不塞进 handle。
    """

    iid: str


@dataclass(frozen=True)
class InstanceState:
    """`get` / `list` 返回的实例状态（§2.3）。`reason` / `message` 只用于诊断与错误消息（C12）。"""

    iid: str
    status: InstanceStatus  # 归一后的语义状态；平台原生状态串放 reason
    expires_at: datetime | None
    labels: Mapping[str, str]
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ExecResult:
    """前台执行的结果（§2.3）。

    超过 `ProcessSpec.timeout_s` 时后端终止进程并返回 `timed_out=True`（`exit_code` 为后端得到的值，不应依赖）。
    超时是被执行命令的结果，不是执行通道的故障：健康检查把它算作一次失败（§3.6），不当作基础设施错误。
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


@dataclass(frozen=True)
class ProcessStatus:
    """后台进程的状态查询结果（§2.3、§3.6）。

    `found=False` 表示查询不到（例如执行守护进程重启后丢失进程记录），编排核心按 `lost` 处理。
    """

    found: bool
    running: bool
    exit_code: int | None


# ─────────────────────────────────────────────────────────────────
# 时钟注入缝
# ─────────────────────────────────────────────────────────────────


@runtime_checkable
class Clock(Protocol):
    """可注入的时钟。编排核心一律经它取时间与等待，便于时序不变量的确定性测试。

    真实实现挂在系统时钟与 `asyncio.sleep` 上；测试用可手动推进的实现。
    """

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


# ─────────────────────────────────────────────────────────────────
# 平台接口（§2.3）
# ─────────────────────────────────────────────────────────────────


@runtime_checkable
class Platform(Protocol):
    """编排核心对平台的全部调用都经本协议。语义与后端契约见 `docs/Architecture.md` 2.3 节。"""

    caps: Capabilities

    # 生命周期（C1）
    async def create(self, spec: InstanceSpec) -> InstanceHandle:
        """创建实例。不幂等，编排核心不重试，而是对账（§2.4、§5.5）。

        抛错时平台可能已经接受了请求。后端须把"不知道平台是否收到"的情形（5xx、连接中断、超时）报为
        `transient`——编排核心只对它按标签对账、找回实例；其他类别按"没有可用的实例"报告，但释放时
        仍按标签扫尾。调用可被取消（编排核心在 launch 停用时取消在途的创建）。
        """
        ...

    async def get(self, iid: str) -> InstanceState: ...

    async def delete(self, iid: str) -> None:
        """删除；不存在视为成功。"""
        ...

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        """按标签过滤、内部翻页取全；只用于对账、回收与 gc（§5.5、§5.6）。"""
        ...

    async def renew(self, iid: str, expires_at: datetime) -> None: ...

    # 执行、进程、文件（C2、C3）
    async def exec(self, handle: InstanceHandle, proc: ProcessSpec) -> ExecResult:
        """前台执行：返回退出码与输出；超过 `proc.timeout_s` 时终止进程，返回 `timed_out=True`。"""
        ...

    async def start_process(self, handle: InstanceHandle, proc: ProcessSpec) -> str:
        """后台启动进程，返回进程 ID。"""
        ...

    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus:
        """查询后台进程状态；查询不到时返回 `found=False`。"""
        ...

    async def write_file(
        self,
        handle: InstanceHandle,
        path: str,
        data: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None: ...

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes: ...

    # 地址（C7）
    async def internal_address(self, handle: InstanceHandle) -> str: ...

    # 网络（C4、C5）
    async def link(
        self,
        members: Mapping[str, InstanceHandle],
        topology: Topology,
    ) -> None:
        """组内互联：调用返回时拓扑已生效（C4 第 4 条）。幂等（§2.3）。"""
        ...
