"""编排核心：一个 trial 的创建、放行、名字、启动、健康检查、就绪、释放与回收。

依赖规则（Architecture §16.2）：core 只 import `flotilla.platform.base`，不 import 任何后端；
core 中不出现规则、CIDR、网段等平台概念。时间一律经注入的 `flotilla.platform.base.Clock`，
不直接用 `time` / `asyncio.sleep` / `datetime.now`。
"""

from __future__ import annotations
