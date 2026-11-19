"""Admission: what a deployment's capabilities must honor for a plan to open (section 13)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.compose.plan_task import PlanError
from flotilla.core.admission import admission_errors
from flotilla.platform.base import Capabilities
from flotilla.testing.planning import plan_project


def _project(services: dict[str, Any], **top: Any) -> Any:
    return normalize({"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top})


def _errors(project: Any, caps: Capabilities) -> list[str]:
    return list(admission_errors(plan_project(project), caps))


def test_accepts_plain_multi_service(caps: Capabilities) -> None:
    assert not _errors(_project({"db": {"image": "pg"}}), caps)


def test_multi_service_needs_link(caps: Capabilities) -> None:
    errors = _errors(_project({"db": {"image": "pg"}}), replace(caps, link=False))
    assert errors == ["deployment cannot connect this task's services"]
    # Single-service tasks are unaffected; network_mode: none services are not network members.
    assert not _errors(_project({"init": {"image": "i", "network_mode": "none"}}), replace(caps, link=False))


@pytest.mark.parametrize("internal", [False, True])
def test_offline_services_reject_non_dns_exceptions(caps: Capabilities, internal: bool) -> None:
    project = _project(
        {} if internal else {"init": {"image": "i", "network_mode": "none"}},
        networks={"default": {"internal": internal}},
    )
    dns = replace(caps, cluster_dns=("10.96.0.10",), implicit_egress=("10.96.0.10:53/udp,tcp",))
    assert not _errors(project, dns)
    extra = replace(dns, implicit_egress=(*dns.implicit_egress, "10.1.1.1:443/tcp"))
    assert _errors(project, extra)


def test_none_needs_ingress_but_no_network_preserves_peer_links(caps: Capabilities) -> None:
    caps = replace(caps, inbound_isolation=False)
    project = _project({"init": {"image": "i", "network_mode": "none"}})
    assert _errors(project, caps)
    assert not _errors(project, replace(caps, sandbox_ingress=True))
    internal = _project({"db": {"image": "pg"}}, networks={"default": {"internal": True}})
    assert not _errors(internal, caps)
    assert plan_project(internal).external_units == frozenset()


def test_too_many_members(caps: Capabilities) -> None:
    services = {f"s{i}": {"image": "x"} for i in range(10)}
    assert _errors(_project(services), caps)  # 10 + main = 11 > 10


def test_udp_needs_link_udp(caps: Capabilities) -> None:
    svc = {"dns": {"image": "d", "ports": [{"target": 53, "protocol": "udp"}]}}
    assert _errors(_project(svc), caps)
    assert not _errors(_project(svc), replace(caps, link_udp=True))


def test_privileged_needs_runtime(caps: Capabilities) -> None:
    svc = {"k": {"image": "k", "privileged": True}}
    assert _errors(_project(svc), caps)
    assert not _errors(_project(svc), replace(caps, privileged_runtime=True))
    readonly = _project({"k": {"image": "k", "privileged": True, "read_only": True}})
    with pytest.raises(PlanError, match="readonly_rootfs"):
        plan_project(readonly)  # readonly rootfs on the privileged runtime is refused at planning


def test_volumes_need_shared_storage(caps: Capabilities) -> None:
    mount = {"type": "volume", "source": "data", "target": "/db"}
    project = _project({"a": {"image": "a", "volumes": [mount]}}, volumes={"data": {}})
    assert not _errors(project, caps)
    assert _errors(project, replace(caps, shared_volume=False))
