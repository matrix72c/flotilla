"""外部策略与组内互联编译为 `networkPolicy`（backends/opensandbox.md 第 4 节）。纯函数。

一个实例的 `networkPolicy` 同时表达外部策略与组内对端：创建时只按外部策略编译（对端未知），`link` 时以
"外部策略 + 对端"重新编译**完整**策略并整体替换（§4.5），所以 `link` 不改变外部策略。

两种形式（§4.2），同一份策略中不同时出现 `allow` 与 `deny` 目标——结果不依赖平台对规则优先级的定义：

| 形式 | 外部策略 | `defaultAction` | 规则 |
|---|---|---|---|
| 窄 | `none`、`allowlist` | `deny` | `allow` 对端 /32；`allowlist` 的域名与（减去 sandbox 网段后的）CIDR |
| 任意 | `any` | `allow` | `deny`（sandbox 网段 − 对端）拆出的 CIDR 组，加 `platform_cidrs` |

本模块实现 `cidr` 策略（对端以实例 IP 表达，§4.1）。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from flotilla.platform.base import ErrorCategory, ExternalPolicy, FlotillaError

IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class NetworkSettings:
    """部署配置 `[opensandbox.network]`（§1）。"""

    sandbox_cidrs: tuple[str, ...]  # 平台分配 sandbox 地址的网段
    platform_cidrs: tuple[str, ...] = ()  # 平台自身基础设施（节点、service 网段、元数据地址等）
    max_rules: int = 4096  # OPENSANDBOX_EGRESS_MAX_RULES

    def __post_init__(self) -> None:
        for c in (*self.sandbox_cidrs, *self.platform_cidrs):
            ipaddress.ip_network(c, strict=False)  # 不合法时抛 ValueError
        if self.max_rules < 1:
            raise ValueError("max_rules 须 >= 1")


def compile_policy(
    external: ExternalPolicy,
    peers: Iterable[str],
    settings: NetworkSettings,
) -> dict[str, Any]:
    """编译一个实例的完整 `networkPolicy`。`peers` 是对端内部 IP（创建时为空）。

    规则数超过 `max_rules` 时抛 `invalid`（编译期报错，不发出请求，§4.2）。
    """
    peer_nets = sorted({_host_net(p) for p in peers}, key=_sort_key)
    if external.mode == "any":
        if not settings.platform_cidrs:
            raise _invalid("external = any 需要部署配置 platform_cidrs（§4.4）")
        sandbox = _nets(settings.sandbox_cidrs)
        denied = _exclude(sandbox, peer_nets) + _nets(settings.platform_cidrs)
        rules = [{"action": "deny", "target": str(n)} for n in _collapse(denied)]
        policy = {"defaultAction": "allow", "egress": rules}
    else:
        rules = [{"action": "allow", "target": str(n)} for n in peer_nets]
        if external.mode == "allowlist":
            rules += _allowlist_rules(external, settings)
        policy = {"defaultAction": "deny", "egress": rules}
    if len(policy["egress"]) > settings.max_rules:
        raise _invalid(f"networkPolicy 规则数 {len(policy['egress'])} 超过部署上限 {settings.max_rules}")
    return policy


def _allowlist_rules(external: ExternalPolicy, settings: NetworkSettings) -> list[dict[str, str]]:
    """窄列表：域名作为域名规则；CIDR 先减去 sandbox 网段（不能借外部规则放行组外实例）。"""
    rules = [{"action": "allow", "target": host} for host in sorted(set(external.hosts))]
    sandbox = _nets(settings.sandbox_cidrs)
    for cidr in sorted(set(external.cidrs)):
        net = ipaddress.ip_network(cidr, strict=False)
        if net.prefixlen == 0:
            raise _invalid(f"窄列表中出现任意外网写法 {cidr}：DNS 会被拒（§4.2），请改用 external = any")
        rules += [{"action": "allow", "target": str(n)} for n in _exclude([net], sandbox)]
    return rules


def _exclude(base: Sequence[IPNet], holes: Sequence[IPNet]) -> list[IPNet]:
    """`base` 各网段挖掉 `holes` 后剩下的网段（同族才相减）。"""
    out: list[IPNet] = []
    for net in base:
        pieces: list[IPNet] = [net]
        for hole in holes:
            if hole.version != net.version:
                continue
            nxt: list[IPNet] = []
            for p in pieces:
                if hole.supernet_of(p):  # type: ignore[arg-type]
                    continue
                if p.supernet_of(hole):  # type: ignore[arg-type]
                    nxt.extend(p.address_exclude(hole))  # type: ignore[arg-type]
                else:
                    nxt.append(p)
            pieces = nxt
        out.extend(pieces)
    return out


def _collapse(nets: Iterable[IPNet]) -> list[IPNet]:
    v4 = [n for n in nets if n.version == 4]
    v6 = [n for n in nets if n.version == 6]
    return [*ipaddress.collapse_addresses(v4), *ipaddress.collapse_addresses(v6)]


def _nets(cidrs: Iterable[str]) -> list[IPNet]:
    return [ipaddress.ip_network(c, strict=False) for c in cidrs]


def _host_net(address: str) -> IPNet:
    ip = ipaddress.ip_address(address)
    return ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}")


def _sort_key(net: IPNet) -> tuple[int, int]:
    return net.version, int(net.network_address)


def _invalid(message: str) -> FlotillaError:
    return FlotillaError(message, stage="create", category=ErrorCategory.INVALID, retryable=False)
