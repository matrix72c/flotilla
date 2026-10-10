"""扫描器：与部署相关的判断（第 13 节）。"""

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
    # 单服务任务不受影响；network_mode: none 的服务不算互联成员。
    assert scan("t", _project({"init": {"image": "i", "network_mode": "none"}}), replace(caps, link=False)).status == (
        "accepted"
    )


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
    r = scan("t", _project(services), caps, env_dir=str(tmp_path))
    assert r.status == "accepted"
    assert any("missing.jar" in f.reason and f.kind == "warn" for f in r.findings)  # 与 Docker 一致：空目录
    # 部署不支持文件挂载：单文件的只读 bind 拒绝。
    assert scan("t", _project(services), replace(caps, volume_file=False), env_dir=str(tmp_path)).status == "rejected"
    # create_host_path: false 且源不存在：拒绝。
    strict = {"a": {"image": "a", "volumes": [bind("missing.jar", bind={"create_host_path": False})]}}
    assert scan("t", _project(strict), caps, env_dir=str(tmp_path)).status == "rejected"


def test_compose_rejections_carry_through(caps: Capabilities) -> None:
    r = scan("t", _project({"a": {"image": "a", "pid": "service:main"}}), caps)
    assert r.status == "rejected"


@pytest.mark.parametrize("udp", [False, True])
def test_runtime_params_listed(caps: Capabilities, udp: bool) -> None:
    p = _project({"proxy": {"image": "p", "environment": {"U": "${CB2TB_API_UPSTREAM:-x}"}}})
    # 报参数名（provider 的 runtime_params 按它给值，§10），不是服务里的变量名。
    assert scan("t", p, replace(caps, link_udp=udp)).runtime_params == ("CB2TB_API_UPSTREAM",)
