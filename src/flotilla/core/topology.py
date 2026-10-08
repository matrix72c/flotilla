"""`depends_on` 分层（Architecture §5.3）。

把单元按依赖拓扑排成若干层：每层的单元只依赖更早层中的单元，可以同时启动。`required: false` 的依赖
同样决定先后（被依赖者存在时要先启动、按条件等待），只是它失败时不阻塞依赖者——由启动阶段处理，不在这里。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from flotilla.core.plan import Dependency


def layers(units: Iterable[str], depends_on: Mapping[str, Mapping[str, Dependency]]) -> list[list[str]]:
    """按依赖分层，层内按名字排序（确定性）。成环时抛 `ValueError`（构建时本应拒绝）。"""
    remaining = set(units)
    deps = {u: set(depends_on.get(u, {})) & remaining for u in remaining}
    done: set[str] = set()
    out: list[list[str]] = []
    while remaining:
        ready = sorted(u for u in remaining if deps[u] <= done)
        if not ready:
            raise ValueError(f"depends_on 成环：{sorted(remaining)}")
        out.append(ready)
        done.update(ready)
        remaining.difference_update(ready)
    return out
