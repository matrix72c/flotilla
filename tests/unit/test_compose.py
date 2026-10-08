"""Compose 插值与规范化（§4.2 第 1 步、§3.5、§4.6）。"""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.compose.interpolate import InterpolationError, interpolate
from flotilla.compose.model import Dependency, Healthcheck, NetworkAttachment, VolumeMount
from flotilla.compose.normalize import KEEPALIVE, harbor_overlay, normalize
from flotilla.compose.units import parse_duration

# ───────────────────────────── 插值 ─────────────────────────────


@pytest.mark.parametrize(
    ("text", "variables", "expected", "unresolved"),
    [
        ("${A}", {"A": "1"}, "1", {}),
        ("x${A}y", {"A": ""}, "xy", {}),
        ("${A:-d}", {"A": ""}, "d", {}),  # :- 空串也取默认
        ("${A-d}", {"A": ""}, "", {}),  # - 只在未设置时取默认
        ("${A:-d}", {}, "d", {"A": "d"}),
        ("${A}", {}, "", {"A": None}),
        ("$${HOSTNAME}", {"HOSTNAME": "h"}, "${HOSTNAME}", {}),  # $$ 是字面 $，不插值
        ("a$$b", {}, "a$b", {}),
        ("${A:-http://127.0.0.1:9}", {}, "http://127.0.0.1:9", {"A": "http://127.0.0.1:9"}),
    ],
)
def test_interpolate_strings(text: str, variables: dict[str, str], expected: str, unresolved: dict[str, Any]) -> None:
    got = interpolate(text, variables)
    assert got.value == expected
    assert got.unresolved == unresolved


@pytest.mark.parametrize("text", ["$A", "${A:?err}", "${A:+x}", "${}", "${1A}"])
def test_interpolate_rejects_unsupported(text: str) -> None:
    with pytest.raises(InterpolationError):
        interpolate(text, {})


def test_interpolate_recurses_but_not_into_keys() -> None:
    got = interpolate({"${K}": ["${A}", {"b": "${B:-2}"}], "n": 3}, {"A": "1"})
    assert got.value == {"${K}": ["1", {"b": "2"}], "n": 3}
    assert got.unresolved == {"B": "2"}


@pytest.mark.parametrize(
    ("value", "seconds"), [("5s", 5.0), ("1m30s", 90.0), ("500ms", 0.5), ("2h", 7200.0), (10, 10.0), ("1.5s", 1.5)]
)
def test_parse_duration(value: object, seconds: float) -> None:
    assert parse_duration(value, path="x") == seconds


@pytest.mark.parametrize("value", ["", "5", "5x", "s5", True, None, "1m-3s"])
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value, path="x")


# ───────────────────────────── Harbor 保活覆盖 ─────────────────────────────


def test_overlay_adds_keepalive_main_with_prebuilt_image() -> None:
    merged = harbor_overlay({"services": {"db": {"image": "postgres"}}}, main_image="reg/x@sha256:1", main_build=False)
    assert merged["services"]["main"] == {"command": list(KEEPALIVE), "image": "reg/x@sha256:1"}
    assert merged["services"]["db"] == {"image": "postgres"}


def test_task_compose_overrides_overlay() -> None:
    # 与 `docker compose -f base -f task` 一致：任务显式的 command 优先；映射递归合并。
    task = {"services": {"main": {"command": ["bash"], "environment": {"A": "1"}}}}
    merged = harbor_overlay(task, main_image=None, main_build=True)
    assert merged["services"]["main"] == {"command": ["bash"], "build": {"context": "."}, "environment": {"A": "1"}}


# ───────────────────────────── 规范化 ─────────────────────────────


def _norm(services: dict[str, Any], **top: Any) -> Any:
    doc = {"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top}
    return normalize(doc, {})


def _paths(project: Any, kind: str) -> set[str]:
    return {f.path for f in project.findings if f.kind == kind}


def test_short_forms_expand() -> None:
    p = _norm(
        {
            "db": {"image": "pg", "networks": ["back"]},
            "app": {
                "image": "app",
                "command": "run --fast",
                "depends_on": ["db"],
                "environment": ["A=1", "B=x=y"],
                "networks": {"back": {"aliases": ["api"]}},
            },
        },
        networks={"back": {}},
    )
    assert not p.rejected
    app = p.services["app"]
    assert app.command == ("/bin/sh", "-c", "run --fast")  # shell 形式（§3.5）
    assert app.depends_on == {"db": Dependency()}
    assert app.environment == {"A": "1", "B": "x=y"}
    assert app.networks == {"back": NetworkAttachment(aliases=("api",))}
    assert p.services["main"].networks == {"default": NetworkAttachment()}  # 未声明 networks → default
    assert set(p.networks) == {"back", "default"}


def test_network_mode_none_means_no_networks() -> None:
    p = _norm({"init": {"image": "i", "network_mode": "none"}})
    assert p.services["init"].networks == {}
    assert not p.rejected


@pytest.mark.parametrize(
    ("test", "argv"),
    [
        (["CMD", "curl", "-f", "http://x"], ("curl", "-f", "http://x")),
        (["CMD-SHELL", "pg_isready || exit 1"], ("/bin/sh", "-c", "pg_isready || exit 1")),
        ("pg_isready", ("/bin/sh", "-c", "pg_isready")),
    ],
)
def test_healthcheck_test_forms(test: object, argv: tuple[str, ...]) -> None:
    p = _norm(
        {"db": {"image": "pg", "healthcheck": {"test": test, "interval": "2s", "retries": 30, "start_period": "1m"}}}
    )
    assert p.services["db"].healthcheck == Healthcheck(test=argv, interval_s=2.0, retries=30, start_period_s=60.0)


@pytest.mark.parametrize("hc", [{"disable": True}, {"test": ["NONE"]}])
def test_healthcheck_disabled(hc: dict[str, Any]) -> None:
    p = _norm({"db": {"image": "pg", "healthcheck": hc}})
    assert p.services["db"].healthcheck is None and p.services["db"].healthcheck_disabled


def test_volumes_long_syntax() -> None:
    p = _norm(
        {
            "app": {
                "image": "a",
                "volumes": [
                    {"type": "volume", "source": "data", "target": "/data", "volume": {"nocopy": True}},
                    {"type": "bind", "source": "./conf", "target": "/etc/conf", "read_only": True, "bind": {}},
                ],
            }
        },
        volumes={"data": {}},
    )
    assert p.services["app"].volumes == (
        VolumeMount("volume", "data", "/data", nocopy=True),
        VolumeMount("bind", "./conf", "/etc/conf", read_only=True),
    )


@pytest.mark.parametrize(
    ("svc", "path"),
    [
        ({"image": "a", "volumes": ["./x:/x"]}, "services.app.volumes[0]"),  # 短语法卷未实现
        ({"image": "a", "volumes": [{"type": "bind", "source": "/etc", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "volumes": [{"type": "bind", "source": "../up", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "volumes": [{"type": "volume", "source": "ghost", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "read_only": True}, "services.app.read_only"),
        ({"image": "a", "pid": "service:main"}, "services.app.pid"),
        ({"image": "a", "network_mode": "host"}, "services.app.network_mode"),
        ({"image": "a", "network_mode": "service:main"}, "services.app.network_mode"),
        ({"image": "a", "cap_drop": ["ALL"]}, "services.app.cap_drop"),
        ({"image": "a", "frobnicate": 1}, "services.app.frobnicate"),
        ({"image": "a", "networks": {"ghost": {}}}, "services.app.networks.ghost"),
        (
            {"image": "a", "networks": {"default": {"ipv4_address": "10.0.0.5"}}},
            "services.app.networks.default.ipv4_address",
        ),
        ({"image": "a", "environment": {"FROM_HOST": None}}, "services.app.environment.FROM_HOST"),
        ({"image": "a", "depends_on": {"ghost": {"condition": "service_started"}}}, "services.app.depends_on.ghost"),
        ({"image": "a", "command": "${NOPE}"}, "services.app"),  # 非 environment 中的未提供变量
        ({"image": "a", "healthcheck": {"interval": "5s"}}, "services.app.healthcheck"),  # 沿用镜像 test 未实现
        ({"build": {"context": ".", "ssh": ["default"]}}, "services.app.build.ssh"),
        ({}, "services.app"),  # 既没有 image 也没有 build
    ],
)
def test_rejections_report_field_path(svc: dict[str, Any], path: str) -> None:
    p = _norm({"app": svc})
    assert path in _paths(p, "reject"), p.findings


def test_known_proxy_read_only_is_equivalent_but_others_rejected() -> None:
    proxy = {"image": "kali", "entrypoint": ["python3", "/opt/model-api-proxy/proxy.py"], "read_only": True}
    p = _norm({"model-api-proxy": proxy})
    assert "services.model-api-proxy.read_only" in _paths(p, "equivalent") and not p.rejected
    # 同名但入口不同：仍拒绝（按服务名 + 入口精确匹配）。
    p = _norm({"model-api-proxy": {**proxy, "entrypoint": ["sh", "-c", "write-things"]}})
    assert "services.model-api-proxy.read_only" in _paths(p, "reject")


@pytest.mark.parametrize(
    ("network", "path", "kind"),
    [
        ({"driver": "bridge", "internal": True, "ipam": {}}, None, None),
        ({"driver_opts": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}}, "driver_opts", "equivalent"),
        ({"driver_opts": {"com.docker.network.bridge.enable_icc": "false"}}, "driver_opts", "reject"),
        ({"driver": "overlay"}, "driver", "reject"),
        ({"ipam": {"config": [{"subnet": "10.5.0.0/16"}]}}, "ipam", "reject"),
        ({"external": True}, "external", "reject"),
    ],
)
def test_network_definitions(network: dict[str, Any], path: str | None, kind: str | None) -> None:
    p = _norm({"app": {"image": "a", "networks": ["n"]}}, networks={"n": network})
    if path is None:
        assert not p.findings and p.networks["n"].internal == bool(network.get("internal"))
    else:
        assert any(f.path.startswith(f"networks.n.{path}") and f.kind == kind for f in p.findings), p.findings


def test_equivalent_fields_are_recorded() -> None:
    p = _norm(
        {
            "app": {
                "image": "a",
                "init": True,
                "restart": "always",
                "tmpfs": ["/tmp"],
                "ports": [{"target": 53, "protocol": "udp"}, {"target": 80, "protocol": "tcp"}],
                "networks": {"default": {"gw_priority": 1}},
                "depends_on": {"main": {"condition": "service_started", "restart": True}},
                "hostname": "kafka",
            }
        }
    )
    assert not p.rejected
    assert {
        "services.app.init",
        "services.app.restart",
        "services.app.tmpfs",
        "services.app.ports",
        "services.app.networks.default.gw_priority",
        "services.app.depends_on.main.restart",
    } <= _paths(p, "equivalent")
    assert "services.app.hostname" in _paths(p, "warn")
    app = p.services["app"]
    assert (app.restart, app.tmpfs, app.udp_ports, app.hostname) == ("always", ("/tmp",), (53,), "kafka")


def test_runtime_params_recorded_from_environment() -> None:
    p = _norm({"proxy": {"image": "p", "environment": {"UPSTREAM": "${CB2TB_API_UPSTREAM:-http://127.0.0.1:9}"}}})
    assert not p.rejected
    assert p.runtime_params == {
        "proxy": {"UPSTREAM": ("${CB2TB_API_UPSTREAM:-http://127.0.0.1:9}", "http://127.0.0.1:9")}
    }
    assert p.services["proxy"].environment == {"UPSTREAM": "http://127.0.0.1:9"}  # 默认值先代入，运行时再覆盖


def test_build_args_proxy_defaults_are_allowed() -> None:
    p = _norm({"app": {"build": {"context": ".", "args": {"HTTP_PROXY": "${HTTP_PROXY:-}"}}}})
    assert not p.rejected
    assert p.services["app"].build is not None and p.services["app"].build.args == {"HTTP_PROXY": ""}


def test_missing_main_is_rejected() -> None:
    p = normalize({"services": {"db": {"image": "pg"}}}, {})
    assert "services" in _paths(p, "reject")


def test_unknown_top_level_key_rejected() -> None:
    p = normalize({"services": {"main": {"image": "m"}}, "secrets": {}}, {})
    assert "secrets" in _paths(p, "reject")
