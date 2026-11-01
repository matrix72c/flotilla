"""Scanner: deployment-related checks (section 13)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.platform.base import Capabilities
from flotilla.scan import scan


def _project(services: dict[str, Any], **top: Any) -> Any:
    return normalize({"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top}, {})


def _rejects(result: Any) -> set[str]:
    return {f.reason for f in result.findings if f.kind == "reject"}


def test_accepts_plain_multi_service(caps: Capabilities) -> None:
    r = scan("t", _project({"db": {"image": "pg", "restart": "always"}}), caps)
    assert r.status == "accepted"
    assert r.restart_services == ("db",)
    assert r.external_units == ("db", "main")


def test_multi_service_needs_link(caps: Capabilities) -> None:
    r = scan("t", _project({"db": {"image": "pg"}}), replace(caps, link=False))
    assert r.status == "rejected" and any("link" in x for x in _rejects(r))
    # Single-service tasks are unaffected; network_mode: none services are not network members.
    assert scan("t", _project({"init": {"image": "i", "network_mode": "none"}}), replace(caps, link=False)).status == (
        "accepted"
    )


@pytest.mark.parametrize("internal", [False, True])
def test_offline_services_reject_non_dns_exceptions(caps: Capabilities, internal: bool) -> None:
    project = _project(
        {} if internal else {"init": {"image": "i", "network_mode": "none"}},
        networks={"default": {"internal": internal}},
    )
    dns = replace(caps, cluster_dns=("10.96.0.10",), implicit_egress=("10.96.0.10:53/udp,tcp",))
    assert scan("t", project, dns).status == "accepted"
    extra = replace(dns, implicit_egress=(*dns.implicit_egress, "10.1.1.1:443/tcp"))
    assert scan("t", project, extra).status == "rejected"


def test_none_needs_ingress_but_no_network_preserves_peer_links(caps: Capabilities) -> None:
    caps = replace(caps, inbound_isolation=False)
    project = _project({"init": {"image": "i", "network_mode": "none"}})
    assert scan("t", project, caps).status == "rejected"
    assert scan("t", project, replace(caps, sandbox_ingress=True)).status == "accepted"
    internal = _project({"db": {"image": "pg"}}, networks={"default": {"internal": True}})
    result = scan("t", internal, caps)
    assert result.status == "accepted" and result.external_units == ()


def test_too_many_members(caps: Capabilities) -> None:
    services = {f"s{i}": {"image": "x"} for i in range(10)}
    r = scan("t", _project(services), caps)  # 10 + main = 11 > 10
    assert r.status == "rejected"


def test_udp_needs_link_udp(caps: Capabilities) -> None:
    svc = {"dns": {"image": "d", "ports": [{"target": 53, "protocol": "udp"}]}}
    assert scan("t", _project(svc), caps).status == "rejected"
    assert scan("t", _project(svc), replace(caps, link_udp=True)).status == "accepted"


def test_privileged_needs_runtime(caps: Capabilities) -> None:
    svc = {"k": {"image": "k", "privileged": True}}
    assert scan("t", _project(svc), caps).status == "rejected"
    r = scan("t", _project(svc), replace(caps, privileged_runtime=True))
    assert r.status == "accepted" and any(f.kind == "warn" for f in r.findings)


def test_share_mount_conflict(caps: Capabilities) -> None:
    svc = {"a": {"image": "a", "working_dir": "/.flotilla/x"}}
    assert scan("t", _project(svc), caps).status == "rejected"


@pytest.mark.parametrize("kind", ["volume", "bind"])
def test_read_only_tmp_is_rejected(caps: Capabilities, kind: str) -> None:
    source = "data" if kind == "volume" else "./data"
    mount = {"type": kind, "source": source, "target": "/tmp"}
    top: dict[str, Any] = {"volumes": {"data": {}}} if kind == "volume" else {}
    assert scan("t", _project({"a": {"image": "a", "volumes": [mount]}}, **top), caps).status == "accepted"
    result = scan("t", _project({"a": {"image": "a", "volumes": [{**mount, "read_only": True}]}}, **top), caps)
    assert "/tmp must stay writable; mount it read-write" in _rejects(result)
    nested = {"type": kind, "source": source, "target": "/tmp/pwn", "read_only": True}
    assert scan("t", _project({"a": {"image": "a", "volumes": [nested]}}, **top), caps).status == "accepted"


def test_bind_sources(caps: Capabilities, tmp_path: Path) -> None:
    (tmp_path / "conf.yml").write_text("x")
    (tmp_path / "dir").mkdir()

    def bind(source: str, **extra: Any) -> dict[str, Any]:
        return {"type": "bind", "source": source, "target": f"/{source}", "read_only": True, **extra}

    services = {
        "a": {
            "image": "a",
            "volumes": [
                bind("conf.yml"),
                bind("dir"),
                bind("missing.jar", bind={"create_host_path": True}),
            ],
        }
    }
    # A missing source is rejected even with create_host_path: the platform mounts an empty
    # directory over the target, so the task runs with empty files and grades as unsolvable.
    r = scan("t", _project(services), caps, env_dir=str(tmp_path))
    assert r.status == "rejected"
    assert any("missing.jar" in x and "empty directory" in x for x in _rejects(r))
    plain = {"a": {"image": "a", "volumes": [bind("missing.jar")]}}
    assert scan("t", _project(plain), caps, env_dir=str(tmp_path)).status == "rejected"
    # Deployment lacks file mounts: a read-only single-file bind that does exist is still rejected.
    nofile = {"a": {"image": "a", "volumes": [bind("conf.yml")]}}
    assert scan("t", _project(nofile), replace(caps, volume_file=False), env_dir=str(tmp_path)).status == "rejected"


def test_compose_rejections_carry_through(caps: Capabilities) -> None:
    r = scan("t", _project({"a": {"image": "a", "pid": "service:main"}}), caps)
    assert r.status == "rejected"


def test_undeclared_network_is_reported_without_aborting_scan(caps: Capabilities) -> None:
    result = scan("t", _project({"main": {"image": "m", "networks": ["missing"]}}), caps)
    assert result.status == "rejected"
    assert any(f.path == "services.main.networks.missing" for f in result.findings)
    assert result.external_units == ()


@pytest.mark.parametrize("udp", [False, True])
def test_runtime_params_listed(caps: Capabilities, udp: bool) -> None:
    p = _project({"proxy": {"image": "p", "environment": {"U": "${EXAMPLE_API_UPSTREAM:-x}"}}})
    # Report the parameter name (provider runtime_params are keyed by it), not the service variable name.
    assert scan("t", p, replace(caps, link_udp=udp)).runtime_params == ("EXAMPLE_API_UPSTREAM",)
