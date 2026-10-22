"""Validate declared exceptions to a no-network policy without guessing DNS endpoints."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

_DNS_EGRESS = re.compile(r"(?P<ip>\[[0-9a-fA-F:]+\]|[0-9.]+):53/(?P<protocols>tcp|udp|tcp,udp|udp,tcp)")


def cluster_dns_address(value: str) -> str:
    """Require a concrete resolver IP, not a hostname, subnet, or catch-all address."""
    address = ipaddress.ip_address(value)
    if address.is_unspecified or address.is_multicast:
        raise ValueError("cluster_dns must contain concrete unicast resolver addresses")
    return str(address)


def non_dns_egress(implicit: Iterable[str], cluster_dns: Iterable[str]) -> tuple[str, ...]:
    """Only exact declared resolver IPs on TCP/UDP 53 qualify; unknown syntax fails closed."""
    resolvers = {cluster_dns_address(value) for value in cluster_dns}
    rejected = []
    for entry in implicit:
        match = _DNS_EGRESS.fullmatch(entry)
        if match is not None:
            try:
                address = cluster_dns_address(match["ip"].strip("[]"))
            except ValueError:
                pass
            else:
                if address in resolvers:
                    continue
        rejected.append(entry)
    return tuple(rejected)
