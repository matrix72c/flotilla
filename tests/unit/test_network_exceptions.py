"""DNS exceptions are precise deployment declarations, never inferred from port numbers or prose."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from flotilla.platform.base import Capabilities


@pytest.mark.parametrize(
    ("implicit", "allowed"),
    [
        ((), True),
        (("10.96.0.10:53/udp",), True),
        (("10.96.0.10:53/tcp,udp", "[fd00::a]:53/udp,tcp"), True),
        (("8.8.8.8:53/udp",), False),
        (("10.96.0.10:443/tcp",), False),
        (("10.96.0.10:53/udp", "169.254.169.254:80/tcp"), False),
        (("10.96.0.0/16:53/udp",), False),
        (("kube-dns:53/udp",), False),
        (("10.96.0.10:53/udp (cluster DNS)",), False),
        (("10.96.0.10:53/*",), False),
        (("10.96.0.10:53/udp,tcp,icmp",), False),
        (("10.96.0.10:53/udp\n",), False),
    ],
)
def test_only_declared_dns_is_exempt(caps: Capabilities, implicit: tuple[str, ...], allowed: bool) -> None:
    caps = replace(caps, cluster_dns=("10.96.0.10", "fd00::a"), implicit_egress=implicit)
    assert caps.supports_no_network is allowed
    assert caps.supports_network_none is allowed


def test_dns_requires_explicit_declaration_and_deny_all_support(caps: Capabilities) -> None:
    assert not replace(caps, implicit_egress=("10.96.0.10:53/udp",)).supports_no_network
    assert not replace(caps, external_forms=frozenset({"any"})).supports_no_network


def test_network_none_also_requires_inbound_isolation(caps: Capabilities) -> None:
    caps = replace(caps, inbound_isolation=False)
    assert caps.supports_no_network
    assert not caps.supports_network_none
    assert replace(caps, sandbox_ingress=True).supports_network_none


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "224.0.0.1", "10.96.0.0/16", "kube-dns"])
def test_resolver_declarations_reject_non_host_targets(address: str) -> None:
    from flotilla.testing.config import config

    with pytest.raises(ValidationError):
        config(platform={"cluster_dns": [address]})


def test_implicit_egress_without_a_matching_resolver_declaration_forbids_no_network() -> None:
    """An undeclared address reaching out is not excused by being on port 53."""
    from flotilla.testing.config import config

    declared = config(platform={"implicit_egress": ["10.96.0.10:53/udp"], "cluster_dns": ["10.96.0.10"]})
    assert declared.platform_capabilities().supports_no_network
    undeclared = config(platform={"implicit_egress": ["10.96.0.10:53/udp"]})
    assert not undeclared.platform_capabilities().supports_no_network
