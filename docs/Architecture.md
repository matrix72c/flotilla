# flotilla 架构设计

> 状态：目标架构，包含尚未实现的模块与命令。当前可用功能和验证范围以 README 为准。

> 本文回答"flotilla 怎么做"：编排核心、任务编译、回收与使用方适配，以及它对平台的抽象接口 `Platform`（第 2 节）。
> 需求与范围见 `PRD.md`，xtuner 侧接口见 `XTuner_Environment_Design.md`。
>
> 本文不描述任何具体平台：对平台的能力要求由本文的设计推导，写在 `Platform_Requirements.md`（C1–C16）；
> 某个平台怎么实现 `Platform`、各部署满足到什么程度，写在后端文档（目前只有 `backends/opensandbox.md`）。
>
> **网络职责边界**（第 6 节）：flotilla 把 Compose 网络编译为与平台无关的拓扑（哪些单元属于哪些网络）与每个单元的外部策略，
> 按实际地址写入 hosts，在平台确认拓扑生效且 hosts 写入完成后按依赖启动业务。
> 拓扑怎么落到平台的策略、规则与地址上，由后端负责；可达、隔离与生效由平台保证（`Platform_Requirements.md` C4–C6），
> 在部署接入或升级时由契约测试验证（第 14 节），flotilla 不在 trial 中做流量探测。

---

## 1 概览

### 1.1 分层

```
┌──────────────────────────────────────────────────────────────────┐
│ 使用方    xtuner（FlotillaProvider）   Harbor（BaseEnvironment）       │  第 10、11 节
├──────────────────────────────────────────────────────────────────┤
│ 编排核心  任务清单 → Trial：创建、地址、放行、hosts、启动顺序、健康检查、 │  第 3–7 节
│          卷、回收；只调用 Platform 接口                                │
├──────────────────────────────────────────────────────────────────┤
│ 平台后端  Platform 的实现；目前只有 OpenSandboxPlatform               │  第 8 节
├──────────────────────────────────────────────────────────────────┤
│ sandbox 内  平台的执行守护进程（execd）+ 占位入口 + 业务进程              │  第 3 节
└──────────────────────────────────────────────────────────────────┘
```

三层语义分开描述，互不越界：

| 层 | 内容 | 例子 |
|---|---|---|
| Compose / Harbor 语义 | 使用方看到的行为 | `app` 能连 `db:5432`；`db` 健康后 `app` 才启动；`backend` 网络对外不可达 |
| flotilla 机制 | 用 `Platform` 接口实现上述语义的方法 | 把 Compose 网络编译为拓扑交给 `link`，把对端地址写进 `/etc/hosts`；经平台执行通道按依赖顺序启动业务进程、执行健康检查 |
| 平台能力 | 平台必须提供的能力与保证 | `Platform_Requirements.md`：C3 后台进程、C4 组内互联、C5 入站隔离 |

### 1.2 组件

| 组件 | 运行位置 | 职责 |
|---|---|---|
| `flotilla build` | 离线构建机 | 解析 Compose，构建与合并镜像，推送，生成任务清单 |
| `flotilla scan` | 离线 | 对任务逐字段归类（实现 / 等效 / 拒绝），输出报告 |
| 编排核心（`flotilla.core`） | 训练或评测进程内 | 一个 trial 的创建、放行、名字、启动、健康检查、就绪、释放 |
| 回收器（`Reaper`） | 训练或评测进程内，每个 provider 实例一个 | 创建登记、后台删除、重试删除、共享卷清理、维持锚点 |
| 锚点（anchor） | 每个 provider 实例一个小规格 sandbox | 进程存活的心跳；共享存储上的目录操作（7.2 节）；不参与组内互联，外部策略 `none` |
| `flotilla gc` | 训练侧命令 / 进程启动时 | 清理没有存活锚点的 launch 的残留 |
| `flotilla probe` | 训练侧命令 | 部署接入或升级时对该部署运行契约测试（含网络可达、隔离、DNS 行为、策略生效），产出能力报告（2.2、14 节）；不在 trial 路径中运行 |
| 平台后端 | 训练或评测进程内 | 实现 `Platform`；目前只有 OpenSandbox 后端（`backends/opensandbox.md`） |

sandbox 内不运行任何 flotilla 的守护进程，也没有 flotilla 的控制端口。
业务进程由编排核心经平台的执行通道启动与管理（第 3 节）。

### 1.3 原则

1. **按服务调度**：每个 Compose 服务一个 sandbox；sandbox 内不运行 Docker。
2. **只依赖接口**：编排核心不出现任何平台的 URL、字段名、规则写法和错误码，只调用 `Platform`；同一平台的部署差异只在后端配置中。
3. **网络按 Compose 语义**：共享网络即可达，不共享网络即隔离；不额外限制端口或服务；trial 之外（其他 trial、平台上的其他实例、外部）不能入站（6.3 节）；
   出站由 Compose 的 `internal` 与调用方显式给出的外部策略共同决定（第 6 节）。
   flotilla 只给出拓扑与外部策略；其执行、生效与平台基础设施访问由平台保证，flotilla 不以流量探测补偿（6.5 节）。
4. **语义不走样**：每个 Compose 字段要么实现，要么等效，要么拒绝；不静默忽略。平台缺能力导致做不到的，列为缺口并在扫描中报告。
5. **失败早暴露**：所有与启动有关的失败在交给使用方之前发生，并带上服务、阶段和日志。
6. **回收不依赖调用方**：释放立即返回，由 flotilla 实例的回收器完成；进程崩溃后由 `launch_id` 与锚点心跳清理（5.6 节）。
7. **控制面调用可估算、有上限**：健康检查与退出检测经平台执行通道由编排核心驱动，只发生在启动期；运行期的编排核心调用只有每单元低频的存活查询（5.7 节），agent harness 自身对后台进程的存活轮询另计（第 12 节）。
   第 12 节给出估算，每进程的执行调用受 `exec_rate` 硬上限约束。
8. **凭证不下沉**：平台凭证只在训练或评测进程中使用，不进入 sandbox、镜像、任务清单和日志。

### 1.4 术语

| 术语 | 含义 |
|---|---|
| 任务 | 一个 Harbor 格式的任务目录 |
| 任务清单（manifest） | `flotilla build` 的产物，描述一个任务的全部服务、镜像、网络、卷、资源 |
| trial | 一次环境实例：一个任务清单在平台上的一次运行 |
| 服务 | Compose 中的一个 service；一个服务对应一个 sandbox，称为一个**单元**（unit） |
| agent 服务 | 使用方执行 agent 的服务，默认为 `main` |
| `launch_id` | 一个 flotilla 实例（一个 provider，或一次命令行调用）启动时生成的标识，写入它创建的所有 sandbox 标签，用于残留清理。一个进程中可以有多个 |
| 部署 | 一个平台实例（例如一个 OpenSandbox 部署），对应一份后端配置与一份能力报告 |
| 执行通道 | 平台提供的执行、进程、文件接口，控制面经平台网关访问 |
| 内部地址 | sandbox 在平台内部网络中的 IP，trial 内的业务流量使用它和服务原有的端口 |

---

## 2 平台接口

编排核心对平台的全部调用都经过 2.3 节的 `Platform` 接口。接口按 flotilla 需要的**语义**定义（例如"让这组实例按这个拓扑互联"），
不按某个平台的原语定义（例如"给这个实例下发这些 CIDR 规则"）：同一语义在不同平台上的实现方式不同，放在后端里；接口因此不随平台变化。

接口背后平台必须提供的能力与保证，由本文的设计推导，列在 `Platform_Requirements.md`（C1–C16），每项有来源、类别与验收方法。
所有能力都**不由 flotilla 补足**：不以隧道、代理、额外端口规则、流量探测或固定等待替代。缺失时按类别处理：

| 类别 | 缺失时 |
|---|---|
| 必需 | provider `start` 拒绝该部署 |
| 功能 | 扫描器在该部署上拒绝依赖它的任务，其余任务照常 |
| 安全（`inbound_isolation`、`exec_auth`、`no_auto_mount`） | provider `start` **默认拒绝**；配置中逐项显式接受（16.3 节 `accept_insecure`）后照常运行，能力报告、扫描报告与每个 trial 的元数据都标出（理由见 `Platform_Requirements.md` 第 3 节） |
| 规模 | 按部署声明的容量设置限流 |
| 降级 | 能力报告标出，不拒绝 |

### 2.1 接口与能力的对应

| 方法 / 字段 | 用途 | 平台能力 |
|---|---|---|
| `create` `get` `delete` `list` `renew` | 实例生命周期、回收、续期 | C1、C10、C15 |
| `exec`；`start_process` `process_status`；`write_file` `read_file` | 前台执行、后台进程、文件（含 `/etc/hosts`） | C2、C3、C9 |
| `internal_address` | 写 hosts 用的实际地址 | C7 |
| `link` | 组内互联：让一组实例按拓扑互通 | C4、C5 |
| `InstanceSpec.external` | 单个实例对组外目标的出站，创建时确定 | C6、C13 |
| `InstanceSpec.volumes` | 共享存储 | C8 |
| `InstanceSpec.privileged` / `devices` | 可选运行时 | C14 |

flotilla 不发布任何业务端口，也不需要发布自己的端口；控制面只使用执行通道。

### 2.2 能力声明

同一个后端连接不同部署，能力随部署而不同，因此 `Capabilities` 不写在代码里，而是来自该部署的**能力报告**：
`flotilla probe` 对部署运行契约测试（第 14 节）得到，报告记录 endpoint、日期、测试版本与每项结果；provider `start` 时加载，
endpoint 不符或报告缺失时拒绝启动。扫描器用同一份报告决定哪些任务可以在该部署上运行。

```python
@dataclass(frozen=True)
class Capabilities:
    # C1
    max_label_value_len: int
    list_visibility_s: float          # 创建请求从发出起、出现在列表中或确定不会被创建的时限（C1 第 3 条）；对账、回收与 gc 按它等待（2.4、5.5、5.6 节）
    max_ttl_seconds: int | None       # None 表示只受下限约束
    # C4 / C5
    link: bool                        # C4 第 1、3、4 条：TCP 可达、与放置无关、返回即生效
    link_udp: bool                    # C4 第 2 条
    link_max_members: int
    inbound_isolation: bool           # 安全类：C5
    # C6 / C13
    external_forms: frozenset[str]    # {"none", "any", "allowlist"} 的子集
    implicit_egress: tuple[str, ...]  # 平台为自身基础设施隐式放行的目标与范围，只用于报告（6.5 节）
    # C7
    internal_address: Literal["api", "exec", "none"]   # 从平台接口取得 / 经执行通道读取 / 取不到
    # C8
    shared_volume: bool               # 跨节点读写共享（C8 第 2 条）
    volume_file: bool                 # 子路径可以指向单个文件（文件 bind，4.4 节）
    no_auto_mount: bool               # 安全类：C8 第 5 条
    # C9
    exec_auth: bool                   # 安全类：三条到达路径都校验按实例下发的凭证（3.8 节）
    # C14
    privileged_runtime: bool
    devices: frozenset[str]           # {"fuse", "kvm"}
```

字段与 `Platform_Requirements.md` 附录 A一一对应，不对应任何平台字段。某项为 `False` 时编排核心不绕行，按本节开头的类别处理：
`link = False` 时多服务任务全部拒绝，单服务任务不受影响。

`Capabilities` 只收首版接口会用到的能力。能力报告可以多记录其他项（C14 的其余子项、C16 多容器）供评估，但它们不进入 `Capabilities`，
编排核心与扫描器一律按不具备处理；要启用某一项，先在 `InstanceSpec` / `ProcessSpec` 中加入参数并补契约测试（第 17 节）。

### 2.3 平台接口

```python
class Platform(Protocol):
    caps: Capabilities

    # 生命周期（C1）
    async def create(self, spec: InstanceSpec) -> InstanceHandle: ...
    async def get(self, iid: str) -> InstanceState: ...                  # 归一状态、过期时间
    async def delete(self, iid: str) -> None: ...                        # 不存在视为成功
    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]: ...   # 内部翻页取全；只用于对账、回收与 gc
    async def renew(self, iid: str, expires_at: datetime) -> None: ...

    # 执行、进程、文件（C2、C3）
    async def exec(self, handle: InstanceHandle, proc: ProcessSpec) -> ExecResult: ...      # 前台：退出码与输出
    async def start_process(self, handle: InstanceHandle, proc: ProcessSpec) -> str: ...    # 后台，返回进程 ID
    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus: ...  # running / exit_code；查询不到为 found=False
    async def write_file(self, handle: InstanceHandle, path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None: ...
    async def read_file(self, handle: InstanceHandle, path: str) -> bytes: ...

    # 地址（C7）
    async def internal_address(self, handle: InstanceHandle) -> str: ...

    # 网络（C4、C5）
    async def link(self, members: Mapping[str, InstanceHandle], topology: Topology) -> None: ...
```

参数与返回值只包含 flotilla 的概念，都是不可变值（`frozen`，序列用 `tuple`，映射只读）：

```python
@dataclass(frozen=True)
class InstanceSpec:
    image: str                       # repo@sha256:…（C15）
    entrypoint: tuple[str, ...]      # 占位入口（3.4 节），不是业务命令
    labels: Mapping[str, str]
    timeout_seconds: int             # 必须有限
    resources: Resources             # cpu / memory 限制值（C10）
    volumes: tuple[SharedVolume, ...]   # 共享存储上的子目录（第 7 节）
    external: ExternalPolicy         # 对组外目标的出站（6.4 节），trial 中不再改变
    privileged: bool = False         # 需要 C14 的特权运行时
    devices: frozenset[str] = frozenset()

@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    env: Mapping[str, str]           # 进程的完整环境
    timeout_s: float | None          # exec 必填；后台进程为 None

@dataclass(frozen=True)
class SharedVolume:
    key: str                         # "launches/<launch>/<trial>/<volume>"，或锚点的 ""（共享根目录）
    mount_path: str
    read_only: bool

@dataclass(frozen=True)
class Topology:
    networks: Mapping[str, frozenset[str]]   # 网络名 → 成员名（members 的键）

@dataclass(frozen=True)
class ExternalPolicy:
    mode: Literal["none", "any", "allowlist"]
    hosts: tuple[str, ...] = ()      # 域名
    cidrs: tuple[str, ...] = ()

@dataclass(frozen=True)
class InstanceHandle:
    iid: str                         # 只有 iid，见下文"句柄"

class InstanceStatus(StrEnum):
    PENDING = "pending"              # 尚未可执行命令
    RUNNING = "running"              # 可执行命令
    TERMINAL = "terminal"            # 已终止：失败、退出或正在删除，不会再回到 RUNNING
    UNKNOWN = "unknown"              # 后端无法归一的状态

@dataclass(frozen=True)
class InstanceState:
    iid: str
    status: InstanceStatus
    expires_at: datetime | None
    labels: Mapping[str, str]
    reason: str | None = None        # 平台原生状态与原因，只用于诊断与错误消息（C12）
    message: str | None = None

@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False          # 超过 timeout_s 被终止；是命令的结果，不是执行通道的故障

@dataclass(frozen=True)
class ProcessStatus:
    found: bool                      # False：查询不到（执行守护进程重启后丢失了记录），编排核心按 lost 处理（5.7 节）
    running: bool
    exit_code: int | None
```

`InstanceSpec` 没有环境变量字段：业务环境变量可能含口令，而平台查询接口可能原样返回创建参数，所以只随执行传入（3.5 节）。

**状态归一**：后端把平台的原生状态映射到 `InstanceStatus`，原生状态串放进 `reason`；编排核心只按 `status` 判断（5.1 节等待 `RUNNING`、5.7 节判 `lost`），
代码中不出现任何平台的状态拼写。归不进前三类的原生状态（包括平台日后新增的）为 `UNKNOWN`，编排核心按"不健康"处理：创建阶段视同未就绪，运行期视同终止。
映射表在后端文档中（OpenSandbox：3.5 节）。

**句柄**：`InstanceHandle` 只含 `iid`，任何时候都能由 `iid` 单独重建。对账（2.4、5.5 节）在创建响应丢失时只能从 `list` 拿到 `iid`，之后仍要对该实例 `internal_address` / `link` / 执行；
后端需要的其他信息（例如执行守护进程的访问地址）由后端按 `iid` 自行解析与缓存，不放进句柄。

**时钟**：编排核心的时间与等待一律经注入的 `Clock`，不直接使用 `time`、`asyncio.sleep`、`asyncio.timeout` 或 `datetime.now`：

```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # 墙钟：续期的 expires_at 等写给平台的时间
    def monotonic(self) -> float: ...       # 单调时钟：宽限 G、可见时限、两次观察等时序判断
    async def sleep(self, seconds: float) -> None: ...
```

`G + list_visibility_s < W`、gc 两次观察、续期宽限这些时序不变量（5.5、5.6 节）因此能在假平台上以手动推进的时钟确定性地测试（第 14 节）；该约束由 lint 强制（16.2 节）。

**语义**（后端必须满足，契约测试验证）：

- **执行**：`exec` 与 `start_process` 启动的进程以 `proc.uid` / `proc.gid` 运行，环境**恰好**是 `proc.env`，不混入平台组件（例如执行守护进程）的环境；
  平台本身做不到时由后端补齐（OpenSandbox 见后端文档 3.6 节）。`exec` 超过 `timeout_s` 时终止进程，返回 `timed_out=True`（健康检查把它算作一次失败，3.6 节）；
- **初始隔离**：实例创建后不能与任何其他实例互通，只按 `spec.external` 访问外部；该状态在实例可执行命令之前已经生效（C5）；
- **`link(members, topology)`**：`members` 是 trial 中至少在一个网络里的单元（6.2 节）。调用返回时，至少共享一个网络的两个成员之间经 `internal_address` 的地址以任意 TCP（`link_udp` 时含 UDP）端口可达，
  不共享网络的成员之间、成员与组外任何实例之间不可达，各成员的 `spec.external` 不变。每个 trial 只在 `wire` 阶段调用一次；失败时以相同参数重试，所以调用必须幂等。
  平台只能异步应用时，后端等到平台的完成状态再返回；平台没有完成状态时，后端报告 `link = False`，而不是以等待或探测代替；
- **删除不撤销互联**：trial 的全部单元保留到 `release` 一起删除（5.3 节），编排核心不在删除前撤销放行。后端按地址放行时，一个成员被删除后其地址可能分给其他 trial 的新实例，
  同组尚未删除的成员在这段时间内仍能连到它：正常释放时只有删除的几秒，成员被平台自行删除时（5.7 节）持续到该 trial 释放。
  这是 C5 的**已知例外**：flotilla 接受这一风险，不为它增加撤销步骤，也不作为部署的安全缺口逐部署接受；按实例身份放行的后端没有这一例外；
- 方法都不接受也不返回任何平台概念（规则、CIDR、策略对象）；它们怎么落到平台上是后端的事。

### 2.4 错误分类

后端把平台错误映射为以下类别，编排核心据此决定重试与向使用方报告的 `retryable`：

| 类别 | 例子 | 后端内重试 | 向使用方报告 |
|---|---|---|---|
| `rate_limited` | HTTP 429 | 是，指数退避 | 用尽后 `retryable=True` |
| `capacity` | 资源不足、调度超时、配额超限 | 否 | `retryable=True` |
| `transient` | 5xx（含平台并发上限的 503）、连接重置、超时 | 是（仅幂等操作） | 用尽后 `retryable=True` |
| `image` | 镜像不存在、拉取失败 | 否 | `retryable=False` |
| `invalid` | 参数被平台拒绝 | 否 | `retryable=False`，需要修正任务或配置 |
| `not_found` | 删除 / 查询时不存在 | — | 删除时视为成功 |

另有两个类别不来自后端，由编排核心在运行期判定（5.4 节）：`lost`（ready 之后的基础设施故障，`retryable=True`）与 `service`（任务自身的错误，`retryable=False`）。
错误上的 `service` 属性（出错服务的名字）与 `service` 类别是两回事。

创建不是幂等操作：超时后不直接重试，而是按 trial 标签对账后再决定。平台保证的是：一个创建请求**从发出起**
`list_visibility_s`（C1 第 3 条，含平台排队、接受与列表同步的全部延迟）之内，要么已出现在列表中，要么永远不会被创建。所以一次列出为空不能说明创建未被接受：

- 从该创建请求发出起算，在 `list_visibility_s` 之内按退避反复列出；期间列出了该单元的实例，即按创建成功处理；
  超过时限仍未出现，才视为未被接受并重试；平台给不出这个上界时（例如请求进入无期限的异步队列）不满足 C1，部署不可用，flotilla 不以"结果未知、一直等"代替；
- 对账结束前该创建的结果是**未知**，留在回收器的"进行中的创建"中，不算结束（5.5 节）；
- 已经创建的实例交给回收器。发出创建请求前先向回收器登记（5.5 节），因此调用方在响应返回前被取消时，
平台已接受的实例也能被找到。

---

## 3 sandbox 内的运行方式

### 3.1 职责划分

sandbox 内没有 flotilla 的入口进程与控制接口。职责按下表划分：

| 平台提供（`Platform_Requirements.md`） | flotilla 提供（编排核心） |
|---|---|
| 执行命令并返回输出与退出码（C2） | Compose 启动顺序（`depends_on` 三种条件） |
| 后台进程：启动、查询状态与退出码（C3） | 健康检查；restart 策略的核对（3.6 节） |
| 文件读写（C2） | 业务进程的命令合成（Docker 规则）、用户、工作目录、环境变量 |
| 挂载与运行时：共享卷、只读、特权运行时（C8、C14） | 准备配置、`/etc/hosts`、任务文件目录、共享卷目录 |
| 执行通道本身及其鉴权（C9，3.8 节） | 经执行通道管理业务进程 |
| 组内互联、入站隔离、外部出站的执行与生效保证，实际地址，平台基础设施访问（C4–C7、C13，第 6 节） | 把 Compose 网络编译为拓扑与外部策略、写 hosts；按依赖启动；按业务整体替换拓扑 |

### 3.2 Compose 运行时语义的承担者

一个容器在 Docker 中由 dockerd 与容器内的 init 提供的运行时语义，在 flotilla 中由下表的机制承担；依赖平台的列出对应的能力（`Platform_Requirements.md`），
做不到的按 4.6 节归类，不静默丢弃。各部署的现状见后端文档。

| 语义 | 承担者 | 能力 / 归类 |
|---|---|---|
| 实例保持运行 | 占位入口（3.4 节） | C2 第 4 条 |
| 启动、状态、日志、事件等控制操作 | 平台执行通道（3.8 节） | C2、C3 |
| 网络就绪 | `link` 返回即生效（C4 第 4 条），trial 中不做流量探测 | C4 |
| 服务名解析、`extra_hosts` | 每个 sandbox 的 `/etc/hosts`（3.7 节） | C2 第 3 条 |
| `hostname` / `domainname` | 名字解析：写入其他单元的 hosts；自身主机名：C14（`hostname`） | 不设置自身主机名，扫描对可能读取它的服务告警，任务不拒绝（4.6 节） |
| 业务命令、用户、工作目录、环境变量 | 编排核心合成，经 `start_process` 以 argv、uid / gid、cwd、env 执行（3.5 节） | C2 第 2 条 |
| `group_add`（附加组） | C14（`supplementary_groups`） | 拒绝 |
| 能力集（`cap_add` / `cap_drop`）、`no-new-privileges` | C14 | `cap_add`、`privileged` 放入特权运行时；`cap_drop`、`security_opt` 拒绝（4.6 节） |
| 退出码 | `process_status` | C3 |
| 日志 | 业务输出重定向到实例内的文件，经 `exec` 读尾部（3.5 节） | — |
| 停止（`stop_signal`、`stop_grace_period`） | 服务只随 sandbox 删除而停止（3.5 节） | 等效 |
| 健康检查 | 编排核心经执行通道执行 `test`（3.6 节） | 调用量见第 12 节（C11） |
| restart 策略 | 不在运行中重启；`check()` 时核对服务是否退出过（3.6 节） | 等效 |
| 运行期故障检测 | 编排核心按 ID 查询（5.7 节） | C1 |
| 子进程收割、`init: true` | 执行通道的进程是执行守护进程的子进程，由平台收割 | C3 |
| 共享 PID / IPC / 网络 namespace（`network_mode: service:X` 等） | 跨 sandbox 无法共享 namespace，一个 sandbox 只有一个根文件系统 | C16；拒绝（4.6 节） |
| tmpfs | 本地目录，启动前清空 | 等效（4.6 节） |
| `read_only` | C14（`readonly_rootfs`） | 拒绝 |
| bind 挂载 | 共享卷或写入镜像（4.4 节） | C8 第 7 条（`volume_file`） |
| `ulimits` | C14 | 拒绝（可按数据集配置放行，扫描报告） |
| `tty` / `stdin_open` | C14（`pty`） | 拒绝 |
| `shm_size` | C14（`shm_size`） | 不超过平台默认值为等效，否则拒绝 |
| 依赖自己是 PID 1 的入口（s6-overlay、systemd） | 业务进程是执行通道的子进程，不是 PID 1 | 拒绝（设计限制，不是平台缺口） |
| 卷的初始内容 | 锚点在创建前写入共享卷目录（7.3 节） | — |

### 3.3 共享目录（share）

share 只放与任务无关、agent 需要的内容，不承载入口进程：

```
/.flotilla/                       ← 共享卷，key = share/releases/<release>，只读
  bin/busybox                     占位入口与后端可能用到的静态工具（3.4 节）
  runtime/miniconda3/             agent 运行时，安装前缀就是 /.flotilla/runtime/miniconda3
  runtime/node/node-v22.14.0/     同上
  harness/lagent/                 agent harness 代码
  harness/agents/<kind>/          各 agent 的配置
  RELEASE.json                    发布号、各组件版本、全部文件的内容清单（sha256）
```

- **挂载点固定、来源按发布号区分**：conda、node 的 shebang 与 prefix 写死了安装路径，挂载点对所有发布都是 `/.flotilla`，不同发布只是不同的子目录。
  share 的内容不能指向挂载点之外（软链接跨出 `/.flotilla` 即失效），构建发布时检查。
- **发布不可变**：`flotilla share publish` 经锚点（7.2 节）在 `share/staging/` 下组装新发布，未改动的组件用硬链接复制（`cp -al`），
  写出 `RELEASE.json` 后原子 `rename` 到 `share/releases/<release>`。provider 配置钉住发布号与 `RELEASE.json` 的 sha256；旧发布由 `flotilla share gc` 删除。
- **只放与任务无关的内容**：数据集、`tests/`、flag、verifier、bind 源与卷种子一律不进 share。
- **完整性**：share 的安全依赖"没有不可信代码能写共享根目录"，即平台不向实例自动挂载未声明的可写目录（C8 第 5 条，安全类 `no_auto_mount`）。
  flotilla 为每个单元声明 share 卷；provider `start` 时经锚点核对 `RELEASE.json` 与文件清单（大小、mtime），全量 sha256 由 `flotilla share verify` 按需执行；
  运行期间被改写发现不了，所以 `no_auto_mount = False` 的部署按安全类默认拒绝。

### 3.4 占位入口

sandbox 的入口只负责让 sandbox 保持运行、不启动任何业务：

```
/.flotilla/bin/busybox sleep 2147483647
```

- 业务进程在拓扑经平台确认生效、hosts 写入并读回一致之后才经执行通道启动（5.1 节），满足 PRD N1；
- 镜像原有的 `ENTRYPOINT` / `CMD` 由任务清单记录，编排核心合成业务命令（3.5 节），不作为 sandbox 入口；
- 平台注入执行通道时可能对镜像有前提（例如入口被包装为 `/bin/sh …`，C2 第 1 条），由部署声明；需要 `/bin/sh` 而镜像没有时，构建加一层 busybox（4.2 节第 7 步）。

### 3.5 业务进程

| 方面 | 做法 |
|---|---|
| 命令 | 按 Docker 规则合成：`entrypoint` 覆盖镜像 `ENTRYPOINT`，`command` 覆盖 `CMD`；shell 形式合成为 `["/bin/sh", "-c", …]`；以 argv 交给 `start_process` |
| 用户 | `user` 或镜像 `USER`，构建时按镜像内的 `/etc/passwd` / `/etc/group` 解析为数字 uid / gid |
| 工作目录 | `working_dir` 或镜像 `WORKDIR` |
| 环境变量 | 镜像 `ENV` → `env_file` → `environment`，后者覆盖前者；变量插值在构建时完成；运行时参数与 agent 叠加在启动时并入（第 10 节）。结果就是业务进程的完整环境 |
| 输出 | 标准输出与错误重定向到实例内的 `/run/flotilla/<service>.log`；失败报告与 Harbor 的逐服务日志经 `exec` 读它的尾部（`tail -c`）。不依赖平台保存输出：平台的命令日志可能没有大小上限，也可能一次全部读入内存 |
| 停止 | 服务进程不单独停止，只随 sandbox 删除而结束；`stop_signal`、`stop_grace_period` 只记录 |
| 保活 | agent 服务要一直可以 `execute` 到 `release`。照搬 Harbor 的 Docker 环境：在任务 Compose 之前叠加一层覆盖，把 `main` 的 `command` 设为 `["sh", "-c", "sleep infinity"]`，任务显式设置的 `command` 优先（4.2 节第 1 步） |

业务进程与使用方的 `execute` 都是执行通道的子进程，处在同一视图中。

**执行上下文**：为与 `docker exec` 一致（继承容器的 `ENV`、`USER`、`WORKDIR`），编排核心为每个服务保存一份执行上下文：上表的环境变量、uid / gid、工作目录。
每次执行通道调用都从它构造完整的 `ProcessSpec`：

| 调用方 | 环境 | 用户与工作目录 |
|---|---|---|
| 业务进程、健康检查 | 上下文 | 上下文 |
| 使用方的 `execute`（每个服务的客户端，第 10 节） | 上下文 → `request.env_vars` → 调用参数，后者覆盖前者 | 调用参数，未给时用上下文 |
| 编排核心自己的操作（读地址、写 hosts、读日志尾部） | 空 | root，`/` |

使用方未给超时时用 provider 的默认上限：平台可能不因客户端断开而终止命令。上下文只在训练进程内存中，不写入 sandbox，也不经平台创建参数。

**使用方 `execute` 的命令形态**：xtuner / Harbor 客户端把命令作为 **shell 字符串**传入（xtuner 的 `exec_in` 会先拼 `export K=V; <command>`，Harbor 对 sidecar 用 `sh -c`、对 main 用 `bash -c`）。客户端据此合成 `argv = ["/bin/sh", "-c", command]`（镜像有 bash 时用 bash，与 Harbor 一致），环境经执行上下文随请求 `envs` 注入（不拼进字符串，见后端 3.6 节）；`detach=True` 走 `start_process`，前台走 `exec`。客户端接口与各方法到 `Platform` 的映射见第 10 节。

### 3.6 健康检查与退出

- **健康检查**由编排核心经执行通道执行：`test`（`CMD` / `CMD-SHELL` / `NONE`）、`interval`、`timeout`、`retries`、`start_period`、`start_interval`，
  判定规则与 Docker 一致；`disable: true` 等同于无健康检查。镜像自带的 `HEALTHCHECK` 在 Compose 未覆盖时生效。
  `CMD-SHELL` 需要镜像内的 `/bin/sh`，与 Docker 相同。
- **只在启动期执行**：Compose 中健康状态只影响启动门控（`depends_on: service_healthy` 与 `up --wait`；Docker 不因 unhealthy 重启容器）。
  因此健康检查从服务启动开始执行，到服务第一次 healthy 之后停止；ready 之后不再执行，`docker inspect` 式的健康状态不再更新，扫描标为等效。
  判定与 Docker 一致：第一次检查在启动后一个间隔（`start_period` 内用 `start_interval`）；退出码 0 即 healthy；`start_period` 内、第一次成功之前的失败不计入 `retries`；
  连续失败达到 `retries` 即 unhealthy，trial 以 `start` 阶段、`service` 类别失败。超过 `timeout` 被终止的检查算一次失败；执行通道本身的瞬时错误不算检查结果，该次跳过。
- **就绪条件与 Harbor 的 `docker compose up --wait` 一致**（PRD F3）：被 `service_completed_successfully` 依赖的服务以退出码 0 结束（Compose 对这类服务只看退出，它的健康检查不执行）；其余带健康检查的服务 healthy 且进程仍在运行（进程已退出时检查成功也不算）；其余服务的进程已启动。
  没有任何服务以 `service_healthy` 等待的健康检查同样要通过，训练与评测的就绪时刻因此一致。
- **启动期的退出检测**：按 `[process] poll_interval`（默认 2 秒，16.3 节）轮询 `process_status`，只覆盖被 `service_completed_successfully` 依赖且尚未完成的服务。
  ready 之后不轮询任何进程状态。
- **restart 策略不在运行中执行**：在原 sandbox 中重启服务，要求平台在主进程退出时清理整个进程树，否则旧进程的子进程残留（占着端口、继续写数据），重启结果不可信。
  首版不重启：restart 策略不是 `no` 的服务，在 `check()`（5.7 节）时查一次 `process_status`；进程已退出、且按策略 Docker 会重启它（`always`、`unless-stopped`；`on-failure` 时退出码非 0），
  就把 trial 报为 `service` 类别、不可重试的错误，样本丢弃，不进入 reward。
  这把"服务退出、按策略被重启"在 Docker 下的结果，换成了"样本作废"：不会产生错误的 reward，但这类 trial 的算力浪费掉，发现也晚到验证之后。
  扫描统计带 restart 策略的服务，M2 按实际作废率决定是否需要真正的重启（第 17 节）。

### 3.7 名字解析：`/etc/hosts`

Compose 中服务通过服务名、`aliases`、`container_name`、`hostname`、`links` 别名互相访问，名字按网络可见。
flotilla 用**内部 IP + 每个 sandbox 自己的 `/etc/hosts`** 实现，不使用平台主机名，也不在 sandbox 内运行 DNS 服务：

| 做法 | 说明 |
|---|---|
| 内容 | 每个单元一份：与本单元至少共享一个网络的每个服务，在这些共享网络上的全部名字（服务名、alias、`container_name`、`hostname`、`links` 别名）→ 该服务的内部 IP |
| 网络可见性 | 与本单元不共享任何网络的服务不出现在本单元的 hosts 中，查询落到平台原有的 DNS，得到 NXDOMAIN，与 Compose 一致 |
| `extra_hosts` | 追加到同一文件 |
| 系统条目 | 保留平台原有的条目（`localhost`、本机主机名等），flotilla 的条目放在带标记的块中，只替换这一块 |
| 写入时机 | `link` 返回之后、业务进程启动之前（5.1 节），经 `write_file` 写入，再 `read_file` 读回比对 |
| 更新 | 写入后不再更新：单元重建只发生在 `wire` 之前（5.4 节），地址在生命周期内不变（C7） |

已知限制（与 Docker 内置 DNS 的差异，扫描报告中说明，归为等效）：

- 直接发 DNS 查询的程序（`dig`、`nslookup`、自带 DNS 客户端的运行时）不读 `/etc/hosts`，解析不到服务名；
- 没有轮询：同一名字对应多个地址（`replicas` / `scale`）时，多数解析器只返回第一条；
- 应用可能缓存解析结果，hosts 更新后需要重新解析才生效；
- 依赖 `/etc/hosts` 可写、平台不在运行中重写它（C2 第 3 条）。

### 3.8 执行通道与鉴权

使用方的 `execute` / `upload` / `download` 与编排核心的进程管理都走平台的执行通道。能连到执行通道的有三条路径：

| 路径 | 说明 |
|---|---|
| 训练侧经平台网关 | 平台凭证只在训练 / 评测进程中（1.3 节原则 8） |
| trial 内其他单元经内部地址直连 | 按 Compose 语义，同网络的服务之间不限制端口（6.2 节），所以其他单元能连到执行通道的端口 |
| 同一单元内的业务进程经本地回环 | — |

网关鉴权只保护第一条路径。flotilla 要求执行通道对三条路径都校验按实例下发的凭证（C9，`exec_auth`）；否则 trial 内任何服务都能以 root 在其他单元执行命令，
agent 攻破任一服务后可以绕过任务的权限边界，reward 可能失真。部署不满足时：

- 按安全类默认拒绝（第 2 节）；显式接受后，扫描报告逐任务标出"判分可能依赖服务间权限边界"的迹象（非 agent 服务持有 secrets、以非 root 运行、agent 服务只在 internal 网络中）；
- flotilla 不以网络规则排除执行通道端口作为替代：那是对 Compose 语义的额外端口限制，且挡不住本地回环。

---

## 4 离线构建：`flotilla build`

### 4.1 输入与输出

| | 内容 |
|---|---|
| 输入 | Harbor 任务目录：`environment/docker-compose.yaml`（或单服务的 `environment/Dockerfile`）、`task.toml` |
| 输出 | 推送到镜像仓库的服务镜像；每个任务一份任务清单 `flotilla.manifest.json`；本地任务文件；扫描报告（第 13 节） |
| 幂等 | 按"构建上下文内容哈希 + Compose 规范化结果 + flotilla 构建器版本 + 影响构建产物的部署输入"得到构建键；键不变则跳过。部署输入目前只有入口包装是否需要 `/bin/sh`（第 2 步，4.2 节第 7 步），取自目标部署的能力报告；同一任务为两种部署构建时得到两个键，清单记录所用的值，provider `open` 时与本部署不符则报 `invalid`。内容哈希与 `FILES.json`（4.7 节）规则相同：逐条目计入路径、类型、mode、uid/gid、文件内容、符号链接目标；不计 mtime、xattr、ACL |

构建在离线构建机上用 Docker（BuildKit）完成，训练侧不构建任何东西。

### 4.2 构建步骤

1. **解析与规范化**：`docker compose config` 等价的处理——合并 `extends` / 多文件、完成变量插值、
   展开短语法（端口、卷、`depends_on`）。插值只用任务目录内的 `.env` 与 `env_file`，不读构建机环境。
   `environment` 中引用了任务目录未提供的变量时不固化，记为该服务的**运行时参数**（清单 `runtime_params`），运行时由 provider 配置给值（第 10 节），
   没配置时用默认值，没有默认值时 `open` 报错。`build.args` 中的插值仍在构建时完成。
   合并时与 Harbor 一样，在任务文件之前叠加 agent 服务的保活覆盖（3.5 节），任务文件可以覆盖它。
2. **扫描**：逐字段归类（4.6 节），按目标部署的能力报告判断；有拒绝项的任务不再构建。
3. **构建镜像**：`build` 的服务用 BuildKit 构建；`image` 的服务拉取后按 digest 固定。
4. **处理 bind 源**：按 4.4 节：写入镜像的写到目标路径；由平台挂载的放入第 6 步的任务文件 `binds/<n>`。
5. **记录镜像元数据**：`ENTRYPOINT`、`CMD`、`USER`（解析为数字 uid / gid）、`WORKDIR`、`ENV`、`HEALTHCHECK`、`STOPSIGNAL`。
6. **导出任务文件**：bind 源（`binds/<n>`）、共享卷的初始内容（`seeds/<volume>.tar`，7.3 节）、全部引用只读的卷的内容（`_volumes/<volume>/`），
   写入本地输出目录 `<out>/<build_key>/files/`。构建机离线、没有平台凭证，**不写共享存储**；这些文件由任务发布（4.7 节）上传。
7. **镜像改动**：服务镜像只在以下情况改动：第 4 步写入目标路径的 bind、7.3 节的 `nocopy` 清空、镜像中没有 `/bin/sh` 而部署声明需要它（3.4 节）。
   公共镜像在不需要这些改动时按原 digest 转推，不产生衍生镜像。
8. **推送并写清单**：镜像以 digest 引用；`task_files` 标为"待发布"（4.7 节）。

### 4.3 镜像命名

镜像仓库（C15）由使用方选择。可以把任务镜像放在同一个仓库中，以 tag 区分：

```
<registry>/<namespace>/<repo>:<task-slug>--<service>--<buildkey12>
```

- `task-slug` 由任务路径规范化得到，超长时截断并附哈希；tag 总长不超过 128；
- 清单中同时记录 digest，运行时以 `repo@sha256:…` 拉取，tag 只供人查看与 `flotilla gc --images` 盘点；
- 可以把公共基础镜像镜像到使用方控制的仓库，减少运行期的外部依赖；同一 digest 只推一次。

### 4.4 bind mount

bind mount 对服务可观察的语义有三点：内容来自源；目标路径原有的内容被遮蔽；`:ro` 时容器内的 root 也不能写。
由平台以共享卷挂载时三点都由平台保证（C8），不需要 sandbox 内的 `mount` 与特权运行时。

**同源的引用按源组统一处理。** 规范化后源路径相同、或一个是另一个的子路径的 bind 引用构成一个**源组**。
源组中只要有一个写者，所有引用（包括 `:ro` 的）都要看到同一份可变数据。

| 情况 | 做法 | 类别 |
|---|---|---|
| 源在任务目录外（`/var/run/docker.sock`、宿主路径） | — | 拒绝 |
| 源组没有写者 | 每个引用由平台只读挂载任务文件 `tasks/<build_key>/binds/<n>`（引用源的子路径时 key 指向对应子路径），所有 trial 共用 | 实现 |
| 源组有写者，只有这一个引用 | 构建时写入镜像目标路径：先删除原有内容再写入，目录遮蔽也成立 | 等效 |
| 源组有写者，有多个引用（同一服务多次挂载，或跨服务） | 锚点在 trial 创建前把 `binds/<n>` 复制为本 trial 的 `launches/<launch>/<trial>/binds/<n>`（保留属主与权限），每个引用按自己的读写模式挂载 | 实现 |

- 写入不会回到共用的任务目录；
- 源是单个文件时需要平台支持子路径指向文件（C8 第 7 条，`volume_file`）；不支持时拒绝，`secrets` / `configs` 例外：
  同一服务的全部 secrets 放在一个目录中，以只读卷挂载到 `/run/secrets`，扫描标为等效；
- 目标位于另一个卷的挂载点之下（嵌套挂载）时，按 Compose 的顺序（外层先挂）写进 `volumes` 列表，依赖平台按列表顺序挂载（C8 验收覆盖）。

### 4.5 任务清单

schema 定义在 `src/flotilla/manifest.py`（Pydantic，`extra="forbid"`），示例：

```jsonc
{
  "schema": 2,
  "task": "examples/web-app",
  "build_key": "…",
  "agent_service": "main",
  "units": {
    "target": {
      "image": "registry/…@sha256:…",
      "resources": {"cpu": "2", "memory": "8192Mi"},           // 取自 task.toml（C10）
      "context": {"env": {…}, "uid": 0, "gid": 0, "cwd": "/app"}, // 执行上下文（3.5 节）：镜像 ENV → environment，USER 已解析为数字
      "command": ["/entrypoint.sh", "/app/entrypoint.sh"],       // 按 Docker 规则合成的业务命令；null 表示没有业务进程
      "healthcheck": {"test": ["/bin/sh", "-c", "/evaluator/health.sh"], "interval_s": 5, "timeout_s": 5,
                      "retries": 180, "start_period_s": 0, "start_interval_s": 5},
      "restart": "no",
      "mounts": [{"scope": "task",  "key": "binds/0",          "target": "/cve_metadata.yml",   "read_only": true},
                 {"scope": "trial", "key": "secret_file_data", "target": "/run/task-secret", "read_only": false}],
      "privileged": false
    }
  },
  "networks": {"target_network": {"internal": true, "members": ["main", "target"],
                                  "names": {"agent": ["main"], "main": ["main"], "target": ["target"]}}},  // 名字 → 服务（3.7 节）
  "depends_on": {"target": {"secrets_init": {"condition": "service_completed_successfully", "required": true}}},
  "trial_volumes": [{"key": "secret_file_data", "seed": "seeds/secret_file_data.tar"},     // 本 trial 要准备的目录（7.1、7.3 节）
                    {"key": "binds/2", "copy_from": "binds/2"}],                            // 有写者的多引用 bind 源组（4.4 节）
  "task_files": "tasks/<build_key>",                                                       // 相对共享根目录；publish 写入
  "task_files_published": {"host_path": "…", "files_sha256": "…"},                         // 4.7 节
  "runtime_params": [{"service": "model-api-proxy", "var": "UPSTREAM_BASE_URL",
                      "expr": "${CB2TB_API_UPSTREAM:-http://127.0.0.1:9}", "param": "CB2TB_API_UPSTREAM",
                      "default": "http://127.0.0.1:9"}],
  "external_units": ["model-api-proxy"],   // 至少在一个非 internal 网络中的单元（6.4 节）
  "findings": [ /* 非"实现"字段的归类：path、kind（equivalent / warn / reject）、reason（第 13 节） */ ]
}
```

挂载以 `scope` + `key` 表达：`task` 相对已发布的任务文件目录（只读、所有 trial 共用），`trial` 相对本 trial 的目录（prepare 阶段准备）。
share 由编排核心给每个单元统一挂载，不在清单中。清单到编排核心输入（`TrialPlan`）的翻译在 `flotilla.integrations.plan`：
运行时参数代入、agent 叠加都在那里完成，xtuner 与 Harbor 适配共用。

清单是训练侧唯一的任务输入，训练侧不解析 Compose。清单中不含平台凭证。

### 4.6 字段处理表

"实现"与"等效"见 PRD 6.3 节。表中未列出的字段一律按拒绝处理，扫描器报出字段路径。
标"缺口"的项需要平台能力（`Platform_Requirements.md` C14、C16）且首版接口没有对应参数，一律按表中的类别处理，扩展方式见第 17 节。

| 字段 | 类别 | 做法 |
|---|---|---|
| `image`、`build.*`（除 `ssh`、`secrets`） | 实现 | 4.2 节 |
| `command`、`entrypoint`、`working_dir`、`user` | 实现 | 3.5 节 |
| `group_add` | 拒绝（缺口） | 需要 `supplementary_groups`（C14） |
| `environment`、`env_file` | 实现 | 构建时合并；引用未提供的变量时为运行时参数（4.2 节） |
| `healthcheck`、镜像 `HEALTHCHECK` | 实现 / 等效 | 3.6 节；ready 之后不再更新健康状态为等效 |
| `depends_on`（三种条件、`required`、`restart`） | 实现 | 5.3 节 |
| `restart`、`deploy.restart_policy` | 等效 | 3.6 节：不在运行中重启；服务实际退出、Docker 会重启它时样本作废。只写了策略而服务从不退出的不受影响 |
| `stop_grace_period`、`stop_signal` | 等效 | 服务只随 sandbox 删除而停止（3.5 节），只记录 |
| `init` | 实现 | 执行守护进程收割僵尸进程 |
| `tty`、`stdin_open` | 实现 / 拒绝（缺口） | 需要 `pty`（C14） |
| `hostname`、`domainname` | 实现 / 缺口 | 两项能力分开：**名字解析**实现，名字写入其他单元的 hosts（3.7 节）；**自身主机名**是缺口（C14），sandbox 的 `gethostname()` 仍是平台分配的名字。扫描对可能读取自身主机名的服务（集群类数据库、按主机名选角色的应用）告警，任务不拒绝 |
| `extra_hosts` | 实现 | 3.7 节 |
| `networks`（含 `aliases`、`internal`）、`links`、`container_name` | 实现 / 等效 | 3.7 节、第 6 节；hosts 的限制（3.7 节）为等效 |
| `networks.<n>.gw_priority`、`priority` | 等效 | 只影响多网卡时的默认路由；sandbox 只有一块网卡。只记录 |
| 网络的 `ipam` | 等效 / 拒绝 | 空（`ipam: {}`）或只含 `driver: default` 时等效；指定 `subnet` / `ip_range` / `gateway`，或服务的 `ipv4_address` / `ipv6_address` 时拒绝：地址由平台分配（C7），按固定 IP 访问的服务不成立 |
| 网络的 `driver`、`driver_opts` | 按项 | `driver` 只接受 `bridge`（或省略）。`driver_opts` 按键白名单处理：`com.docker.network.bridge.name`、`com.docker.network.driver.mtu` 等只影响宿主侧网桥的键等效；`com.docker.network.bridge.gateway_mode_ipv4/6`：`nat` / `routed` 只改变出站方式，出站另由 6.4 节决定，等效；`isolated`（网桥在宿主上没有地址，容器连不到宿主）等效，flotilla 下没有可连的宿主；`com.docker.network.bridge.enable_icc: "false"`（同网容器互不可达）拒绝，因为拓扑只能表达同网全通；`enable_ip_masquerade: "false"` 拒绝；白名单外的键一律拒绝 |
| `network_mode: none` | 实现 | 不加入任何网络：不在拓扑中、外部策略 `none`，hosts 不含其他服务 |
| `network_mode: service:X`、`container:X`，`pid: service:X`、`ipc: service:X` | 拒绝（缺口） | 需要多容器实例（C16）；该服务与 X 合并为一个 sandbox 的成员容器。合并单元占一个内部地址、按这一个地址加入拓扑（C4）：Compose 只声明共享 PID / IPC 时，合并仍强制两者共享网络 namespace，其网络成员资格取各成员 `networks` 的并集，可能比 Compose 原意多出可达性（例如 `cve-2019-11043` 的 `target` 与 `php`），扫描报告标出 |
| `volumes`：named volume | 实现 | 第 7 节 |
| `volumes`：`volume.nocopy` | 实现 | 不做首次复制（7.3 节） |
| `volumes`：bind mount | 实现 / 等效 / 拒绝 | 4.4 节 |
| `volumes`：tmpfs、`tmpfs` | 等效 | 构建时在镜像中清空该目录，sandbox 启动时为空，与 tmpfs 一致（服务不在原 sandbox 中重启，3.6 节）。容量与内存计费不同，只记录 |
| `ulimits` | 拒绝（缺口） | 需要逐命令 rlimit（C14）；可按数据集配置放行，扫描逐任务报告 |
| `sysctls` | 实现 / 拒绝 | 需要特权运行时，经执行通道在业务启动前设置；没有特权运行时拒绝 |
| `cap_add`、`privileged` | 实现 | 放入特权运行时（C14）；部署没有时拒绝 |
| `cap_drop`、`security_opt` | 拒绝（缺口） | 需要逐命令的能力集控制（C14） |
| `devices` | 实现 / 拒绝 | 只接受部署提供的设备 |
| `read_only` | 拒绝（缺口） / 等效 | 需要 `readonly_rootfs`（C14）。例外：按"服务名 + 入口"列出的、不写任何文件的加固型服务按等效（`compose/normalize.py` 的 `READ_ONLY_EQUIVALENT`；目前仅列出不写文件的推理网关代理） |
| `deploy.resources.limits`、`mem_limit`、`cpus` | 实现 | 原值作为资源限制（C10） |
| `shm_size` | 等效 / 拒绝（缺口） | 不超过平台默认值时等效，否则拒绝 |
| `deploy.replicas`、`scale` | 实现 / 等效 | 每个副本一个单元；hosts 中同名多条，没有轮询（3.7 节） |
| `ports`、`expose` | 等效 | 同网络的服务之间本就全端口可达，经内部 IP 用原端口访问；宿主映射在 sandbox 中无意义，不发布任何端口，只记录 |
| `profiles` | 实现 | 构建时按默认 profile 选择服务 |
| `labels`、`logging`、`platform`（与构建机一致时） | 等效 | 只记录 |
| `secrets`、`configs`（文件来源） | 实现 / 等效 | 按 `:ro` bind 处理（4.4 节） |
| `network_mode: host`、`pid: host`、`ipc: host`、`userns_mode: host` | 拒绝 | 隔离的 sandbox 中不成立 |
| `runtime`、`deploy.resources.reservations.devices`（GPU） | 拒绝 | — |
| `volumes_from` | 实现 / 拒绝 | 被引用的卷都是 named volume 时转为共享卷；否则拒绝 |
| 依赖自己是 PID 1 的入口（s6-overlay、systemd） | 拒绝 | 设计限制（3.2 节） |

### 4.7 任务发布：`flotilla publish`

构建离线、不调用平台；把任务文件放上共享存储需要平台凭证，所以拆成单独一步，由持有凭证的人在能访问平台的机器上执行：

| | 构建 `flotilla build` | 发布 `flotilla publish` |
|---|---|---|
| 运行位置 | 离线构建机 | 能访问平台的机器 |
| 凭证 | 镜像仓库的推送凭证；没有平台凭证 | 平台凭证（环境变量，原则 8） |
| 输入 | Harbor 任务目录 | 构建输出：清单与 `<out>/<build_key>/files/` |
| 输出 | 镜像、清单（`task_files` 待发布）、本地任务文件 | 共享根目录下的 `tasks/<build_key>/`；清单改为已发布 |

发布步骤：

1. 起一个临时锚点（挂载共享根目录，7.2 节），把 `files/` 经文件接口上传到 `tasks/.staging/<build_key>.<uuid>/`，按文件校验 sha256；
2. 在其中恢复属主与权限，写出 `FILES.json`（每个条目的路径、类型、mode、uid/gid、大小与 sha256、符号链接目标；按路径排序），再原子 `rename` 到 `tasks/<build_key>/`；
   目标已存在且 `FILES.json` 一致时跳过，不一致时报错，不覆盖；
3. 在清单中写入 `task_files_published: {"host_path": …, "files_sha256": <FILES.json 的 sha256>}`。

训练侧只接受已发布的清单：provider 在 `open` 时检查 `host_path` 与配置一致，并在首次用到某个 `build_key` 时经锚点核对 `FILES.json` 的 sha256；
不一致以 `invalid` 报告。没有任务文件的任务不需要发布。

---

## 5 trial 生命周期

### 5.1 阶段

```
open        读清单、校验名字、生成 trial_id                              只在本地，不调用平台
  │
prepare     锚点创建本 trial 的共享卷目录、解包种子、复制可写 bind 源          需要共享卷时（7.2、7.3 节）
  │
create      并行创建全部单元：占位入口，初始隔离，外部策略，卷              create；创建受进程级限流
  │ 等待全部 RUNNING
address     取得每个单元的内部地址                                        internal_address
  │
wire        按 Compose 网络生成拓扑，一次 link，平台确认生效后返回（6.2 节）   link（单服务 trial 跳过）
  │
hosts       为每个单元写入 /etc/hosts 块，读回比对（3.7 节）                write_file / read_file
  │
start       按 depends_on 分层启动业务进程，执行健康检查（5.3 节）            start_process 等
  │ 全部满足就绪条件（3.6 节）
ready       交给使用方
  │
release     交给回收器，立即返回（5.5 节）
```

每个阶段有独立超时（默认：create 600s、address 30s、wire 60s（含后端等待平台完成状态）、hosts 30s、start 取 `depends_on` 链上健康检查时长之和加 120s，上限 1800s）。
超时以该阶段的错误报告（5.4 节）。

"初始隔离"是 `create` 的语义（2.3 节）：实例在可执行命令之前就不能与任何其他实例互通（C5），所以本 trial 在 `wire` 之前谁也连不进来，也连不出去（6.3 节）。

没有独立的网络确认阶段：`link` 返回即表示拓扑已生效（C4 第 4 条），`hosts` 的读回比对是 flotilla 对自己写入内容的检查；Compose 的健康检查在 `start` 中按 Compose 语义执行，不兼作网络探测。

### 5.2 标签

每个 sandbox 创建时带 `metadata`：

| 键 | 值 | 用途 |
|---|---|---|
| `flotilla/launch` | `launch_id` | 进程崩溃后的残留清理（5.6 节） |
| `flotilla/trial` | `trial_id` | 创建超时后的对账、回收 |
| `flotilla/unit` | 单元名 | 诊断 |
| `flotilla/task` | 任务 slug（截断到 `max_label_value_len`） | 诊断 |
| `flotilla/role` | `unit` / `anchor` | gc 区分锚点（5.6 节） |

使用方的 `request.labels` 以 `x-` 前缀并入，不能覆盖上述键。键与值按最严格的通行约束生成（Kubernetes 标签规则，值不超过 `max_label_value_len`，C1 要求 ≥ 63）。

过期判定不用自写的标签：以平台查询返回的过期时间为准（C1）。

### 5.3 启动顺序

`depends_on` 构成有向无环图（构建时检查成环）。编排核心按拓扑分层：

- 一层中的单元同时 `start_process`；
- 等待被依赖服务满足条件：`service_started`（进程已启动）、`service_healthy`（健康检查通过，3.6 节）、
  `service_completed_successfully`（退出码 0）；
- `required: false` 的依赖不存在或失败时不阻塞依赖者；但它若在 trial 中，仍要满足就绪条件（3.6 节），失败时 trial 以它自己的错误失败；
- 被依赖服务失败（退出非 0、健康检查用尽、带 restart 策略的服务在启动期退出）时，trial 以 `start` 阶段失败，带上该服务的日志尾部（3.5 节）。

`depends_on.restart: true` 只对**显式**的重启（`docker compose restart` 之类）生效。flotilla 的两个使用方都没有显式重启服务的入口，
所以这个字段在 flotilla 中不引起任何动作，归为实现。

一次性服务（完成后退出的服务）退出后 sandbox 仍然保留到 `release`，与 Docker 中已退出的容器一样；编排核心记下其退出码。
提前删除这些 sandbox 可以减少存活数量，在规模化阶段按实测决定是否实现（第 17 节）。

### 5.4 错误

trial 的错误带 `stage`（`prepare` / `create` / `address` / `wire` / `hosts` / `start` / `run`）、`category`（2.4 节）、
`service`（能定位时）与 `retryable`：

| 情况 | category | retryable |
|---|---|---|
| 平台限流、容量不足 | `rate_limited` / `capacity` | 是 |
| 平台 5xx 用尽重试；`link` 超时（`wire`） | `transient` | 是 |
| 镜像拉取失败 | `image` | 否 |
| 服务启动失败、健康检查失败、一次性服务退出非 0；带 restart 策略的服务退出（3.6 节） | `service` | 否 |
| 清单与部署能力不符（扫描时未发现）；取不到内部地址；`link` 被平台拒绝（例如超出规则数量上限） | `invalid` | 否 |
| ready 之后的基础设施故障（5.7 节） | `lost` | 是 |

provider 内部只对单个单元的 `create` 做一次重试（删除旧实例、重建）。重试发生在 `create` 阶段，此时还没有 `link`，旧实例可以直接删除，
新实例照常进入 `address` 与第一次 `link`，不需要重写任何 hosts。**不做**按集群或放置域的删除重建：可达与放置无关是平台的保证（C4 第 3 条），
flotilla 不在 trial 中检测它。`start` 阶段的失败属于任务本身，不重试。重试用尽后按上表报告；flotilla 不重来整个 trial，xtuner 也不重来，样本失败。

失败时已经创建的 sandbox 全部交给回收器，清理失败不覆盖原始错误（PRD F6）。

### 5.5 回收器

每个 flotilla 实例（一个 `launch_id`）一个 `Reaper`。它按 trial 记账：已知实例 ID、进行中的创建、共享卷键。

**创建登记**：编排核心的每个 `create` 调用都作为 Reaper 名下的任务执行，而不是调用方的任务：

- 发请求前登记到该 trial 的"进行中的创建"；
- 调用方被取消时，这个任务不被取消，响应（成功或失败）照常送达 Reaper，拿到的实例 ID 记入该 trial；
- 调用方只 `await` 该任务的结果（经 `asyncio.shield`）。

**释放**：

- `release` 把 trial 标记为待回收后立即返回，不 await 任何平台调用；此后不再为该 trial 发出新的创建请求（包括 5.4 节的单元重建）；
- 该 trial 的所有进行中的创建结束后（结果未知的创建按 2.4 节对账到 `list_visibility_s` 为止才算结束），Reaper 删除记下的全部实例（不先撤销互联，2.3 节）；
  只有本 trial 有过**失败**的创建时，才另按 `flotilla/launch` + `flotilla/trial` 标签列出并删除列出的实例：超时、连接中断、5xx 的结果未知（2.4 节），
  而 `capacity` / `image` 也可能发生在平台接受请求之后（调度超时、拉取失败），实例已经存在。全部创建都返回了实例 ID 时，
  记下的实例就是全部，不需要列出：正常释放的 trial 不产生列表调用（第 12 节）；
- 后台以受限并发（默认 16）删除，`not_found` 视为成功，失败按指数退避重试，最长 30 分钟；
- **删尽的判定**：记下的实例都已确认删除（`get` 返回 `not_found`）；有过失败的创建时另需：本 trial 最后一个失败的创建调用**结束**后 `list_visibility_s` 之内，每轮删除之后再列出一次，新出现的实例同样删除，
  超过该时限且最近一次列出为空。时限从调用结束起算：C1 的时限从请求到达平台起算，而后端可能排队或退避重试，到达时刻只知道不晚于调用结束；
- 删尽之后，才经锚点删除共享卷目录（7.4 节），不会删掉仍被挂载的卷，也不会删掉晚出现的实例所挂的目录；
  平台的"确认删除"不一定意味着实例内的进程已终止，个别写入可能在目录删除之后留下残留；残留位于本 launch 的目录下，launch 结束后由 gc 删除，不影响正确性；
- 实例关闭时（`close`，Harbor 适配与命令行调用；xtuner 不调用，见第 10 节）等待队列清空，最多 `close_timeout`（默认 20s）；队列在时限内清空（本 launch 的实例都已确认删除）时，先经锚点删除 `launches/<launch_id>` 目录，再删除锚点；
  否则只删除锚点，剩余部分由其他进程的 gc 处理（5.6 节）。

### 5.6 锚点与残留清理：`flotilla gc`

判断"一个 launch 的资源能否删除"只看该 launch 的进程是否还活着，而不看实例的期限。

**锚点**：每个 flotilla 实例启动时创建一个锚点 sandbox（标签 `flotilla/launch=<launch_id>`、`flotilla/role=anchor`，小规格），
TTL 较短（默认 10 分钟，须满足平台的 TTL 下限），由 Reaper 每 30 秒续期一次。进程活着 ⇔ 锚点存在。
锚点出现在按标签列出的结果中之后，本实例才创建 `launches/<launch_id>` 目录，建好目录之后才创建本 launch 的任何实例，所以 gc 看到的该 launch 的资源都晚于它的锚点，且有实例的 launch 一定有目录。
锚点同时执行共享存储上的目录操作（7.2 节）。

两个角色都需要：flotilla 不要求平台提供卷删除（`Platform_Requirements.md` 第 5 节），删除目录、解包种子、复制 bind 源都需要一个挂载共享根目录的 sandbox；
存活判定只依赖 C1。

**gc**（provider 的 `start` 钩子中执行，此后每 10 分钟执行一次；也可用命令行 `flotilla gc`，见本节末尾）：

1. 列出本部署中所有 `flotilla/role=anchor` 的实例（按 metadata 过滤、翻页取全），得到存活的 launch 集合；
2. 之后经本实例的锚点列出 `launches/` 下的目录，launch 不在存活集合中的，记为候选。有实例的 launch 一定有目录（见上），所以不需要列出全部实例；
   gc 的调用量与残留的 launch 数成正比，与部署中的实例总数无关；
3. 候选需要在相隔至少 `W`（默认 3 分钟）的两次 gc 观察中都没有锚点才删除；所以 provider `start` 时的那次 gc 只登记候选，残留在下一次 gc（`gc_interval` 之后）才删除；
4. 按 `flotilla/launch=<id>` 列出候选 launch 的实例并全部删除；
   确认删除、且再列出为空后，经本实例的锚点删除 `launches/<launch_id>` 目录（7.4 节）。目录是 gc 找到这个 launch 的唯一入口，所以总是最后删除。

目录与实例都只在第二次观察之后删除，这一点靠下面的时间关系成立。设该 launch 最后一次续期成功在 `t0`：该进程在 `t0 + G` 之后不再发出任何创建请求与目录操作（见"锚点丢失"），
它此前发出的创建在 `t0 + G + list_visibility_s` 之前都已出现在列表中或永远不会出现（C1 第 3 条）；gc 第一次观察到无锚点不早于 `t0`，
第二次观察不早于 `t0 + W`。所以只要 `W > G + list_visibility_s`，第二次观察之后列出的实例已经完整，删目录时不会有该 launch 的新实例或新目录出现。

**锚点丢失**：Reaper 续期失败时重试，不重建。本实例从最后一次续期成功起计时，超过宽限期 `G`（默认 90 秒）仍未续期成功时，本 `launch_id` **永久停用**。
停用按时间判定，不等待 Reaper 发现：每个创建请求与目录操作发出前检查"距最后一次续期成功不足 `G`"，不满足即不发出，按 `lost` 报告；
停用时仍在途的创建与目录操作被取消（后端在调用内的排队与重试随之停止），所以 `t0 + G` 之后不会再有请求到达平台。停用时：

- 所有进行中的 trial 标记为 `lost`（5.7 节），交给回收器；
- 生成新的 `launch_id`，创建新锚点后才开启新 trial；旧 launch 的实例与目录由 gc 清理。

不复用旧 ID：gc 判定"无锚点"的观察可能早于本实例发现锚点丢失，复用旧 ID 会与已经开始的删除并发。
安全条件 `G + list_visibility_s < W`，续期间隔明显短于 `G`；配置不满足时 provider `start` 报错（`list_visibility_s` 取自能力报告）。

**平台 TTL** 仍是最后一道保障：每个实例的 TTL 有限，为 trial 预计时长的 1.5 倍（默认上限 6 小时，且不超过部署的 `max_ttl_seconds`），
长 trial 由编排核心在剩余 30 分钟时续期（`renew`，部署的 TTL 上限较短时提前量取 TTL 的一半）；续期失败按退避重试，剩余 TTL 不足 2 分钟仍未成功才按 5.7 节标记 `lost`。
**命令行 `flotilla gc`**：一次性进程没有常驻锚点，像 `flotilla publish` 一样起一个临时锚点（4.7 节）执行第 2、4 步的目录操作，结束时删除；
在一次运行内做两次相隔 `W` 的观察，所以一次运行至少耗时 `W`。`--launch <id>` 只处理指定 launch，跳过第 3 步的等待，由操作者确认该 launch 的进程已经退出；
第 1 步列出时该 launch 的锚点仍存在则拒绝执行（锚点在进程退出后至多一个锚点 TTL 内过期）。

**临时锚点**（`flotilla publish`、`flotilla share publish`、命令行 `flotilla gc`）：与常驻锚点相同的镜像与标签（`flotilla/role=anchor`），使用自己新生成的 `launch_id`，
TTL 同样较短并由命令续期，命令结束时删除；它不创建 `launches/<launch_id>` 目录，也不创建其他实例，所以不会成为 gc 的候选，命令崩溃后由平台 TTL 回收。

### 5.7 运行期故障

ready 之后、`release` 之前，基础设施仍可能失败；这时 agent 或验证可能照常返回，故障被当成任务结果写进 reward（违反 PRD T2）。
以下信号任一出现，trial 即标记为 `lost`：

| 信号 | 来源 |
|---|---|
| 某个单元的 sandbox 不存在或状态不再是 `RUNNING`（含 `UNKNOWN`，2.3 节） | 每个单元每 5 分钟一次 `get`（错开发出；查询本身的瞬时错误不算）；执行调用返回 `not_found` 时立即 `get` 确认 |
| 某个单元的执行通道持续不可用（非业务进程的错误） | 执行调用失败并重试用尽 |
| 某个业务进程查询不到（执行通道重启后丢失了进程记录） | `check()` 中的 `process_status` |
| 续期失败 | 编排核心 |
| 本 launch 停用 | Reaper（5.6 节） |

运行期不按标签列出：平台的列表开销可能与部署内实例总数成正比，列表只用于对账、回收与 gc（第 12 节）。

**不算**基础设施故障的：业务进程退出、崩溃、健康检查失败——这些是任务内的行为。其中带 restart 策略的服务退出会使样本作废（3.6 节），但类别是 `service`，不是 `lost`。

`lost` 通过两个途径暴露给使用方：

- 之后对该 trial 的 `get` 与执行调用立即抛 `stage="run"`、`category="lost"`、`retryable=True` 的错误；
- 环境的 `check()`（`XTuner_Environment_Design.md` 5.6 节）返回该错误；xtuner 在验证之后、接受分数之前调用它，有错误则样本丢弃。

**`check()`** 在返回"健康"之前做一次有时限的确认（默认 10 秒），任一不满足即返回错误，**超时或状态未知按 `lost`**，不把未知当作健康：

1. 对每个单元 `get` 一次，实例都在且 `status` 为 `RUNNING`；
2. 对每个单元经执行通道执行一次空命令，成功返回；
3. 对每个业务进程查一次 `process_status`：查询不到为 `lost`；带 restart 策略的服务已退出为 `service`（3.6 节）；
4. 最近一次续期没有失败，本 launch 未停用。

### 5.8 控制面调用量

一个 3 单元的 trial，从创建到就绪的平台调用：创建 3、状态查询约 3–6（有退避）、取地址 3（`internal_address = "api"` 时并入状态查询）、
`link` 1（后端落到平台上的调用数见后端文档，OpenSandbox 为每成员一次）、hosts 写入与读回 6、业务启动 3、健康检查按各服务的间隔执行至 healthy、删除 3 与删除确认的 `get` 3；创建结果都明确时释放不列出（5.5 节）。
运行期：每单元每 5 分钟一次 `get`、续期；`check()` 时每单元一次 `get`、一次执行与每个业务进程一次 `process_status`；另有 agent harness 对 detached 进程的存活轮询（`is_pid_running` → `process_status`，默认每进程约 1 次/分钟，第 12 节）。按标签列出只出现在结果未知的创建的对账与释放（5.5 节）、gc 与锚点创建时的可见性确认中。每进程固定开销：锚点续期每 30 秒一次；gc 每 10 分钟一次锚点列出与一次目录列出（经锚点执行），另每个候选 launch 一次按标签列出。总量估算见第 12 节。

---

## 6 网络

flotilla 只在语义层面描述网络：把 Compose 的网络关系编译为一组成员之间的**拓扑**（`Topology`），为每个单元确定**外部策略**（`ExternalPolicy`），
分别经 `link` 与 `InstanceSpec.external`（2.3 节）交给平台；两者在一个 trial 中都只设置一次。规则怎么写、对端怎么表达、网段怎么挖除，都在后端里（OpenSandbox 见 `backends/opensandbox.md` 第 4、5 节）。

| flotilla 负责 | 平台负责（`Platform_Requirements.md`） |
|---|---|
| Compose 网络 → 拓扑（6.2 节）；按单元确定外部策略（6.4 节） | 让拓扑成立：可达、不可达、初始隔离、返回即生效（C4） |
| 按实际地址为每个单元写入 hosts（3.7 节） | 提供组内互访用的地址（C7） |
| `link` 返回、hosts 写入之后才按 `depends_on` 启动业务（5.1 节） | 组外实例与外部主机不能连入（C5） |
| — | 按外部策略放行对外访问（C6）；声明自身基础设施的隐式放行（C13） |

平台的网络保证由部署接入或升级时的契约测试验证（第 14 节），结果写入能力报告，不在每个 trial 的路径上探测。

### 6.1 与放置无关

组内可达性与实例被调度到哪个节点、集群、资源池无关（C4 第 3 条）。flotilla 不选择放置，不从实例 ID 解析集群，不在创建时带放置提示，
不因放置不一致删除重建，也不经入口隧道补足互访。部署不满足时，能力报告中 `link = False`，扫描器在该部署上拒绝多服务任务，
单服务任务不受影响。各部署的现状见后端文档第 2 节。

### 6.2 trial 内：Compose 网络 → 拓扑

| Compose 语义 | flotilla 的做法 |
|---|---|
| 同一网络中的服务可以用任意 TCP / UDP 端口互访 | 每个 Compose 网络是拓扑中的一个网络，成员是连到它的单元；平台保证同网成员任意端口可达（C4 第 1、2 条） |
| 至少共享一个网络即可达；不共享任何网络即不可达 | 直接由拓扑表达，不需要 flotilla 生成逐对规则 |
| 默认网络：未声明 `networks` 的服务都在 `default` 中 | 规范化后 `default` 是普通网络 |
| 名字按网络可见 | `/etc/hosts` 只含共享网络上的名字（3.7 节） |
| 服务端口经原端口访问 | 使用 `internal_address` 与服务原有的端口；不发布任何端口 |

- `link` 的成员是本 trial 中至少在一个网络里的单元（2.3 节）；成员少于两个的 trial 不调用 `link`，初始隔离即最终状态；
- `request.sandboxes` 中的独立 sandbox 不在任何拓扑中，只有外部策略；
- 使用 UDP 的任务需要 `link_udp`（C4 第 2 条，功能类）：部署没有 `link_udp` 时，清单中声明了 UDP 端口（`ports` / `expose` 的 `/udp`）的任务在扫描时拒绝；
  未声明协议的服务无法从 Compose 可靠判断，扫描报告告警；
- 成员数超过 `link_max_members` 的任务在扫描时拒绝。

### 6.3 trial 外：入站隔离

其他 trial 的实例、平台上的其他实例与平台外部的主机不能连入本 trial 的单元（C5）。flotilla 这边的保证只有两条：
不发布任何端口；控制面只经平台的执行通道（3.8 节）。其余由平台负责：`link` 语义本身要求成员与组外实例不可达（2.3 节），
而"部署中未经 flotilla 创建的实例是否能连入"只能由部署保证，由部署者声明、写入能力报告的 `inbound_isolation`，不满足时按安全类处理（第 2 节）。
地址复用窗口是 C5 的已知例外（2.3 节）。

### 6.4 出站

每个单元的出站由两部分决定：

1. **Compose 的 `internal`**：只在 `internal: true` 网络中的单元，外部策略为 `none`（不能访问推理网关在内的任何外部目标）；
2. **调用方的外部策略**：至少在一个非 `internal` 网络中的单元（清单 `external.units`，4.5 节）按 provider 配置 `[network] external`（16.3 节）获得外部策略。

`[network] external` 有两种形式：

- **任意外网：`"any"`**。与 Compose 普通网络一致：能访问任意外部地址与域名，但**不因此**能访问组外的实例与平台自身的基础设施（C6 第 2 条，6.5 节）。
  部署的 `external_forms` 不含 `any` 时，provider `start` 拒绝这一配置；
  推理网关等运行时参数指向的地址若落在平台基础设施中（例如集群内的 service），`any` 下不可达，provider `start` 报错，改用窄列表；
- **窄列表：`[...]`**（例如只放行推理网关）→ `allowlist`。列表中不能出现 `0.0.0.0/0` 这类表达任意外网的写法，provider `start` 拒绝并提示改用 `"any"`；
  列表中的目标不能是 sandbox 地址（域名解析到哪里由部署者保证）。
  这是调用方为训练施加的出站策略，**不等同于 Compose 的普通网络**：扫描报告与 trial 元数据把它记为"出站受调用方策略限制"，不归为等效，
  依赖任意外网访问的任务在这种配置下的结果需要单独判断。

例如 CVE 类任务中 `main` 只在 internal 网络里，经同网的 `model-api-proxy` 访问推理网关：外部策略给的是 `model-api-proxy`，`main` 是 `none`。

`request.sandboxes` 中的独立 sandbox 按 `[network] external`。锚点不运行任务代码，外部策略固定为 `none`，不参与任何 `link`。

`[network] external` 对所有非 `internal` 单元相同，不按服务区分。这是有意的取舍：Compose 本身不区分同一普通网络中服务的出站，按服务区分需要逐任务配置；
代价是窄列表中的目标（例如推理网关）对任务中所有非 `internal` 服务可达，而不只是负责访问它的服务。扫描报告列出每个任务中获得外部策略的服务。

flotilla 不生成平台基础设施（DNS、元数据等）的例外，也没有对应的配置项（6.5 节）。

### 6.5 生效与平台基础设施访问

**生效**是平台契约，不由 flotilla 探测补偿：

| 时机 | 平台保证 | flotilla 的做法 |
|---|---|---|
| 创建 | 实例可执行命令之前已处于初始隔离，外部按 `spec.external`（C5 第 1 条） | `create` 返回即视为已生效 |
| `link` | 返回时拓扑已对新连接生效（C4 第 4 条） | 返回即进入下一阶段；失败以 `wire` 阶段报告，类别按 5.4 节 |

平台只能异步应用时，由后端等待平台的完成状态；没有完成状态的部署 `link = False`，不以固定等待、临时监听或连通探测替代。
hosts 写入后读回比对（3.7 节）、Compose 健康检查（3.6 节）都不是网络探测。

**平台基础设施访问**（DNS、元数据、镜像拉取等）由平台保证，不经 flotilla 的策略表达（C13）。删除 flotilla 的配置不会消除平台自己的放行，
所以平台为此放行了什么、影响哪些业务语义，是部署声明的一部分，写入能力报告的 `implicit_egress`：

- `none`（含 `internal`）的实际范围 = 无 + `implicit_egress`；扫描报告据此标出比 Docker 多出的访问；
- `any` 不含平台自身的基础设施（节点、编排系统 API、平台控制面、云元数据），C13 的隐式放行除外（C6 第 2 条）；后端负责把它们排除（OpenSandbox 经部署配置的平台网段），编排核心不知道这些地址；
- `implicit_egress` 只用于报告与扫描判断，flotilla 不据此生成规则。

### 6.6 Harbor `network_policy`

Harbor 适配的首版设计只支持 `public`；`no-network`、`allowlist` 与阶段间策略切换暂不支持，按能力拒绝相应任务。
训练侧从 Compose 的 `internal` 网络与 `[network] external` 构造策略，不经 Harbor 的策略配置。
扩展条件见第 17 节。Harbor 适配尚未实现。

---

## 7 卷

### 7.1 分类

按**挂载引用**（每个 `volumes` 条目是一个引用：单元、挂载点、读写模式）分类，而不是按引用它的单元数：同一单元把一个卷挂到两个位置，
或一处读写、一处 `:ro`，两处都要看到同一份数据，本地目录做不到。

| 卷 | 引用 | 做法 |
|---|---|---|
| named volume | 全部引用只读 | 共享存储中按 `build_key` 的不可变目录，只读挂载，所有 trial 共用（7.3 节） |
| named volume | 恰好 1 个引用，读写 | 该单元的本地磁盘，路径即挂载点；随 sandbox 删除 |
| named volume | 其余情况（≥ 2 个引用且至少一个读写，无论是否在同一单元） | 共享存储（C8）中本 trial 的子目录，每个引用一个 `SharedVolume`，按自己的读写模式挂载 |
| tmpfs | — | 本地目录，每次启动业务进程前清空（等效，4.6 节） |
| bind mount | — | 4.4 节 |
| `external: true` 的卷 | — | 拒绝（训练中不存在跨 trial 的外部卷） |

### 7.2 共享存储

共享存储是"共享根目录 + 相对子路径"（C8）。flotilla 只说"把哪个子路径挂到哪里、是否只读"（`SharedVolume`，2.3 节）：

- `key` 是相对共享根目录的路径，不含 `..`；锚点的 `key` 为空，表示共享根目录本身；
- 共享根目录由部署配置给出（16.3 节）；挂载、授权、跨节点共享、节点可用性与性能由平台负责；子目录的创建、初始化与清理由 flotilla 经锚点负责；
- `SharedVolume` 怎么落到平台的卷形式上是后端的事（OpenSandbox 见 `backends/opensandbox.md` 第 6 节）。

路径布局（均相对共享根目录）：

| 目录 | 由谁创建 | 时机 |
|---|---|---|
| 共享根目录本身 | 部署者 | 部署前置条件；provider `start` 时经锚点检查 |
| `share/releases/<release>` | `flotilla share publish`（经临时锚点） | 发布时；provider `start` 时检查并校验（3.3 节） |
| `tasks/<build_key>/…` | `flotilla publish`（经临时锚点，4.7 节） | 构建之后、训练之前 |
| `launches/<launch_id>` | 本实例的锚点 | provider `start`，第一个 trial 之前 |
| `launches/<launch_id>/<trial_id>/<volume>`、`launches/<launch_id>/<trial_id>/binds/<n>` | 本实例的锚点 | trial 的 `prepare` 阶段 |

**目录在创建前准备好**：flotilla 在所有部署上统一由锚点预先创建子目录，不依赖平台自动创建；子路径不存在时平台应当明确报错或自动创建，
不能无限期等待（C8 第 4 条，降级：缺失时 flotilla 仍正确，只是诊断变差）。

**锚点**：flotilla 自带的小镜像，以读写方式挂载共享根目录（不是整个项目目录），只运行 flotilla 的代码，不运行任务代码。
不需要共享卷的 trial 不经过锚点。锚点续期失败、尚在宽限期 `G` 之内时，需要共享卷的 trial 等待锚点，超时以 `prepare` 阶段、`transient` 类别报告；超过 `G` 后按 5.6 节停用本 launch，进行中的 trial 按 `lost` 报告。

**不暴露项目目录**：单元只挂自己需要的子目录（share、自己引用的任务文件、本 trial 的卷），从不挂共享根目录或整个项目目录。

没有初始内容的卷，锚点在 `mkdir` 时按清单的 `owner` `chown`（要求 C8 第 2 条的属主保留）。

### 7.3 初始内容

Docker 的语义：空的 named volume 第一次挂载到容器时，复制镜像中该路径的原有内容（`volume.nocopy` 时不复制）。

- 构建时（4.2 节第 6 步）把来源服务镜像中挂载点的内容打包为 `seeds/<volume>.tar`，保留属主与权限；来源按 Docker 的规则确定：第一个在挂载点有内容的引用服务；
- `prepare` 阶段锚点把 tar 解包到 `launches/<launch>/<trial>/<volume>`，完成后才创建引用它的单元，服务启动时看到的就是完整的初始内容；
  不需要 sandbox 内的任何操作，也不需要单元之间的协调；
- 所有引用都只读的卷：内容解包到 `tasks/<build_key>/_volumes/<volume>/`（任务发布时写入），每个引用单元以 `readOnly` 挂载，只读由平台保证，同一任务的所有 trial 共用；
- 本地卷：挂载点就是镜像原路径，内容天然存在；`nocopy: true` 时构建时清空该单元镜像中挂载点下的原有内容（保留目录本身与属主、权限）。

### 7.4 回收

标准接口没有卷删除，目录都经锚点删除：

1. trial 结束时，回收器先确认该 trial 的实例全部删除（5.5 节），再经本实例锚点删除 `launches/<launch_id>/<trial_id>`；
2. 其他 launch 的残留由任一存活进程的 gc 经自己的锚点删除 `launches/<launch_id>`（5.6 节）。

卷只存在于本 trial 的子目录中，残留不会被其他 trial 挂载。

**不自动挂载**：平台不向实例自动挂载任何未声明的可写目录（C8 第 5 条，安全类，`no_auto_mount`）；否则不可信代码能改写 share 与其他 trial 的卷（3.3 节）。
flotilla 不以"声明了卷所以不会被自动挂载"这类平台现象作为设计前提。

---

## 8 平台后端

`Platform` 的实现放在 `platform/<后端>/` 下（16.1 节），每个后端一份文档，给出 `Platform_Requirements.md` C1–C16 的实现方式、各部署的现状与缺口、
向平台提出的要求和探测记录。目前只有一个后端：

| 后端 | 文档 | 部署 |
|---|---|---|
| `OpenSandboxPlatform` | `backends/opensandbox.md` | OpenSandbox 兼容部署（差异由部署配置描述） |

后端不按部署名分支。标准接口表达不了、而某个部署另有做法的地方，后端内提供按配置选择的实现策略（例如 OpenSandbox 的组内互联 `network.link`），
每种策略都通过同一套契约测试；编排核心不知道策略的存在（16.2 节）。

以下是与后端无关、由编排核心决定的输入：

### 8.1 资源

| Compose | `Resources` |
|---|---|
| `deploy.resources.limits.cpus` / `cpus` | `cpu` = 原值 |
| `deploy.resources.limits.memory` / `mem_limit` | `memory` = 原值 |
| 未声明 | 按服务类型的默认值（第 12 节），可逐任务覆盖 |

flotilla 不做换算：内存上限须等于原值，CPU 上限不低于原值、实际倍数记入能力报告与 trial 元数据（C10）；磁盘按部署默认配额。

### 8.2 TTL

每个实例的 `timeout_seconds` 总是有限值，不超过 `max_ttl_seconds`；长 trial 与锚点靠续期（5.5、5.6 节）。回收器、锚点与 gc 照常保留，平台 TTL 是最后一道保障。

### 8.3 凭证与数据可见性

- 平台凭证只从训练进程的环境变量读取，请求日志中脱敏；不写入 `env`、标签、清单（PRD N4）；
- 业务环境变量（可能含 Compose 声明的口令）不放进 `InstanceSpec.env`，只随执行请求传入（执行上下文，3.5 节）：平台查询接口可能原样返回创建参数；
  执行请求是否被平台记录、对谁可见，由各后端文档说明。

---

## 9 其他平台

E2B 等其他平台若要接入，是另一个独立的后端：实现同一个 `Platform` 接口，按 `Platform_Requirements.md` 逐项给出 C1–C16 的映射，
写一份新的后端文档（`backends/<平台>.md`），通过同一套契约测试（第 14 节）；不满足的能力按类别处理（第 2 节），不另立一套需求，
也不提供入口隧道之类的补足机制。

---

## 10 xtuner 适配：`FlotillaProvider`

实现 `XTuner_Environment_Design.md` 第 5 节的接口，不 import xtuner。xtuner 对同一配置、同一事件循环共享一个 provider 实例（该文档第 6 节）；
同一进程中可能因配置或事件循环不同而有多个实例，每个有自己的 `launch_id`、锚点与 `Reaper`，互不干扰：gc 只按"锚点已不存在"判定残留，不会删除同进程中另一个实例的 sandbox。
xtuner 没有启停钩子：下表的初始化在第一次 `open` 时执行，进程退出时 xtuner 不调用 `close`，锚点随 TTL 过期，残留由其他进程的 gc 处理（5.6 节）。

| 接口 | 行为 |
|---|---|
| 初始化（第一次 `open`） | 加载部署配置与能力报告（2.2 节），生成 `launch_id`，启动 `Reaper` 与锚点，经锚点检查共享根目录并校验钉住的 share 发布（3.3 节），创建 `launches/<launch_id>` 目录，执行一次 gc（只登记候选，5.6 节）；并发的首次 `open` 只初始化一次，失败时每次 `open` 都报该错误。下文的"provider `start`"指这一步 |
| `open(request)` | 读 `request.task["manifest"]` 指向的清单（进程内按路径缓存）；`names` = 清单中的服务名 ∪ `request.sandboxes` 的键，重名报错；检查运行时参数与 agent 叠加；不调用平台 |
| `get(name)` | 第一次对任一服务名调用时启动整个 trial（第 5 节），同一 trial 并发的 `get` 共享一次启动；`request.sandboxes` 中的名字各自独立创建，不接入 Compose 网络 |
| 客户端 | 实现 xtuner 的 sandbox 客户端接口，由本适配层基于 `Platform` 实现（下表），不 import xtuner；每次执行从该服务的执行上下文构造 `ProcessSpec`（3.5 节）。平台层不知道 xtuner 的接口 |
| `info(name)` | `env_id`（sandbox id）、`url`（None，不发布端口）、`image`、`workspace_path`（服务 `working_dir`）、`metadata`（各阶段耗时、重建次数、出站是否受调用方策略限制） |
| `check()` | 5.7 节：已标记 `lost` 时直接返回该错误；否则做一次有时限的确认，超时或未知按 `lost`；健康时返回 `None` |
| `release()` | 交给回收器，立即返回；幂等 |
| 事件循环 | 锚点续期、回收、运行期监视都是后台任务。共卡训练只在 `produce_batch` 期间运行事件循环，训练步之间这些任务暂停；为不让锚点超过宽限期 `G` 而停用 launch（5.6 节），它们运行在 provider 自己的线程与事件循环中，客户端调用经 `run_coroutine_threadsafe` 转入 |

**客户端方法到 `Platform` 的映射**（签名取自 `upstream/main` 的 `sandbox.py`，`DetachedShellEntry` 与 pool 对它的调用）：

| 客户端方法 | 映射 |
|---|---|
| `execute(command, cwd, timeout_sec, detach) -> dict` | `command` 是 shell 字符串，合成 `["/bin/sh","-c",command]`（3.5 节）。`detach=False` → `exec`，返回 `{"stdout","stderr","return_code"}`；`detach=True` → `start_process`，返回 `{"stdout":"","stderr":"","return_code":0,"pid":<handle>}`，`handle` 是本地句柄 |
| `is_pid_running(handle) -> bool` | 句柄映射到 `start_process` 返回的进程 ID，查 `process_status`：`running` 为真；`found=False`（execd 重启丢失记录）按 5.7 节使 `check()` 报 `lost` |
| `health_check() -> {"ok": bool}` | 一次空 `exec` 成功即 `{"ok": True}`，失败 `{"ok": False, "error": …}`（pool 健康轮询与 `_sandbox_alive` 依赖它） |
| `upload_bytes(path, data)` / `download_file(path) -> bytes` | `write_file` / `read_file`；上传避开 multipart（后端 3.6 节） |
| `aclose()` | 关闭本客户端持有的连接，不删除实例（删除由回收器负责） |

- `request.env_vars` 并入每个名字（Compose 服务与 `request.sandboxes`）的客户端执行（3.5 节），与 xtuner 接口的约定一致；不进入 Compose 服务的业务进程与健康检查，不改变任务自身的环境；`request.sandboxes` 中的独立 sandbox 没有业务进程，只有客户端执行；
- 抛出的错误带 `stage` / `category` / `retryable`（5.4 节），由 xtuner 按属性识别；
- 进程级限流：创建速率、并发创建数、并发 trial 数、执行调用速率（`exec_rate`），均可配置；
- 完整配置与启动校验见 16.3 节。

**运行时参数**（`runtime_params`）：按清单中 `param` 的名字给值，对所有任务生效，例如 `{"CB2TB_API_UPSTREAM": "https://<推理网关>/v1"}`；
provider 在启动业务进程时按清单把值代入原表达式，写入声明它的服务的环境变量。参数指向的地址需要能从 sandbox 访问：窄列表时出现在 `[network] external` 中，`"any"` 时不落在平台基础设施中（6.4 节），provider 在 `start` 时检查。

**agent 叠加**（`agent_overlay`）：agent 的运行时与 harness 在 share 中（3.3 节）。叠加只剩进程级的环境变量：

```python
agent_overlay = {
    "env": {"PATH": "/.flotilla/runtime/node/node-v22.14.0/bin:${PATH}",
            "SANDBOX_RUNTIME_DIR": "/.flotilla/runtime"},   # 进程级；逐轨迹的变量仍走 request.env_vars
    "services": None,                                # 叠加到哪些服务：默认只有清单的 agent_service
}
```

- 不提供追加挂载：与任务无关的文件进 share，与任务相关的文件由配方经执行通道上传；
- `env` 与服务自身的环境变量同名时，叠加覆盖；`${VAR}` 引用服务原有的值；同样作用于 `request.sandboxes`。

---

## 11 Harbor 适配

实现 Harbor 0.23 `BaseEnvironment`：

| 方法 / 能力 | 行为 |
|---|---|
| `start` | 启动 trial（第 5 节） |
| `exec` / `upload_*` / `download_*` | 默认作用于 agent 服务；指定服务时作用于该服务，服务不存在时报错 |
| `stop` | 整个环境结束：交给回收器（5.5 节） |
| `capabilities` | `docker_compose = True`；`disable_internet`、`network_allowlist*`、`dynamic_network_policy` 均为 `False` |
| `stop_service(name)` | 报错：首版不支持（见下） |

Harbor 0.23 按 `capabilities` 在 trial 开始前拒绝 `no-network`、`allowlist` 与阶段之间切换网络策略的任务（6.6 节），flotilla 不需要自己检查。

`stop_service("main")` 只在 Harbor 的独立验证模式（`environment_mode = "separate"`）下、收集 `main` 以外服务的产物之前调用，要求 `main` 内的全部进程（含 agent 经 `exec` 留下的后台进程）都停止。
现有部署都给不出这个保证（平台没有"停止实例内全部进程"，删除也不能确认进程已终止），所以首版不支持：
扫描器拒绝"独立验证模式且有 `main` 以外服务的产物或收集钩子"的任务，`stop_service` 被调用时报错。Harbor 对这个错误只记 warning，所以拒绝必须在扫描时完成。扩展见第 17 节。

独立验证模式中，收集 sidecar 产物或运行收集钩子可能要求 `stop_service`。首版适配设计拒绝依赖该能力的任务；扩展条件见第 17 节。

Harbor 的清单来源与训练相同：`flotilla build` 的输出目录。适配层不修改 Harbor。

---

## 12 规模

设并发 trial 数为 `T`、平均单元数为 `S`、平均 trial 时长为 `D` 秒、provider 实例数为 `P`。
这些量由使用方和部署决定，以下为容量估算模型，尚未代表任何规模上的实测结果。

| 量 | 估计 |
|---|---|
| 存活 sandbox | `T × S + P`，含每个 provider 的锚点 |
| 稳态创建与删除速率 | 约 `T × S / D`，另加失败重建与清理 |
| 运行期存活查询 | `T × S / 查询间隔秒数`，另加启动期轮询 |
| `link` 调用 | 每个多服务 trial 在 `wire` 阶段一次；单服务为零 |
| 启动期执行 | 每单元取地址、写入并核对 hosts、启动和健康检查 |
| 运行期执行 | `check()` 与 agent harness 的后台进程轮询；前台长执行另占用 HTTP 连接 |
| 按标签列出 | 只在结果未知的创建与 gc 中使用；正常 trial 不调用 |
| 创建并发 | provider 数量乘以 `limits.create_concurrency`，应低于部署上限 |
| 执行速率 | provider 数量乘以 `limits.exec_rate`，应低于部署容量 |

列表可能随部署内实例总数增长，故存活确认使用按 ID 查询（5.7 节）。
在目标负载的切片上测每 trial 的调用量、资源峰值、列表延迟与回收速率，再设置限流、进程数与资源默认值。
预热、批量操作、亲和调度和一次性服务提前回收按实测决定，不作为当前版本的性能承诺。

---

## 13 扫描器：`flotilla scan`

- 输入：任务目录或清单，以及目标部署的能力报告；输出：每个任务一行 JSON（状态、每个非"实现"字段的类别与原因）与汇总表；
- 与构建共用解析与归类代码（4.6 节），保证"能构建"与"扫描通过"一致；
- 与部署相关的归类（多服务互联、特权、设备、UDP、文件 bind、执行鉴权、入站隔离、平台隐式放行对 `internal` 的影响）按能力报告判断，同一任务在不同部署上可能得到不同结论；
- 拒绝声明 Harbor `network_policy`（`public` 以外）的任务，以及会触发 `stop_service` 的独立验证模式任务（6.6 节、第 11 节）；
- 报告列出每个任务的运行时参数、需要特权运行时的原因、hosts 的限制是否可能影响该任务（服务使用自带 DNS 客户端、依赖轮询）、出站是否受调用方策略限制；
- 检查挂载点冲突：服务的卷、bind、`working_dir` 落在 `/.flotilla` 之下，或镜像中该路径已有内容时，拒绝；
- 统计带 restart 策略的服务（3.6 节）；
- 训练数据集接入前全量扫描，拒绝的任务不进入训练。

---

## 14 测试

契约测试只有一套，针对 `Platform` 协议与 `Platform_Requirements.md` 的验收，**按部署分别运行**；不按部署复制测试，也不为某个部署写专用测试分支，部署之间的差异只体现在能力报告中。

**单元测试**

- Compose 规范化与字段归类；清单生成；启动分层；运行时参数提取；bind 源组；share 发布的不可变与校验；
- 拓扑与外部策略：给定 Compose 网络，比对 `Topology` 与每个单元的 `ExternalPolicy`（internal 网络为 `none`、调用方策略、窄列表拒绝 `0.0.0.0/0`）；
  成员不含 `network_mode: none` 单元；单服务 trial、`wire` 之前的重建、释放与 gc 都不调用 `link`；
- 执行上下文（3.5 节）：每个服务的客户端执行都带上服务环境与 `request.env_vars`，业务进程与健康检查不带；每个 `ProcessSpec` 都有完整的 env、uid、gid，`exec` 都有超时；
- hosts 生成：按网络可见、系统条目保留、只替换标记块。

**后端单元测试**：把 `Topology` / `ExternalPolicy` / `SharedVolume` 编译为平台请求（OpenSandbox：`cidr` 策略的规则集、`any` 形式对 sandbox 网段与 `platform_cidrs` 的拒绝、
规则数超过上限时编译期报错、每个创建请求都带 `networkPolicy`、执行以 `env -i` 清空后只按名字带回请求 `envs` 的键、列表的 `metadata` 参数编码；见后端文档）。

**编排核心（假平台）**：内存实现的 `Platform`，带错误注入（429、5xx、创建超时后实例已存在、`link` 失败）与调用计数；`list` 按 `list_visibility_s` 延迟可见。
与编排核心共享一个手动推进的 `Clock`，时序在虚拟时间上运行，不真实等待：

- 回收：取消（含创建请求已被平台接受、响应返回前取消）；创建结果都明确的 trial 释放时不调用 `list`；正常 `close` 删除本 launch 目录；
- gc：续期后的实例不被 gc；停用后不再发出创建；候选只在两次观察后删除；锚点可见前的新 launch 目录不被删除；实例删尽前不删除 launch 目录；
  `gc --launch` 在目标锚点存在时拒绝；临时锚点不成为候选；
- 运行期：实例丢失、进程查询不到使 `check()` 返回 `lost`；带 restart 策略的服务退出使 `check()` 返回 `service`；存活确认与 `check()` 不调用 `list`；
- 健康检查判定对照 Docker 的行为；未发布的清单被 `open` 拒绝；按不同 `Capabilities` 组合验证拒绝与报告。

**契约测试**（`tests/contract/`）：`Platform_Requirements.md` C1–C15 各项的验收，`flotilla probe --deployment <配置>` 在部署接入或升级时运行，输出能力报告；
网络可达、隔离、DNS 行为、生效时机、出口策略不可绕过只在这里验证，不进入 trial 路径。

**端到端**：每个部署上的 web + app + db 任务；Compose 在 Docker 下与在 flotilla 下运行同一组检查脚本，结果一致。

**兼容性**：xtuner 的 SandboxPoolProvider 与 FlotillaProvider 在单服务任务上的行为对照。

---

## 15 平台要求与验收

平台必须提供的能力（C1–C16）、每项的类别与验收方法见 `Platform_Requirements.md`；各部署的现状、缺口、向平台提出的要求与探测记录见后端文档
（OpenSandbox：`backends/opensandbox.md` 第 2、7、8 节）。本文不重复这些内容，也不记录任何部署的实测结果。

---

## 16 代码组织与配置

### 16.1 仓库布局

```
flotilla/
  pyproject.toml            发布名 flotilla-compose，import 名 flotilla；命令行入口 flotilla
  src/flotilla/
    platform/
      base.py               Platform、Clock、Capabilities、InstanceSpec、ProcessSpec、InstanceState 等值类型、错误类别（第 2 节）
      opensandbox/          OpenSandbox 后端（backends/opensandbox.md）
        lifecycle.py        创建、查询、列出（过滤与翻页）、删除、续期
        execd.py            执行、后台进程、文件（envs + env -i 按名字带回、uid / gid、超时、上传避开 multipart；current / legacy 协议）
        network.py          外部策略与 link 编译为 networkPolicy（创建时与 wire 时各一次）
        link_cidr.py        组内互联策略 cidr（标准 IP / CIDR）；目前编译在 network.py，本文件尚为空
        volumes.py          SharedVolume → host 卷
        storage.py          [storage]：标准 host 卷设置
        address.py          内部地址
        http.py             重试、退避、限流、请求日志脱敏、错误映射
        platform.py         组装 Platform（能力报告由 config 加载后以 Capabilities 传入）
      fake.py               内存实现与 ManualClock，带错误注入（第 14 节）
      clock.py              SystemClock：Clock 的真实实现，由使用方注入编排核心
    core/                   编排核心（第 3、5–7 节）：只依赖 platform.base
      trial.py              阶段：prepare / create / address / wire / hosts / start / ready
      topology.py           depends_on 分层
      process.py            业务进程启动、健康检查、启动期退出检测、restart 策略核对
      network.py            Compose 网络 → Topology、每个单元的 ExternalPolicy（第 6 节）
      hosts.py              hosts 块生成与写入（3.7 节）
      volumes.py            卷键、种子、属主、bind 源组
      reaper.py  anchor.py  gc.py
    compose/                解析与规范化、字段归类表（4.6 节）；build 与 scan 共用
    build/                  BuildKit、bind 与任务文件、推送、写清单（第 4 节）
    scan.py                 扫描器（第 13 节）
    share/                  发布布局、publish / verify / gc（3.3 节）
    manifest.py             清单 schema（带 schema 版本，4.5 节）
    config.py               FlotillaConfig：所有运行期配置的唯一入口（16.3 节）；不调用平台的启动校验与到各设置的翻译
    capabilities.py         能力报告 schema 与判定（必需项、安全缺口、到 Capabilities 的投影；Platform_Requirements 第 5 节）
    probe.py                flotilla probe：测得项 + 部署者声明项 → 能力报告（第 14 节）
    integrations/
      xtuner.py             FlotillaProvider（第 10 节）
      harbor.py             Harbor BaseEnvironment（第 11 节）
    cli.py                  build / publish / scan / gc / probe / share
  schema/                   清单与能力报告的 JSON Schema
  tests/
    unit/  e2e/
    contract/               只针对 Platform 协议与部署性质的测试；按部署运行（第 14 节）
  deployments/              公开示例在 examples/；本地配置与报告在 local/，不入库
  images/anchor/            锚点镜像：FROM scratch + 静态 busybox（/.flotilla/bin/busybox 与 /bin 下的 applet 链接）
  images/probe/             probe 与端到端脚本的被测单元镜像（普通任务镜像形状，按 digest 引用，兼验 C15）
```

没有 Go 代码：sandbox 内不运行 flotilla 的程序，share 中只有第三方的静态 busybox 与 agent 运行时。

### 16.2 依赖规则

| 规则 | 理由 |
|---|---|
| `core` 只 import `platform.base`，不 import 后端；`core` 中不出现规则、CIDR、网段等平台概念；`platform` 不 import `core` | 原则 2；后端由配置实例化后注入，测试注入 `fake` |
| `core` 不直接使用挂钟（`time`、`asyncio.sleep` / `timeout` / `wait_for`、事件循环时间、`datetime.now`），一律经注入的 `Clock`（2.3 节） | 时序不变量要能在虚拟时间上确定性地测试（第 14 节） |
| 后端内不按部署名分支；部署差异只在部署配置与按配置选择的实现策略 | 第 8 节；每种策略通过同一套契约测试 |
| `compose`、`build`、`scan` 不调用平台，只读能力报告 | 构建与扫描在离线机器上运行，没有平台凭证；需要写共享存储的部分拆到 `publish`（4.7 节） |
| `integrations`、`probe`、`cli` 在最外层，不被其它内部包 import；`integrations` 按结构实现接口，不 import xtuner / Harbor 的实现 | PRD T1；Harbor 适配只在可选依赖中安装；`probe` 用 core 的锚点，放在外层免得 core 反向依赖它 |

以上 import 规则由 import-linter、挂钟规则由 ruff 的 banned-api 机械检查，进快车道 CI（`pyproject.toml`）；新增顶层包时须同步写进规则。

### 16.3 使用者配置

配置按角色分部分。平台凭证一律从环境变量读取，不出现在配置文件、清单与日志中（原则 8）。

| 角色 | 配置什么 | 何时 | 产物 |
|---|---|---|---|
| 部署者 | 部署配置（`[opensandbox]`、`[opensandbox.network]`、`[storage]`）；创建共享根目录；`flotilla probe`（含部署者声明的 `inbound_isolation`、`implicit_egress`）；`flotilla share publish` | 每个部署一次；share 更新时 | 能力报告、share 发布号与 `RELEASE.json` 的 sha256 |
| 数据集构建者 | 镜像仓库、`namespace/repo`、清单输出目录、目标部署的能力报告、构建并发 | 每个数据集 | 服务镜像、任务清单、本地任务文件、扫描报告 |
| 数据集发布者（常由部署者兼任） | 与 provider 相同的 `[opensandbox]`、`[storage]` | 每次构建之后 | `tasks/<build_key>/`、已发布的清单（4.7 节） |
| 训练者 | provider 配置（下文）；xtuner 配置中 provider 的导入路径；数据 jsonl 中每条的清单路径 | 每个实验 | 实验记录中存部署名、能力报告、share 发布号与清单目录 |

**provider 配置**（`FlotillaConfig`；可以写成 TOML 文件，也可以在 xtuner 配置中直接给 dict）。后端由配置中出现的后端段落决定（目前只有 `[opensandbox]`）；
同一后端的不同部署配置结构完全相同，只是值不同。后端段落的字段含义见后端文档（OpenSandbox：`backends/opensandbox.md` 第 1 节）：

```toml
[opensandbox]
endpoint = "https://<生命周期 API 基址>"
credential_env = "FLOTILLA_OPENSANDBOX_CREDENTIAL"   # 环境变量名，值为部署的 API key
capabilities = "deployments/<部署>.json"              # flotilla probe 的产物（2.2 节）

[opensandbox.extensions]                # 原样写入每个创建请求；键由部署决定，flotilla 不解释
# 例：项目、资源池

[opensandbox.privileged_extensions]     # 需要特权运行时的单元额外写入；键由部署决定
# 例：运行时类

[opensandbox.network]                   # 后端的实现策略与部署事实（backends/opensandbox.md 第 1、4 节）
link = "cidr"                           # 标准 IP / CIDR 策略
sandbox_cidrs = ["<sandbox 网段>"]
platform_cidrs = ["<节点网段>", "<service 网段>", "169.254.169.254/32"]   # external = "any" 时必填（后端文档第 1 节）

[opensandbox.execd]                     # backends/opensandbox.md 3.6 节
protocol = "current"                    # 或 "legacy"（旧版 execd：command 字符串、旧上传后 chown）
wrapper = "/.flotilla/bin/busybox"      # 执行包装用的 busybox；锚点镜像须在同一路径自带

[opensandbox.create_fields]             # 原样合并进创建请求的其余顶层字段，不能覆盖 flotilla 管理的字段
# 按部署需要设置附加字段；不能覆盖 flotilla 管理的字段

[storage]
volumes = "host"                        # 标准 host 卷（后端文档第 6 节）
host_path = "/<平台允许的共享根目录>"      # volumes = "host"：共享根目录（7.2 节）

[share]
release = "2026-09-28.1"
release_sha256 = "<RELEASE.json 的 sha256>"

[network]
external = ["<推理网关的 IP / CIDR 或域名>"]   # 6.4 节；窄列表。任意外网（与 Compose 普通网络一致）写 external = "any"

[security]
accept_insecure = []                    # 显式接受的安全类缺口，例如 ["exec_auth"]（第 2 节）；默认为空，即拒绝

[runtime_params]                        # 4.2 节第 1 步、第 10 节
CB2TB_API_UPSTREAM = "https://<推理网关>/v1"

[agent_overlay]                         # 第 10 节
env = { PATH = "/.flotilla/runtime/node/node-v22.14.0/bin:${PATH}", SANDBOX_RUNTIME_DIR = "/.flotilla/runtime" }

[limits]                                # 每进程
create_rate = 5                         # 个/秒
create_concurrency = 64
trial_concurrency = 512
reap_concurrency = 16
exec_rate = 500                         # 次/秒，执行通道调用的硬上限（第 12 节）
exec_timeout = 3600                     # 秒，使用方的 execute 未给超时时的上限（3.5 节）

[timeouts]                              # 秒，5.1、5.5 节
create = 600
address = 30
wire = 60                               # link 调用；平台异步应用时含后端等待完成状态（2.3、6.5 节）
hosts = 30
prepare = 600                           # 经锚点准备共享卷目录（7.2、7.3 节）
start_max = 1800
close = 20

[process]                               # 3.6 节
poll_interval = 2                       # 启动期对一次性服务的退出检测间隔

[reaper]                                # 5.6 节
anchor_image = "<repo>@sha256:…"        # 锚点镜像（images/anchor/），自带 /.flotilla/bin/busybox
anchor_ttl = 600
renew_interval = 30
grace = 90                              # G
gc_window = 180                         # W，须大于 G + list_visibility_s
gc_interval = 600
trial_ttl = 21600                       # 每个实例的平台 TTL，长 trial 续期

[resources.defaults]                    # 按服务类型（第 12 节）；清单中的逐任务覆盖优先
database = { cpu = "1", memory = "2Gi" }
cache = { cpu = "0.5", memory = "512Mi" }
app = { cpu = "1", memory = "2Gi" }
agent = { cpu = "2", memory = "4Gi" }
```

除 `opensandbox`、`storage`、`share` 外都有默认值。`limits`、`process`、`resources.defaults` 的数值是占位，按 M0 压测与抽样校准确定。

**启动时校验**（provider `start`，任何一项失败都在训练开始前报错；不需要平台的部分由 `FlotillaConfig.check_against` 实现，一次列出全部问题，
共享根目录与 share 发布的核对经锚点在 provider `start` 中做，尚未实现）：凭证环境变量存在；能力报告存在且 endpoint 与配置一致；
共享根目录存在；share 发布存在且 `RELEASE.json` 的 sha256 与配置一致（3.3 节）；`grace + list_visibility_s < gc_window` 且 `renew_interval × 2 ≤ grace`、`renew_interval` 不小于锚点续期失败的重试间隔（5.6 节）；能力报告的必需项全部满足；
`runtime_params` 中的地址都在 `[network] external` 内，`"any"` 时不落在平台基础设施中（第 10 节、6.4 节）；`[network] external` 的形式在能力报告的 `external_forms` 中，
窄列表不含 `0.0.0.0/0` 这类任意外网写法（6.4 节）；能力报告的每个安全类缺口（`inbound_isolation`、`exec_auth`、`no_auto_mount` 为假）都列在 `accept_insecure` 中，
`accept_insecure` 中没有能力报告已满足或不认识的项（第 2 节）；后端自身的校验（OpenSandbox 见后端文档第 1 节）；配置中没有未知字段。

**命令行**：

| 命令 | 使用者 | 作用 |
|---|---|---|
| `flotilla probe --deployment <配置> --declared <声明> --image <repo@sha256:…>` | 部署者 | 对一个部署运行契约测试，写出能力报告（第 14 节、`Platform_Requirements.md` 附录 A）。首版测单服务所需项，其余取自声明文件（来源 `declared`）；测试本身出错时不写报告 |
| `flotilla share publish <dir>` / `verify` / `gc` | 部署者 | 发布、全量校验、删除无引用的旧发布（3.3 节） |
| `flotilla build <tasks…>` | 数据集构建者 | 构建并写清单（第 4 节） |
| `flotilla publish <manifests…>` | 数据集发布者 | 上传任务文件并把清单标为已发布（4.7 节） |
| `flotilla scan <tasks…> --capabilities <报告>` | 数据集构建者 | 逐字段归类报告（第 13 节） |
| `flotilla gc [--launch <id>] [--images]` | 训练者 / 运维 | 清理残留，经临时锚点；`--launch` 在目标锚点仍存在时拒绝（5.6 节） |

---

## 17 首版不做的扩展

以下能力有明确的使用场景，但首版的数据或平台还用不上。每项写明加回来的条件与要点，避免届时重新设计；在此之前，接口、`Capabilities` 与平台要求中都不出现它们。

| 扩展 | 首版行为 | 加回来的条件 | 要点 |
|---|---|---|---|
| Harbor `no-network` / `allowlist` 与阶段之间切换策略 | Harbor 按能力位拒绝（6.6 节） | 评测需要覆盖声明 `no-network` 或 `allowlist` 的任务 | 受控组（`main` 与未显式声明网络的服务）单独成网；`no-network` 需要平台隐式放行只含 DNS 53（C13 的端口限定）；`allowlist` 需要按协议与 TLS SNI 限定，否则 UDP 与同 IP 的其他站点会被放通；切换需要 `link` 以相同成员整体替换拓扑、外部策略可在运行中替换且不清空域名放行的动态结果；Harbor 每个阶段切换两次（开始与 `finally` 中切回） |
| Harbor 独立验证模式的 `stop_service` | 扫描拒绝，调用时报错（第 11 节） | 评测需要覆盖依赖停止服务的独立验证模式任务，且平台提供"停止实例内除执行通道与入口之外的全部进程、返回时已终止"，或删除可确认进程已终止 | 停止后的单元不再接受执行，仍可下载（与 `docker cp` 一致）；以删除代替时之后不能下载，删除前不撤销互联（2.3 节的已知例外） |
| restart 策略在运行中重启 | `check()` 时发现退出即作废样本（3.6 节） | M2 统计的作废率不可接受，且平台在主进程退出时清理整个进程树 | 运行期退出检测：有平台退出事件时用事件，否则按间隔轮询（最坏每秒上万次，须限流）；重启前清空 tmpfs 目录；`stop_signal` / `stop_grace_period` 需要平台可指定信号与宽限期 |
| 一次性服务提前回收 | 保留到 `release`（5.3 节） | 规模化实测存活数量成为瓶颈 | 只回收不在任何网络中的单元，避免撤销互联；退役单元消失不算运行期故障；删除前取回退出码与日志 |
| 多容器实例（C16）与其余 C14 子项（附加组、伪终端、rlimit、`/dev/shm`、只读根文件系统、tmpfs、主机名） | 扫描按 4.6 节拒绝或告警 | 平台提供对应能力 | 在 `InstanceSpec` / `ProcessSpec` 中加参数，`Capabilities` 加字段，补契约测试 |
