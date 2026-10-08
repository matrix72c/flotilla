"""Compose 解析与规范化（Architecture §4.2 第 1 步、§4.6）；build 与 scan 共用。不调用平台。

支持范围见 Architecture §4.6，其余写法明确拒绝并报出
字段路径——"能构建 ⇔ 扫描通过"的前提是两者走同一份代码（§13）。

- `interpolate`：`${VAR}` / `${VAR:-default}` / `${VAR-default}` 与 `$$` 转义；只用调用方给的变量，不读构建机环境。
- `normalize`：叠加 Harbor 的保活覆盖（§3.5）后展开短语法，得到 `Project`。
"""

from __future__ import annotations
