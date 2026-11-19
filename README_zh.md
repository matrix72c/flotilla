# Flotilla

[![CI](https://github.com/matrix72c/flotilla/actions/workflows/ci.yml/badge.svg)](https://github.com/matrix72c/flotilla/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)

[English](README.md)

**把一个多服务 Compose 任务当作隔离的 attempt 在沙盒平台上运行。**

Flotilla 读取已发布的任务目录，把它解析成每服务一个沙盒，按显式网络策略连接，结束后清理自己的私有状态。解析是一次本地计算：只读任务目录自己的文件和各镜像在 registry 里的 config，不写任何之后要读回的文件。没有构建步骤，也没有中间产物需要发布。

## 开始使用

支持 **Linux x86_64 + Python 3.12**。发行包、导入名和 CLI 统一为 `flotilla`。从本仓库的固定 commit 或对应 wheel 安装，**不要直接运行 `pip install flotilla`**：公共 PyPI 同名包属于其他项目。迁移时使用干净环境，不与拥有同名 Python 模块的 `flotilla-compose` 混装。

任务的 `task.toml` 是 Harbor 的格式，所以 `harbor` 是必需依赖；Compose 二进制、Docker 和 GPU 栈都不需要。安装后的 `flotilla anchor build` 使用包内资源构建 Anchor；必须提供固定 digest 的 Python 基础镜像，显式 `--push` 才发布。

## 两件事

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
```

1. **probe** 测量平台能力、安全属性与清理结果，每个部署跑一次。不能以选择某个文件系统协议代替实测。
2. 用现有存储工具把数据集发布到共享存储，再声明它在两侧的位置。中间不生成任何东西，平台挂载的就是你发布的那棵目录树。

```toml
[datasets.example]
local_root = "./tasks"
platform_root = "/shared/datasets/example"
```

两侧都要显式声明，因为任何一侧都推不出另一侧：客户端路径说明不了平台怎么挂这块存储。本地相对路径相对 deployment 文件解析；平台路径受配置的 host/PVC 存储根限制，不猜测或剥离前缀。凭据保存在数据集外，运行时显式注入。

```text
tasks/
  <task-key>/task.toml
  <task-key>/instruction.md
  <task-key>/environment/docker-compose.yaml
  <task-key>/tests/...
```

数据集就是这棵目录树：有 `task.toml` 的目录就是一个任务。镜像必须已发布并按 digest 引用（`repo@sha256:...`）、`USER` 必须是 root——exec daemon 是沙盒的 entrypoint。声明 `build:` 的任务会被拒绝，Flotilla 不构建镜像。

## Python 示例

```python
import asyncio

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt


async def main():
    config = FlotillaConfig.load("deployment.toml")
    dataset = config.resolve_datasets()["example"]
    async with Flotilla(deployment=config) as fleet:
        async with await NativeAttempt.create(fleet, dataset.source("demo/redis")) as attempt:
            env = await attempt.open("main", OpenOptions(startup_timeout=600, lifetime=300))
            result = await env.service("main").exec(["id"], timeout=30)
            print(result.stdout.decode())
            verification = await attempt.verify()
            print(verification)
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


asyncio.run(main())
```

`dataset.tasks()` 按稳定顺序列出所有任务目录，`dataset.source(key)` 给出其中一个在两侧的位置。任务自己的文件按只读挂载、就地读取，不做拷贝；只有可写的卷才按 attempt 物化。隐藏测试只在验证阶段提供；独立 verifier 与显式产物交接由同一生命周期处理。基础设施与清理失败是错误，不是任务分数。reward 解释由消费方负责。

## 特性与文档

- **提前检查 Compose 语义**：[支持范围](docs/Compose_Support.md)。解析器是 Flotilla 自己的，不依赖 `docker compose` 二进制，也不重新实现完整 Compose。
- **显式隔离**：任务网络和部署出站策略共同约束通信；未被证明的安全属性须显式接受。
- **确认式清理**：创建响应丢失时对账，确认删除后才释放配额；`flotilla gc` 处理遗留资源。
- **不静默重放命令**：不能确认远端进程停止时报告 `ExecOutcomeUnknown`。
- **统一并发管理**：复用现有准入、限流、资源、网络和卷调度，不另造调度器。
- **可插拔后端**：内置 OpenSandbox，其他平台通过 entry point 接入。

推荐先读 [原生任务与工作流](docs/Native_Tasks.md)。其他参考：

- [运行时 API](docs/Runtime_API.md)
- [配置参考](docs/Configuration.md)、[部署指南](docs/Deployment.md)
- [平台要求](docs/Platform_Requirements.md)、[网络验证](docs/Network_Validation.md)
- [OpenSandbox](docs/backends/opensandbox.md)
- [编写后端](docs/Writing_a_Backend.md)

## 状态与贡献

Flotilla 是 alpha 软件，1.0 前次版本可能改变 API。可选 Harbor 适配器需要提供 source/runtime 接口的固定 patched Harbor，原版 Harbor 0.24.0 不具备这些接口。参见 [变更日志](CHANGELOG.md)、[贡献指南](CONTRIBUTING.md) 与[行为准则](CODE_OF_CONDUCT.md)。安全漏洞按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

Flotilla 采用 [Apache License 2.0](LICENSE)。Anchor 包含 GPLv2 许可的 BusyBox，参见[第三方组件](THIRD_PARTY.md)。
