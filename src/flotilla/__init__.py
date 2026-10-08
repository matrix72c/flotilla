"""flotilla：把一个 Compose 环境运行成一组 sandbox（每个服务一个）的编排层。

发布名 `flotilla-compose`，import 名 `flotilla`。设计见 `docs/`（PRD、Architecture、
Platform_Requirements、backends/opensandbox、XTuner_Environment_Design）。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("flotilla-compose")  # hatch-vcs 从 git tag 生成
except PackageNotFoundError:  # 未安装（直接从源码树 import）
    __version__ = "0.0.0+unknown"
