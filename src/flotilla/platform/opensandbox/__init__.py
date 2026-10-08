"""OpenSandbox 后端：在 OpenSandbox 兼容平台上实现 `Platform`（`docs/backends/opensandbox.md`）。

按标准接口编写，部署差异由配置描述（§1）。入口是
`OpenSandboxPlatform`（实现 `platform.base.Platform`）与默认 server 代理组装的 `build()`。
"""

from __future__ import annotations

from flotilla.platform.opensandbox.execd import ExecdSettings
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform, build
from flotilla.platform.opensandbox.storage import StorageSettings

__all__ = ["ExecdSettings", "NetworkSettings", "OpenSandboxPlatform", "StorageSettings", "build"]
