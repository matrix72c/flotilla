"""Compile explicit peer and external network policies."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.platform.base import ErrorCategory, ExternalPolicy, FlotillaError

IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class NetworkSettings:
    """Deployment config `[opensandbox.network]`."""

    sandbox_cidrs: tuple[str, ...] = ()  # platform sandbox address ranges for cidr mode
    platform_cidrs: tuple[str, ...] = ()  # platform infrastructure (nodes, service ranges, metadata addresses, etc.)
    max_rules: int = 4096  # OPENSANDBOX_EGRESS_MAX_RULES
    link: Literal["cidr", "sandbox"] = "cidr"

    def __post_init__(self) -> None:
        if self.link not in ("cidr", "sandbox"):
            raise ValueError("link must be cidr or sandbox")
        if self.link == "cidr" and not self.sandbox_cidrs:
            raise ValueError("link = cidr requires sandbox_cidrs")
        for c in (*self.sandbox_cidrs, *self.platform_cidrs):
            ipaddress.ip_network(c, strict=False)  # raises ValueError if invalid
        if self.max_rules < 1:
            raise ValueError("max_rules must be >= 1")


def compile_policy(
    external: ExternalPolicy,
    peers: Iterable[str],
    settings: NetworkSettings,
) -> dict[str, Any]:
    """Compile the full `networkPolicy`. `peers` are peer IPs (cidr) or raw instance IDs (sandbox), empty at create.

    Raises `invalid` when the rule count exceeds `max_rules` (at compile time, without sending a request).
    """
    if settings.link == "sandbox":
        if external.mode == "any":
            raise _invalid("link = sandbox does not support external = any; use an external allowlist")
        rules = [{"action": "allow", "target": _sandbox_target(p)} for p in sorted(set(peers))]
        if external.mode == "allowlist":
            rules += _allowlist_rules(external, settings)
        policy = {"defaultAction": "deny", "egress": rules}
    elif external.mode == "any":
        peer_nets = sorted({_host_net(p) for p in peers}, key=_sort_key)
        if not settings.platform_cidrs:
            raise _invalid("external = any requires the deployment setting platform_cidrs")
        sandbox = _nets(settings.sandbox_cidrs)
        denied = _exclude(sandbox, peer_nets) + _nets(settings.platform_cidrs)
        rules = [{"action": "deny", "target": str(n)} for n in _collapse(denied)]
        policy = {"defaultAction": "allow", "egress": rules}
    else:
        peer_nets = sorted({_host_net(p) for p in peers}, key=_sort_key)
        rules = [{"action": "allow", "target": str(n)} for n in peer_nets]
        if external.mode == "allowlist":
            rules += _allowlist_rules(external, settings)
        policy = {"defaultAction": "deny", "egress": rules}
    if len(policy["egress"]) > settings.max_rules:
        raise _invalid(
            f"networkPolicy rule count {len(policy['egress'])} exceeds the deployment limit {settings.max_rules}"
        )
    return policy


def _allowlist_rules(external: ExternalPolicy, settings: NetworkSettings) -> list[dict[str, str]]:
    """External allowlist. cidr mode excludes sandbox ranges; in sandbox mode IP rules only cover external addresses."""
    rules = [{"action": "allow", "target": host} for host in sorted(set(external.hosts))]
    sandbox = _nets(settings.sandbox_cidrs) if settings.link == "cidr" else []
    for cidr in sorted(set(external.cidrs)):
        net = ipaddress.ip_network(cidr, strict=False)
        if net.prefixlen == 0:
            raise _invalid(
                f"allowlist contains catch-all external {cidr}: DNS would be denied; use external = any instead"
            )
        rules += [{"action": "allow", "target": str(n)} for n in _exclude([net], sandbox)]
    return rules


def _sandbox_target(iid: str) -> str:
    # Exact targets only from real instance IDs; no group targets, wildcards, or already-prefixed strings.
    if iid in ("any", "project") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", iid):
        raise _invalid("sandbox linking requires concrete instance IDs, not group targets or IPs")
    return f"sandbox:{iid}"


def _exclude(base: Sequence[IPNet], holes: Sequence[IPNet]) -> list[IPNet]:
    """Ranges left after removing `holes` from each `base` range (only within the same family)."""
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
