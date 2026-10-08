"""平台后端：`Platform` 协议（`base`）及其实现。

命名约定：本包与 stdlib `platform` 同名。src 布局 + 绝对导入下无冲突——包内一律写
`from flotilla.platform.base import ...`；需要标准库时写 `import platform`（py3 绝对导入默认
拿到标准库）。禁止用相对导入跳到本包的 `platform`，禁止 `import platform` 指代本包。
"""

from __future__ import annotations
