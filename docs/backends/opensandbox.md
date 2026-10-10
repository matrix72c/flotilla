# OpenSandbox 后端：`OpenSandboxPlatform`

本文说明仓库当前的后端实现及其部署前提。后端已具备单元测试，但尚未完成公开 OpenSandbox 部署上的完整端到端验收。
平台能力要求见 `../Platform_Requirements.md`，编排设计见 `../Architecture.md`。部署示例在 `../../deployments/examples/`。

## 1 部署配置

`build()` 接受 server 根地址并追加 `/v1`。控制面凭证仅放在 `OPEN-SANDBOX-API-KEY` 请求头中。

| 配置 | 含义 |
|---|---|
| `opensandbox.endpoint` | Server 根地址，不包含 `/v1` |
| `opensandbox.credential_env` | 保存 API key 的环境变量名 |
| `opensandbox.capabilities` | 能力报告路径，相对于配置文件目录 |
| `opensandbox.network.link` | 当前只支持 `cidr` |
| `opensandbox.network.sandbox_cidrs` | 部署分配给 sandbox 的地址范围，必填 |
| `opensandbox.network.platform_cidrs` | 节点、service、控制面、元数据等地址；外部策略为 `any` 时必填 |
| `opensandbox.execd.protocol` | `current` 或旧版兼容协议 `legacy`，须显式选择 |
| `opensandbox.execd.wrapper` | 所有实例中静态 BusyBox 的绝对路径，默认 `/.flotilla/bin/busybox` |
| `opensandbox.extensions` | 透传给创建接口的扩展 |
| `opensandbox.privileged_extensions` | 特权运行时所需的附加扩展 |
| `opensandbox.create_fields` | 附加创建字段，不能覆盖镜像、网络、卷、TTL、资源等管理字段 |
| `storage.volumes` | 卷形式：标准 `host` 卷或 `pvc` 卷（第 6 节） |
| `storage.host_path` | `host`：所有 worker 可见、平台允许挂载的共享根目录 |
| `storage.claim_name` | `pvc`：承载共享根目录的卷名（`pvc.claimName`，DNS label） |
| `storage.root_subpath` | `pvc`：共享根目录在该卷内的相对路径；空串表示卷的根目录 |

配置校验要求 CIDR 合法，`cidr` 互联有 sandbox 网段，`any` 出站有平台网段。
能力报告、共享目录与 share 发布的接入校验设计见 Architecture 16.3 节；完整 provider 尚未实现。

## 2 能力与实现状态

| 能力 | 后端实现 | 验证范围 |
|---|---|---|
| C1 生命周期 | 创建、查询、删除、按标签分页列出、续期 | Mock HTTP 与单元测试；probe 可测 |
| C2 执行与文件 | execd、数字 uid/gid、工作目录、环境变量、文件读写 | 两种协议的单元测试；probe 可测主要行为 |
| C3 后台进程 | 启动、状态与退出码 | 单元测试；probe 可测 |
| C4–C7 网络与地址 | IP / CIDR 策略编译、策略整体替换、经 execd 读取地址 | 策略与调用单元测试；实际可达性和隔离需部署验证 |
| C8 共享存储 | 标准 host / pvc 卷与 subPath | 请求编译单元测试；probe 测只读与子目录隔离的部分性质 |
| C9 执行鉴权 | 默认经 server 代理携带控制面凭证 | 所有到达路径上的按实例鉴权需部署声明和独立验证 |
| C10 资源 | 等值写入 `resourceLimits` 与 `resourceRequests` | probe 读取实际 cgroup 限制 |
| C11–C13 容量、诊断、隐式放行 | 配置、能力报告与错误映射 | 需部署证据，不能由请求编译证明 |
| C14 特权、设备等 | 部分能力由扩展与报告表达 | 按部署及任务能力拒绝或接受 |
| C15 镜像 | 按 digest 引用 | probe 可测 |
| C16 多容器实例 | 未实现 | 依赖该能力的任务应拒绝 |

上表中的实现不代表任意 OpenSandbox 部署自动满足平台契约。网络策略模式、存储挂载、鉴权和 runtime 会改变实际行为。

## 3 接口映射

### 3.1 生命周期与执行通道

路径相对于 `<server>/v1`。

| `Platform` 方法 | API |
|---|---|
| `create` | `POST /sandboxes` |
| `get` / `delete` | `GET` / `DELETE /sandboxes/{id}` |
| `list` | `GET /sandboxes`，标签条件编码在单个 `metadata` 参数中，逐页取全 |
| `renew` | `POST /sandboxes/{id}/renew-expiration` |
| `exec` / `start_process` | execd `POST /command` |
| `process_status` | execd `GET /command/status/{id}` |
| `write_file` / `read_file` | execd 文件接口，按协议选择 |
| `link` | 对每个成员 `PUT /sandboxes/{id}/networkpolicy`，整体替换策略 |

默认工厂把 execd 请求路由到 `/sandboxes/{id}/proxy/44772`。平台类也接受自定义的 `execd_http(iid)` 工厂。
后端不发布任务端口，也不以应用隧道替代平台网络。

### 3.2 实例地址

经执行通道运行 BusyBox `ip route` 与 `ip addr`，读取默认路由网卡的 IPv4 地址。
后端按实例缓存地址；部署必须保证该地址在实例存活期间稳定、组内可达。

### 3.3 创建请求

每个请求包含镜像、占位入口、标签、有限 TTL、资源、卷和初始网络策略。业务环境变量随执行传入，不放进创建请求的 `env`。
`extensions` 透传；`create_fields` 可提供额外字段，但不能覆盖 flotilla 管理的字段。
创建不自动重试，结果不明时由编排核心按标签对账。

### 3.4 资源与 TTL

CPU、内存等值写入 `resourceLimits` 与 `resourceRequests`，TTL 至少 60 秒。
实际限制由平台执行，probe 读取 cgroup 验证内存上限和 CPU 倍数。长期 trial 的续期与失败处理由 core 完成。

### 3.5 错误与重试

兼容平铺的 `{code, message}` 和 FastAPI 的 `{detail: {code, message}}`。
429 映射为限流，容量错误映射为 capacity，5xx 与连接异常映射为 transient，输入与认证错误不自动重试。
仅幂等控制面操作自动退避重试。命令执行、文件覆盖和创建不自动重试。

### 3.6 execd 协议

- `current` 使用 `argv` 和 `/v1/filesystem/{uid}/{gid}/files/*`；
- `legacy` 对旧版 execd 使用安全引用的 `command` 字符串与旧上传接口，必要时补 `chown`；
- 不在命令失败后自动切换协议，避免重复执行非幂等命令；
- 每次执行经 BusyBox `env -i` 清空继承环境，只带回请求中的环境变量；值经 `envs` 传递，不拼接到命令串；
- 上传在内存中构造带 Content-Length 的 multipart body；前台输出按 NDJSON 事件解析；
- BusyBox 必须存在于配置路径。普通单元从 share 获得它，锚点镜像自行携带。

## 4 网络策略

### 4.1 模型

Compose 网络编译为 `Topology`。至少共享一个网络的两个单元为对端；没有共享网络的单元不能互通。
后端使用对端地址生成主机 CIDR，不依赖服务所在节点。

### 4.2 `cidr` 互联

取得全部成员地址后，对每个成员编译其可达对端与外部策略，整体替换平台网络策略。
可达性、跨节点行为与隔离由部署保证，不能由生成正确的 JSON 推断。

### 4.3 支持范围

当前仅支持标准 IP / CIDR 策略，不包含特定平台的实例引用目标。部署必须支持规则优先级与所需的协议、端口语义。

### 4.4 外部策略

`none` 默认拒绝；`allowlist` 合并明确允许的域名和 CIDR；`any` 默认允许外网，但拒绝 sandbox 与平台基础设施网段，再放行组内对端。
`any` 的安全性依赖配置的地址范围完整，也依赖部署实际执行 IP / CIDR 规则。

### 4.5 生效

`link` 返回前，平台必须确认规则已在数据面生效。后端当前直接依赖策略替换接口的完成语义，未用固定等待补偿。
策略应用时机、初始隔离与更新过程需在真实部署上验证。

### 4.6 隐式放行

DNS、元数据或控制面等平台隐式放行的目标必须完整声明。`declared.json` 是声明的载体，不是自动验证结果。

## 5 入站隔离与鉴权

出口策略不能单独证明入站隔离或执行通道鉴权。部署应核对来自其他实例、外部主机和实例内回环的所有路径。
能力报告标出安全缺口，配置默认不接受这些缺口。参见 Platform_Requirements C5、C9 与 Architecture 第 2 节。

## 6 共享存储

每个 `SharedVolume` 编译为一个标准卷，卷名按挂载顺序生成 `v0`、`v1` 等。挂载点必须为绝对路径，卷键与 `root_subpath` 不得含路径穿越。

| 形式 | 请求 | `subPath` |
|---|---|---|
| `host` | `{"name": "vN", "host": {"path": host_path}, …}` | 卷键；空卷键（共享根目录本身）不写 |
| `pvc` | `{"name": "vN", "pvc": {"claimName": claim_name, "createIfNotExists": false}, …}` | `root_subpath/卷键`；空卷键时为 `root_subpath`，两者都空时不写 |

两种形式都是上游 OpenSandbox 的卷 schema：每个卷一个唯一的 `name` 与恰好一个后端结构。`createIfNotExists` 固定为 `false`，
卷名写错时创建失败，而不是由服务端新建一个空卷。同一 claim 的多个引用在上游 K8s 运行时合成一个 pod volume，`readOnly` 按挂载各自生效。

部署前提：

- `host`：部署允许给定 host 路径，各 worker 上该路径指向同一共享存储；
- `pvc`：claim 已存在，可被所有 worker 上的实例同时读写挂载（K8s 中为 `ReadWriteMany`）；
- 只读、单文件挂载、缺失目录行为与跨节点共享都需要实际验证。

已发布的清单以 `storage_root`（`host:<host_path>` 或 `pvc:<claim_name>[/<root_subpath>]`）记录任务文件所在的共享根目录（Architecture 4.7 节）。

## 7 当前限制

完整的 provider、镜像构建、任务发布、share 发布与 Harbor 适配尚未实现。Pool 模式和多容器实例不在当前支持范围内。
测试覆盖请求形状与编排行为，不等于完成真实平台认证或规模验收。

## 8 验证

### 8.1 单元测试

`tests/unit/test_opensandbox_*` 使用 MockTransport 测协议、编译与调用路由；编排核心使用 FakePlatform 和 ManualClock。
这些测试不需要平台账号。

### 8.2 部署探测

先准备共享目录、share 发布和镜像，填写 `deployments/examples/` 的模板，再运行 `flotilla probe`。
首版 probe 测单服务所需性质的一个子集，未覆盖项来自部署声明；来源随能力报告保存。

### 8.3 后续验收

后续补齐多服务互通、入站隔离、DNS、策略生效、共享卷、取消与残留清理的真实部署验收，并公开可复现的运行方法。
真实部署的凭证、标识、诊断日志与原始能力报告不属于源码。
