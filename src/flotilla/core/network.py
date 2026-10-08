"""Compose 网络 → `Topology`，每个单元的 `ExternalPolicy`（Architecture 第 6 节）。

只在语义层面描述：同网成员任意端口可达、不共享网络即不可达（§6.2），出站按 `internal` 与调用方策略（§6.4）。
规则、对端、网段怎么落到平台上是后端的事——这里不出现任何平台概念（§16.2）。
"""

from __future__ import annotations

from flotilla.core.plan import TrialPlan
from flotilla.platform.base import ExternalPolicy, Topology

_NONE = ExternalPolicy(mode="none")


def topology(plan: TrialPlan) -> Topology | None:
    """本 trial 的拓扑；`link` 成员少于两个时为 None（不调用 `link`，初始隔离即最终状态，§6.2）。

    成员是至少在一个网络里的单元；空网络不进入拓扑。
    """
    networks = {name: net.members for name, net in plan.networks.items() if net.members}
    if len(link_members(plan)) < 2:
        return None
    return Topology(networks=networks)


def link_members(plan: TrialPlan) -> frozenset[str]:
    """`link` 的成员：至少在一个网络里的单元（`network_mode: none` 的单元不在任何网络中）。"""
    return frozenset().union(*(net.members for net in plan.networks.values()))


def external_policy(plan: TrialPlan, unit: str, caller: ExternalPolicy) -> ExternalPolicy:
    """单元的外部策略（§6.4）：在 `external_units` 中的按调用方策略 `caller`，其余（只在 internal 网络中、
    `network_mode: none`）为 `none`。

    `external_units` 由清单给出；这里再按网络核对一次，防止翻译把只在 internal 网络中的单元放出去。
    """
    if unit not in plan.external_units:
        return _NONE
    if not any(unit in net.members and not net.internal for net in plan.networks.values()):
        raise ValueError(f"{unit} 在 external_units 中，但不在任何非 internal 网络里")
    return caller
