"""`/etc/hosts` 块的生成与合并（Architecture §3.7）。

每个单元一份：与本单元至少共享一个网络的每个服务，在这些共享网络上的全部名字 → 该服务的内部地址，
再追加本单元的 `extra_hosts`。平台原有的条目（`localhost`、本机主机名等）保留，flotilla 的条目放在带标记的块中，
写入时只替换这一块。
"""

from __future__ import annotations

from collections.abc import Mapping

from flotilla.core.plan import TrialPlan

BEGIN = "# BEGIN flotilla"
END = "# END flotilla"


def entries(plan: TrialPlan, unit: str, addresses: Mapping[str, str]) -> list[tuple[str, tuple[str, ...]]]:
    """单元的 hosts 条目：(地址, 名字…)，按地址再按名字排序（确定性）。

    名字只来自与本单元共享的网络；不共享任何网络的服务不出现（查询落到平台 DNS，得到 NXDOMAIN，与 Compose 一致）。
    同一名字在多个共享网络上指向同一服务时只出现一次；指向多个服务（副本）时每个地址一条。
    """
    names: dict[str, set[str]] = {}  # 地址 → 名字
    for net in plan.networks.values():
        if unit not in net.members:
            continue
        for host, services in net.names.items():
            for service in services:
                names.setdefault(addresses[service], set()).add(host)
    for host, address in plan.extra_hosts.get(unit, {}).items():
        names.setdefault(address, set()).add(host)
    return [(addr, tuple(sorted(hosts))) for addr, hosts in sorted(names.items())]


def block(lines: list[tuple[str, tuple[str, ...]]]) -> str:
    """带标记的 hosts 块（含结尾换行）。"""
    body = "".join(f"{addr}\t{' '.join(hosts)}\n" for addr, hosts in lines)
    return f"{BEGIN}\n{body}{END}\n"


def merge(original: str, new_block: str) -> str:
    """把 `new_block` 放进原 hosts 内容：已有标记块时替换它，否则追加到末尾；其余行原样保留。"""
    lines = original.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == BEGIN)
        end = next(i for i in range(start, len(lines)) if lines[i].rstrip("\n") == END)
    except StopIteration:
        prefix = original if not original or original.endswith("\n") else original + "\n"
        return prefix + new_block
    return "".join(lines[:start]) + new_block + "".join(lines[end + 1 :])
