# flotilla 产品需求文档

> 状态：开发中的 alpha。本文描述目标设计，含尚未实现的构建、发布与使用方适配；当前实现范围见 README。

> flotilla：把一个 Compose 环境运行成一支"小舰队"——每个服务一个 sandbox，编队启动、互相联络、一起回收。
> 发布名 `flotilla-compose`，import 名 `flotilla`。
>
> 本文回答"做什么、为什么这样做"。相关文档：
>
> | 文档 | 内容 |
> |---|---|
> | `Architecture.md` | flotilla 的架构设计 |
> | `Platform_Requirements.md` | flotilla 对沙盒平台的能力要求（C1–C16），与具体平台无关 |
> | `backends/opensandbox.md` | OpenSandbox 后端：各部署如何满足上述要求、现状与缺口 |
> | `XTuner_Environment_Design.md` | xtuner 把 Environment 作为一等概念的设计 |

---

## 1 一句话

flotilla 是 **Harbor 任务环境与沙盒平台之间的编排层**：把 Harbor 格式任务的环境（Compose 或 Dockerfile）
运行成**一组 sandbox（每个服务一个）**，负责启动顺序、服务之间的连接与隔离、共享存储和回收。
单服务任务是只有一个服务的特例，走同一条路径。

flotilla 不懂 RL，也不懂验证。它通过两个薄适配层对外提供服务：

| 使用方 | 接入方式 | 优先级 |
|---|---|---|
| **xtuner** | 实现 xtuner 的 `EnvironmentProvider` 接口（该接口由 xtuner 定义，flotilla 是其实现之一） | **主要** |
| Harbor | 实现 Harbor 0.23 的 `BaseEnvironment`，用于 `harbor run` 评测 | 次要 |

平台：编排核心只经一个与平台无关的接口 `Platform` 使用沙盒平台（5.5 节）；平台需要提供什么由 `Platform_Requirements.md` 单独规定。
首版的平台后端是 OpenSandbox 兼容平台，其他平台（如 E2B）以新后端接入，不改变本文的需求。

---

## 2 背景

### 2.1 容量模型

规模由使用方的数据与部署容量决定，不预设固定的任务数或并发目标。设同时运行的 trial 数为 `T`、平均服务数为 `S`、
平均时长为 `D` 秒，则存活实例约为 `T × S`，稳态创建速率约为 `T × S / D`；另加每个 provider 的锚点。
按 ID 查询、执行、互联与共享存储的容量要分别评估，部署声明与实际压测共同确定限流。

### 2.2 场景：网站运维 agent

agent 在一个工作区中运维一套网站服务，例如排查故障、修改配置、迁移数据、扩容、恢复服务。
一个典型任务的环境：

```
main      agent 工作区：shell、运维工具、代码仓库
web       反向代理（如 nginx）
app       应用服务（任务自带 Dockerfile，需要构建）
db        数据库（如 MySQL / PostgreSQL）
cache     缓存（如 Redis）
worker    后台任务（可选）
init      一次性初始化：建表、导入数据，完成后退出
```

这类任务在 Compose 中通常会用到：

| Compose 能力 | 用途 |
|---|---|
| `build` | 应用镜像由任务自带的 Dockerfile 构建 |
| `depends_on` 带 `service_healthy` / `service_completed_successfully` | 数据库健康后才初始化；初始化完成后才启动应用 |
| `healthcheck` | 判断数据库、应用是否就绪 |
| 服务名访问 | `app` 通过 `db:5432`、`cache:6379` 访问依赖；agent 通过 `web:80` 访问网站 |
| named volume | 数据库数据目录；应用与反向代理共享的静态文件目录 |
| bind mount | 任务目录中的配置文件（如 nginx 配置） |
| 自定义 network、`internal: true` | 把服务分成前端、后端两组，后端不对外 |
| `environment` / `env_file` | 数据库口令、应用配置 |

### 2.3 当前实现

仓库已包含 Compose 规范化、清单生成、扫描库、trial 生命周期、回收与监视、OpenSandbox 后端、配置与能力探测。
离线镜像构建、任务与 share 发布，以及 XTuner / Harbor 适配仍在开发中；对应 CLI 子命令目前为占位。

目标是让训练与评测通过同一份任务清单得到相同的环境行为。完整流程和真实 OpenSandbox 部署的端到端验证尚未完成。

---

## 3 方案：每个服务一个 sandbox

### 3.1 决定

**每个 Compose 服务运行在一个独立的 sandbox 中，由平台集群直接调度；sandbox 内不运行 Docker。**
flotilla 在平台原语之上实现 Compose 的语义：启动顺序、健康检查、服务名解析、网络连通与隔离、卷。

### 3.2 理由

| 方面 | 说明 |
|---|---|
| 节点池 | 服务运行在通用资源池上，不受嵌套容器节点的限制 |
| 镜像 | 按"节点 × 镜像"拉取，同一节点上的镜像层共享；GRPO 同组 8 个 trial 可以共享任务独有镜像的缓存 |
| 调度粒度 | 多个小 sandbox，碎片化时更容易调度 |
| 可观测性 | 每个服务在平台上独立可见：状态、资源、日志、事件 |
| 隔离 | trial 内按 Compose 网络互联、trial 外不能连入，由平台的组内互联与隔离能力保证；不依赖 sandbox 内的 Docker 网络 |
| 可移植 | 只依赖与平台无关的 `Platform` 接口（5.5 节），换平台只换后端 |

### 3.3 代价

| 代价 | 说明 |
|---|---|
| Compose 语义要自己实现 | 启动顺序、健康检查、服务名、网络、卷都要翻译成平台能力 |
| sandbox 数量 | 实例数量与并发 trial 数、每任务服务数成正比；必须按目标负载评估存活数、创建与回收速率 |
| 内存 | 按限制值设置各服务的内存，要按峰值给；由 flotilla 的资源默认值、逐任务覆盖和抽样校准控制 |
| 控制面调用 | 健康检查与启动期的退出检测由训练侧经平台执行通道驱动，调用量与服务数成正比，有每进程硬上限；运行期只有低频的存活查询（`Architecture.md` 第 12 节） |
| 清理面 | 每个 trial 有多个资源，可能部分删除失败，需要回收器和残留清理 |

---

## 4 定位与集成方式

### 4.1 分层

```
使用方配方    训练配方：读任务、启动 agent、运行验证             只写配置和 hook
    │
xtuner        rollout 编排：阶段、hook、judger、reward、取消     定义 EnvironmentProvider 接口
    │ EnvironmentProvider
    ├── SandboxPoolProvider     xtuner 内置：现有 SandboxPool（含依赖组）逻辑
    ├── FlotillaProvider   由 flotilla 提供：Harbor 任务环境 → 一组 sandbox
    └── 其他实现
            │
flotilla      Harbor 任务环境 ↔ 沙盒平台编排层                  不懂 RL，不懂验证
    │
 沙盒平台      经 Platform 接口接入；首版后端为 OpenSandbox 兼容平台
```

| 层 | 负责 | 不负责 |
|---|---|---|
| 使用方配方 | 训练配置、任务读取、agent 启动、验证与 reward | sandbox 的创建和编排 |
| xtuner | rollout 流程；定义环境接口；每条轨迹打开和释放环境 | 某一种沙盒平台的细节 |
| flotilla | 环境的创建、就绪、连接与隔离、卷、回收、残留清理 | 训练流程、验证方式、reward |

依赖方向：

- 接口由使用方（xtuner）定义。**xtuner 不 import flotilla**，只通过配置中的导入路径加载 provider；
  **flotilla 也不 import xtuner**，按接口的结构实现。
- 切换环境实现改的是 provider 配置与请求的构造方式，配方的流程和 hook 不变。两种实现的请求形态不同：SandboxPoolProvider 忽略 `task`，
  组成全部来自 `request.sandboxes`；FlotillaProvider 的组成来自 `task` 指向的清单，`request.sandboxes` 只放独立 sandbox（如独立验证环境），与清单重名时报错。
  所以统一配方按所选 provider 构造请求（SandboxPoolProvider 下由任务的单服务环境生成 `sandboxes["main"]`，FlotillaProvider 下只给 `task`），
  这部分由配方中一个按 provider 选择的请求构造函数承担；
- 单服务任务可以先用 SandboxPoolProvider 跑通统一配方，多服务任务在 flotilla 就绪后切换 provider。

### 4.2 xtuner 的环境接口

xtuner 上游的 `SandboxPool` 已经支持"一个名字一个依赖组"（#2032），但组成写死在静态配置中，
实现写死为 `SandboxPool`，且 provider 每条轨迹重建一次。环境组成由任务决定、
需要进程级回收器的实现（如 flotilla）无法接入。

因此在 xtuner 中把"环境"提升为一等概念，接口的形态（详见 `XTuner_Environment_Design.md`）：

| 概念 | 含义 |
|---|---|
| `EnvironmentProvider` | 同一配置在进程内（按事件循环）共享一个实例、跨轨迹复用；`open(request)` 为一条轨迹打开环境；一次性初始化在第一次 `open` 时完成，没有启停钩子 |
| `Environment` | 一条轨迹的环境；按名字 `get` 客户端（首次调用时就绪），`release` 释放 |
| `EnvironmentRequest` | 轨迹 ID、组 ID、不透明的任务描述 `task`、额外的具名 sandbox、本轨迹的环境变量 `env_vars` |
| 客户端 | 沿用现有的 sandbox 客户端接口，hook 和 agent 执行器不变 |

这是 xtuner 的通用重构，**不含任何具体平台或 Harbor 概念**，计划提交到 xtuner 上游。
现有的 `SandboxPool` 直接实现 `Environment`，旧配置不改动即可运行；`Runner` / `Judger` 只依赖接口，换环境实现只改 `Runner` 的 `environment` 配置。

**重试只在环境内部**：provider 处理平台层面的失败（创建超时、429、依赖组整组重试），xtuner 不再重来，环境报告的错误使样本失败。
错误带 `stage` / `category` / `retryable`，xtuner 只用于记录。

### 4.3 统一训练配方

使用方可以用统一的训练配方读取 Harbor 格式任务目录。配方由使用方维护：

| 任务部分 | 配方的处理 |
|---|---|
| `environment/` | 离线由 `flotilla build` 编译为任务清单；配方把清单路径作为任务引用交给 provider |
| `instruction.md` | 作为 agent 的输入 |
| `tests/` 与 `task.toml` 的 verifier 配置 | 配方按 Harbor 约定执行，得到 reward |
| `task.toml` 的超时、资源与网络策略 | 作为 agent 超时、验证超时、资源默认值和出口策略 |

- 现有的按数据集划分的配方在统一配方完成对比后下线。
- 新增数据集只需要是 Harbor 格式。
- agent 的启动方式（harness）与任务无关，保持现有做法。

### 4.4 训练中的一条轨迹

```
使用方配方 / xtuner               FlotillaProvider / flotilla          sandbox
  │ open(request)                    │ 读任务清单，只做本地准备              │
  │ get("main")                      │ 创建全部服务、放行、写 hosts、按依赖启动 │
  │─────────────────────────────────►│                                    │
  │◄──────────── main 的客户端 ────────│                                    │
  │ 在 main 中启动 agent（约 2 小时）    │                                    │
  │ 按 Harbor 约定验证                 │ 需要独立验证时：创建独立的验证 sandbox │
  │ release()                        │ 交给后台回收器，立即返回              │
```

训练对环境实现有几项评测场景没有的要求：

| 要求 | 原因 |
|---|---|
| 基础设施故障必须以错误返回，不能变成 reward | xtuner 会丢弃失败样本；如果基础设施故障被当成 0 分，会污染训练信号 |
| 启动相关的失败必须在 agent 开始前暴露 | 否则一次失败会浪费 2 小时的 trial |
| 任务被取消时不能漏删资源 | 权重同步和退出时，进行中的任务会被取消；被取消的任务只有 5 秒收尾，释放不能在这 5 秒内同步完成 |
| 训练进程重启后要能清理上一次的残留 | 否则残留 sandbox 要等 TTL 过期 |
| 可以横向扩展到多个进程 | 不同 provider 实例按各自事件循环和配置持有资源 |
| 控制面调用可估算、有上限 | 数万 trial、十几万 sandbox，轮询次数直接决定控制面压力；调用量按服务数估算，每进程有硬上限（`Architecture.md` 第 12 节） |

### 4.5 评测

Harbor 适配层按 Harbor 原生语义实现：

- `exec` / 上传 / 下载，默认作用于 `main`；可按服务名执行与下载；
- `network_policy` 精确执行（可访问范围不超过 Harbor）。首版适配设计只支持 `public`；`no-network`、`allowlist`、阶段间策略切换与逐服务停止暂不支持，由 Harbor 按能力声明拒绝对应任务。训练侧不走 Harbor 的策略配置，见 `Architecture.md` 第 6.6、11、17 节。

---

## 5 方案概述

### 5.1 离线：任务编译

对每个任务执行一次，结果供所有 epoch 复用：

1. 执行 `build`（Compose 中的 `build`，或单服务任务的 Dockerfile）；
2. 扫描字段；共享网络、进程或 IPC namespace 的服务（`network_mode: service:x` 等）需要平台的多容器 sandbox，在此之前拒绝；
3. 推送到镜像仓库，生成每个任务的任务清单；
4. 服务镜像不追加 flotilla 层，sandbox 内不运行 flotilla 的进程：agent 运行时与 harness 放在只读挂载的共享目录（share）中，按发布号版本化；执行守护进程由平台注入。

### 5.2 在线：一个 trial

1. 并行创建全部服务的 sandbox，入口只是占位进程，彼此隔离，服务进程还未启动；
2. 取得每个 sandbox 的内部地址；
3. 把 Compose 网络编译为拓扑（每个网络是一组服务），交给平台一次互联：至少共享一个网络的服务互相可达；调用返回即生效由平台保证，flotilla 不做连通探测；单服务 trial 跳过这一步；
4. 经平台执行通道为每个 sandbox 写入 `/etc/hosts`（本 sandbox 可见的服务名、alias → 内部地址），读回比对；
5. 按 `depends_on` 条件经执行通道启动服务，健康检查由 flotilla 经执行通道执行；
6. 就绪后交给使用方；
7. 使用方完成 agent 与验证后释放，flotilla 在后台删除全部资源。

### 5.3 服务之间的连接与隔离

目标：trial 内的行为与 Compose 一致。服务按 Compose 中的服务名和 alias 访问同一 trial 中的其他服务，例如 `app` 连接 `db:5432`；
至少共享一个网络的服务之间可达，不共享网络的不可达；不额外限制端口或服务。

flotilla 负责把 Compose 网络编译为拓扑、为每个服务确定外部策略、按实际地址写 hosts、互联生效后按依赖启动、按业务整体替换拓扑；
让拓扑成立（可达、隔离、生效时机）、提供实际地址、平台自身的 DNS 与元数据访问由平台负责，在部署接入或升级时由契约测试验证，不在每个 trial 中探测。

| 方面 | 行为 |
|---|---|
| trial 内连通 | 经内部地址与服务原有端口直连，TCP 与 UDP；至少共享一个网络的服务之间可达，不共享的不可达；默认网络连通所有未声明网络的服务；与服务被调度到哪里无关 |
| 名字 | 每个 sandbox 的 `/etc/hosts` 写入本 sandbox 所在网络上可见的名字；不使用平台主机名，不运行 DNS 服务 |
| trial 外入站 | 其他 trial、平台上的其他实例与外部不能连入 |
| 出站 | 只在 `internal` 网络中的服务不能对外；其余服务按训练侧显式配置的外部策略：任意外网（`"any"`）与 Compose 普通网络一致；配成只放行推理网关等窄列表时，出站受调用方策略限制，不等同于 Compose 的普通网络 |
| 生效 | 互联与外部策略的设置返回即生效；flotilla 不以探测或固定等待补偿 |
| 控制面 | 训练侧只经平台鉴权的执行、文件接口访问 sandbox；不发布任何端口 |
| 共享 namespace | `network_mode: service:x`、共享 `pid` / `ipc` 需要平台的多容器 sandbox，部署不提供时拒绝 |

hosts 的已知限制：直接发 DNS 查询的程序不读 hosts；同名多地址没有轮询；应用可能缓存解析结果。扫描器报告可能受影响的任务。

### 5.4 named volume

| 情况 | 做法 |
|---|---|
| 只有一处读写挂载（典型是数据库数据目录） | 该服务 sandbox 的本地磁盘 |
| 有多处挂载且至少一处可写（多个服务引用，或同一服务挂到多个位置） | 共享存储中每个 trial 独立的子目录，挂载给每一处引用；挂载与跨节点共享由平台保证 |
| 全部挂载只读 | 共享存储中按任务的不可变目录，所有 trial 共用 |
| 初始内容 | 与 Docker 一致：空卷第一次挂载时复制镜像中该路径的原有内容 |
| 回收 | trial 的 sandbox 全部删除后删除本 trial 的目录；进程崩溃留下的目录由残留清理删除 |

### 5.5 对平台的依赖

flotilla 经一个与平台无关的接口 `Platform`（`Architecture.md` 第 2 节）使用沙盒平台；接口按 flotilla 需要的语义定义（例如"让这组实例按这个拓扑互联"），
不按某个平台的原语定义。本文只规定 flotilla 对使用方的行为，不规定平台如何实现：

- 平台必须提供的能力与保证（C1–C16）、每项的类别与验收，由本文需求与 Architecture 的关键决定推导，见 `Platform_Requirements.md`；
- 各平台、各部署如何满足、哪里还不满足，见各后端文档（目前只有 `backends/opensandbox.md`）；
- 平台能力缺失时 flotilla 不补足（不以隧道、代理、探测或固定等待替代），按类别处理：必需项缺失时拒绝该部署；功能项缺失时拒绝依赖它的任务；
  安全项缺失时默认拒绝该部署，显式接受后运行并在报告中标出。

各部署满足哪些能力，由 `flotilla probe` 在部署接入或升级时运行同一套契约测试得到能力报告，扫描器据此提前拒绝在该部署上跑不了的任务。

---

## 6 范围

### 6.1 首版做

按仓库划分：

| 仓库 | 内容 |
|---|---|
| flotilla | per-service 编排（单服务任务作为特例）；服务之间的连接与隔离（拓扑 + hosts）；`flotilla build` / `publish` / `scan` / `gc` / `probe`；`FlotillaProvider`（xtuner 接口实现）；Harbor 0.23 适配层；`Platform` 接口与契约测试；OpenSandbox 后端，按部署进行契约验收 |
| xtuner | `Environment` / `EnvironmentProvider` 接口；`SandboxPool` 实现该接口，新增 `SandboxPoolProvider`；`Runner` / `Judger` 改用该接口；`env_vars`；`check`。改动只在 `sandbox_agent_loop/` 内。设计提案见 [XTuner PR #2135](https://github.com/InternLM/xtuner/pull/2135) |
| 使用方配方 | 统一的 Harbor 训练配方；provider 配置 |

### 6.2 首版不做

- 不修改 Harbor；
- xtuner 只做环境接口的通用重构，不加入任何具体平台、Harbor 或 flotilla 相关内容；
- flotilla 不设计验证方式，不修改 reward 计算和训练算法；
- 不在 sandbox 内运行 Docker；
- 不限制 agent 在 `main` 内的权限；
- 不提供常驻服务；
- 不管理 Kubernetes 集群；
- 按部署名分支的代码；E2B 等其他平台的后端；隧道、代理等补足平台能力的机制。

### 6.3 Compose 字段处理原则

**首版支持 Compose 中所有需要支持的字段。** 每个字段归入以下一类，扫描器按此报告：

| 类别 | 含义 |
|---|---|
| 实现 | 行为与 Compose 一致，有测试 |
| 等效 | 实现方式不同，但对服务可观察的行为一致（例如 bind mount 固化进镜像） |
| 拒绝 | 语义上无法在隔离的 sandbox 中成立，任务不能运行，扫描器给出原因 |

不允许静默忽略未知字段。拒绝分两种：

- **语义上不成立**：`pid: host`、GPU 运行时、平台不提供的宿主设备、无法精确执行的网络策略；
- **部署缺少对应的功能类平台能力**：共享 namespace（多容器 sandbox）、组内互联、跨节点共享卷、附加组、伪终端等。这部分随部署而变，由扫描器按该部署的能力报告判断；平台补齐、且 flotilla 的平台接口已能表达该能力后变为实现（首版接口未承载的 C14 / C16 子项见 `Architecture.md` 2.2 节）。

平台缺少安全类能力（入站隔离、执行通道鉴权、不自动挂载可写目录）时，provider 默认拒绝该部署；负责人在配置中逐项显式接受后才运行，
能力报告、扫描报告与每个 trial 的元数据都标出（理由：训练产出的 reward 难以事后甄别，默认放行时"是否用于正式训练"的决定实际上没有人做）。`hostname` / `domainname` 的名字解析实现（名字对其他服务可见），自身主机名是缺口（`gethostname()` 仍是平台的名字），扫描告警、任务不拒绝。完整的字段处理表见 `Architecture.md`。

---

## 7 需求

### 7.1 通用功能需求

| 编号 | 需求 |
|---|---|
| F1 | 默认的执行、上传、下载作用于 `main` |
| F2 | 可按服务名执行、上传、下载；服务名不存在时报错，不回退到 `main` |
| F3 | 所有服务满足 Compose 定义的就绪条件后才交给使用方 |
| F4 | 独立验证环境按 Harbor 语义单独创建，不接入 agent 环境 |
| F5 | 启动失败时报告失败服务、阶段、原因和日志 |
| F6 | 清理失败不覆盖原始错误，最终结果同时包含两者 |
| F7 | 扫描器报告每个任务的状态和原因 |
| F8 | 至少共享一个网络的服务可以按服务名和 alias、以原有端口互相访问（TCP 与 UDP），不额外限制端口 |
| F9a | 不共享网络的服务之间不可达 |
| F9b | 其他 trial、平台上的其他实例与外部不能连入 trial 内的任何服务 |
| F9c | 只在 `internal` 网络中的服务不能对外；其余服务的对外访问按训练侧配置的策略（任意外网或窄列表）放行 |

### 7.2 训练需求

| 编号 | 需求 |
|---|---|
| T1 | 通过 xtuner 的 `EnvironmentProvider` 接口接入；xtuner 与 flotilla 互不 import |
| T2 | 基础设施故障以错误返回，不产生 reward；错误带阶段和类别。包括环境就绪之后发生的故障（sandbox 丢失、续期失败） |
| T3 | 启动相关的失败在 agent 开始前暴露，启动时间有上限 |
| T4 | 调用方被取消时，资源回收不依赖被取消的任务，由 provider 在后台完成 |
| T5 | 训练进程重启后，自动清理上一次运行留下的资源 |
| T6 | 支持在多个进程中运行；限流可以按进程配置 |
| T7 | 健康检查与退出检测由训练侧经平台执行通道执行，只对需要的服务、只在需要的时段执行，调用量可估算、每进程有硬上限 |
| T8 | agent 的运行时与 harness 来自所有 sandbox 只读挂载的 share（按发布号版本化）；使用方可以给 agent 服务追加进程级环境变量，给每个服务的客户端执行追加逐轨迹的环境变量（不进入服务自身的业务进程）；任务中负责访问推理网关的服务（agent 服务或其代理服务）可以访问推理网关，其地址由训练侧配置注入 |
| T9 | 统一配方覆盖现有所有训练数据集（单服务与多服务），且在 SandboxPoolProvider 与 FlotillaProvider 之间切换只改 provider 配置与请求构造（4.1 节），配方流程与 hook 不变 |
| T10 | 重试只在 provider 内部；provider 报告的错误使样本失败，xtuner 不重新打开环境 |

### 7.3 xtuner 接口需求

| 编号 | 需求 |
|---|---|
| X1 | 接口不包含任何特定平台或任务格式的概念，可提交上游 |
| X2 | 同一配置的 provider 在进程内（按事件循环）共享一个实例、跨轨迹复用；没有启停钩子，provider 在第一次 `open` 时初始化，正确性不依赖进程退出时的清理 |
| X3 | 环境在第一次 `get` 时才创建；`release` 幂等、不抛异常、不被调用方的取消打断 |
| X4 | 现有配置不改动即可运行，现有测试（含依赖组）通过 |
| X5 | hook、agent 执行器使用的客户端接口不变 |

### 7.4 评测需求

| 编号 | 需求 |
|---|---|
| H1 | 实现 Harbor 0.23 `BaseEnvironment`，包括逐服务执行与下载；支持逐服务停止时，停止后该服务内不残留任何使用方启动的进程，不支持时如实声明，由 Harbor 拒绝依赖它的任务 |
| H2 | 支持 Harbor 的独立验证环境（独立创建，不接入 agent 环境） |
| H3 | 精确执行 Harbor `network_policy`，无法精确执行时拒绝任务。"精确"指能访问的范围不超过 Harbor |

### 7.5 非功能需求

| 编号 | 需求 |
|---|---|
| N1 | 启动业务之前，互联已生效、hosts 已写入。flotilla 不做连通或阻断探测 |
| N2 | 每个 sandbox 设置有限的标准 TTL，禁止永不过期；长 trial 续期 |
| N3 | 每个 sandbox 带启动实例、trial、服务等标签，可据此清理 |
| N4 | 平台凭证只在训练侧或评测侧进程中使用，不进入 sandbox、镜像、日志和代码仓库 |
| N5 | 命令执行和文件覆盖不自动重试 |
| N6 | sandbox 内不运行 flotilla 的控制接口，不发布任何端口；控制面只经平台鉴权的执行、文件接口 |
| N7 | trial 内的服务不能经执行通道在其他服务中执行命令（否则 agent 攻破任一服务后可以以 root 在其他服务执行命令）；flotilla 不以额外的端口限制补足 |

---

## 8 里程碑

### M0 公开 alpha 与部署验证

公开平台无关的实现、测试与设计，明确当前功能状态。后续在上游 OpenSandbox 部署上运行 `flotilla probe`，
补齐尚未自动测得的网络、共享存储与安全契约，并验证 web + app + db 的完整 trial。
公开源码不以这些验证全部完成为前提；验证完成前不宣称生产可用。

### M1 训练最小可用

完成镜像构建、任务与 share 发布、XTuner 环境接口及 FlotillaProvider；
跑通任务编译 → 环境启动 → agent → 验证 → 回收 → 残留清理的完整流程。

### M2 数据集迁移

按使用方选定的公开或自有数据集验证 Compose 字段处理与任务清单，比较环境行为、成功率与资源占用。

### M3 规模化

按部署容量逐步提高并发，测量控制面调用与存储开销；根据实测引入预热、缓存、批量操作等优化。

### M4 评测

完成 Harbor 适配，以同一后端和平台契约覆盖训练与评测；按能力明确拒绝尚未实现的独立验证或网络策略。
