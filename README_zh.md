# Flotilla

[![CI](https://github.com/matrix72c/flotilla/actions/workflows/ci.yml/badge.svg)](https://github.com/matrix72c/flotilla/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)

[English](README.md)

**一次准备可搬迁的多服务任务，在沙盒平台反复执行隔离的 attempt。**

Flotilla 拥有评测和 RL 消费方共用的原生任务协议。可选 Harbor importer 在 CPU 准备主机上解析 Compose、固定镜像、提取静态文件和卷 seed。用户将生成的不可变任务包发布到平台存储。执行时每个服务对应一个沙盒，按显式网络策略连接，并在结束后清理私有状态；不再次解析 Harbor/Compose、构建镜像或从镜像导出 seed。

## 开始使用

支持 **Linux x86_64 + Python 3.12**。发行包、导入名和 CLI 统一为 `flotilla`。从本仓库的固定 commit 或对应 wheel 安装，**不要直接运行 `pip install flotilla`**：公共 PyPI 同名包属于其他项目。迁移时使用干净环境，不与拥有同名 Python 模块的 `flotilla-compose` 混装。

主机侧 Harbor 导入需要 `harbor` extra、固定版本的官方 Compose 工具及镜像准备工具。原生执行和 Anchor 不需要 Harbor、Compose、Docker builder 或 GPU 栈。安装后的 `flotilla anchor build` 使用包内资源构建 Anchor；必须提供固定 digest 的 Python 基础镜像，显式 `--push` 才发布。

## 四阶段流程

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
flotilla select --host-root ./tasks --deployment deployment.toml \
  --out selected.jsonl --report selection-report.json
flotilla prepare selected.jsonl --deployment deployment.toml \
  --out ./release --name example --report prepare-report.json \
  --image-repo registry.example.org/team/tasks
```

1. **probe** 测量平台能力、安全属性与清理结果。不能以选择某个文件系统协议代替实测。
2. **select** 只读取和归一化主机输入，不创建沙盒、不构建、不分词、不切分训练/评测集。
3. **prepare** 固化原始应用身份、固定镜像和静态资产。需要时构建并发布最小 `FROM <digest>` / `USER 0:0` 适配镜像，但保留原应用 UID/GID、环境、命令和健康检查。tmpfs 与只读根文件系统仍是真实运行时能力，不因缺少空目录无条件派生镜像。
4. 用户用现有存储工具发布完整任务包，配置显式路径绑定，再由 Harbor、Voyager 或 Python API **消费**。Flotilla 不内置任务包上传工具链。

```toml
[task_releases.example]
local_root = "./release"
platform_root = "/shared/releases/example"
```

本地相对路径相对 deployment 文件解析。平台路径受配置的 host/PVC 存储根限制，不猜测或随意剥离前缀。凭据保存在包外，运行时显式注入；报告不属于 release。

```text
release/
  dataset.jsonl
  tasks/<task-key>/task.json
  tasks/<task-key>/instruction.md
  tasks/<task-key>/files/...
  tasks/<task-key>/tests/...
```

release 就是这棵目录树，没有中央清单，也不按内容哈希寻址。`dataset.jsonl` 每行是 `release`（prepare `--name` 给的名字，对应 deployment 的 `[task_releases.<name>]`）、`task`（`tasks/` 下的目录）、annotations 和非秘密参数，顺序与重复不丢失。消费时只读 `task.json` 和 `instruction.md`，不做哈希校验；缺少文件时直接报错，不回退到原始 Harbor 目录或 builder。

## Python 示例

```python
import asyncio

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt


async def main():
    config = FlotillaConfig.load("deployment.toml")
    bindings = config.release_bindings()
    row = bindings.read_dataset("release/dataset.jsonl")[0]
    async with Flotilla(deployment=config) as fleet:
        async with await NativeAttempt.create(fleet, bindings.resolve(row)) as attempt:
            env = await attempt.open("main", OpenOptions(startup_timeout=600, lifetime=300))
            result = await env.service("main").exec(["id"], timeout=30)
            print(result.stdout.decode())
            verification = await attempt.verify()
            print(verification)
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


asyncio.run(main())
```

隐藏测试只在验证阶段提供；独立 verifier 与显式产物交接由同一原生生命周期处理。基础设施与清理失败是错误，不是任务分数。reward 解释由消费方负责。

## 特性与文档

- **提前检查 Compose 语义**：[支持范围](docs/Compose_Support.md)，不重新实现完整 Compose。
- **显式隔离**：任务网络和部署出站策略共同约束通信；未被证明的安全属性须显式接受。
- **确认式清理**：创建响应丢失时对账，确认删除后才释放配额；`flotilla gc` 处理遗留资源。
- **不静默重放命令**：不能确认远端进程停止时报告 `ExecOutcomeUnknown`。
- **统一并发管理**：复用现有准入、限流、资源、网络和卷调度，不另造调度器。
- **可插拔后端**：内置 OpenSandbox，其他平台通过 entry point 接入。

推荐先读 [原生任务协议与工作流](docs/Native_Tasks.md)。其他参考：

- [运行时 API](docs/Runtime_API.md)
- [配置参考](docs/Configuration.md)、[部署指南](docs/Deployment.md)
- [平台要求](docs/Platform_Requirements.md)、[网络验证](docs/Network_Validation.md)
- [OpenSandbox](docs/backends/opensandbox.md)
- [编写后端](docs/Writing_a_Backend.md)

旧 task-key/`env`/`task_dir` 行需要重新 select/prepare；旧 `prepare` 预热行为只通过明确的 `legacy_warmup` 库接口保留。旧作业继续使用原 commit、镜像和快照，不机械改写历史名称。离线测试、安装包验证、真实平台验收与 GPU 训练分别记录，不能互相替代。

## 状态与贡献

Flotilla 是 alpha 软件，1.0 前次版本可能改变 API。可选 Harbor 消费适配器需要提供 source/runtime 接口的固定 patched Harbor，原版 Harbor 0.24.0 不具备这些接口。参见 [变更日志](CHANGELOG.md)、[贡献指南](CONTRIBUTING.md) 与[行为准则](CODE_OF_CONDUCT.md)。安全漏洞按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

Flotilla 采用 [Apache License 2.0](LICENSE)。Anchor 包含 GPLv2 许可的 BusyBox，参见[第三方组件](THIRD_PARTY.md)。
