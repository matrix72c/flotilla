"""Compose interpolation and normalization."""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.compose.interpolate import InterpolationError, interpolate
from flotilla.compose.model import Dependency, Healthcheck, NetworkAttachment, RuntimeExpression, VolumeMount
from flotilla.compose.normalize import KEEPALIVE, harbor_overlay, normalize
from flotilla.compose.units import parse_duration
from flotilla.platform.base import TmpfsMount

# ───────────────────────────── Interpolation ─────────────────────────────


@pytest.mark.parametrize(
    ("text", "variables", "expected", "unresolved"),
    [
        ("${A}", {"A": "1"}, "1", {}),
        ("x${A}y", {"A": ""}, "xy", {}),
        ("${A:-d}", {"A": ""}, "d", {}),  # :- also uses the default for an empty string
        ("${A-d}", {"A": ""}, "", {}),  # - uses the default only when unset
        ("${A:-d}", {}, "d", {"A": "d"}),
        ("${A}", {}, "", {"A": None}),
        ("${A}/${A:-fallback}", {}, "/fallback", {"A": None}),
        ("${A:-fallback}/${A}", {}, "fallback/", {"A": None}),
        ("$${HOSTNAME}", {"HOSTNAME": "h"}, "${HOSTNAME}", {}),  # $$ is a literal $, not interpolated
        ("a$$b", {}, "a$b", {}),
        ("${A:-http://127.0.0.1:9}", {}, "http://127.0.0.1:9", {"A": "http://127.0.0.1:9"}),
        ("${A:+x}", {"A": "1"}, "x", {}),
        ("${A:+x}", {"A": ""}, "", {}),  # :+ needs a nonempty value
        ("${A+x}", {"A": ""}, "x", {}),  # + only needs the variable to be set
        ("${A:+x}", {}, "", {"A": ""}),  # optional: unset yields the empty string
        ("${P:+${P},}web", {"P": "a"}, "a,web", {}),  # nested reference inside the alternative
        ("${P:+${P},}web", {}, "web", {"P": ""}),
        ("${A:+${B}}", {}, "", {"A": ""}),  # an unused alternative records nothing
        ("${A:-${B:-z}}", {}, "z", {"A": "z", "B": "z"}),
    ],
)
def test_interpolate_strings(text: str, variables: dict[str, str], expected: str, unresolved: dict[str, Any]) -> None:
    got = interpolate(text, variables)
    assert got.value == expected
    assert got.unresolved == unresolved


@pytest.mark.parametrize("text", ["$A", "${A:?err}", "${}", "${1A}", "${A", "${A:-${B}", "${A:+$B}"])
def test_interpolate_rejects_unsupported(text: str) -> None:
    with pytest.raises(InterpolationError):
        interpolate(text, {})


def test_references_include_unused_alternatives_but_not_escaped_literals() -> None:
    result = interpolate("${A:+${B}}/$${LITERAL}", {})
    assert result.value == "/${LITERAL}" and result.unresolved == {"A": ""}
    assert result.references == {"A", "B"}


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


# ───────────────────────────── Harbor keep-alive override ─────────────────────────────


def test_overlay_adds_keepalive_main_with_prebuilt_image() -> None:
    merged = harbor_overlay({"services": {"db": {"image": "postgres"}}}, main_image="reg/x@sha256:1", main_build=False)
    assert merged["services"]["main"] == {"command": list(KEEPALIVE), "image": "reg/x@sha256:1"}
    assert merged["services"]["db"] == {"image": "postgres"}


def test_task_compose_overrides_overlay() -> None:
    # Same as `docker compose -f base -f task`: the task's explicit command wins; mappings merge recursively.
    task = {"services": {"main": {"command": ["bash"], "environment": {"A": "1"}}}}
    merged = harbor_overlay(task, main_image=None, main_build=True)
    assert merged["services"]["main"] == {"command": ["bash"], "build": {"context": "."}, "environment": {"A": "1"}}


# ───────────────────────────── Normalization ─────────────────────────────


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
    assert app.command == ("run", "--fast")  # Compose shell words
    assert app.depends_on == {"db": Dependency()}
    assert app.environment == {"A": "1", "B": "x=y"}
    assert app.networks == {"back": NetworkAttachment(aliases=("api",))}
    assert p.services["main"].networks == {"default": NetworkAttachment()}  # no networks declared -> default
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
        ({"image": "a", "volumes": ["./x:/x:unsupported"]}, "services.app.volumes[0]"),
        ({"image": "a", "volumes": [{"type": "bind", "source": "/etc", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "volumes": [{"type": "bind", "source": "../up", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "volumes": [{"type": "volume", "source": "ghost", "target": "/x"}]}, "services.app.volumes[0]"),
        ({"image": "a", "read_only": "true"}, "services.app.read_only"),
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
        ({"image": "a", "command": "${NOPE}"}, "services.app"),  # unset variable outside environment
        # inheriting the image's healthcheck test is not implemented
        ({"image": "a", "healthcheck": {"interval": "5s"}}, "services.app.healthcheck"),
        ({"build": {"context": ".", "ssh": ["default"]}}, "services.app.build.ssh"),
        ({}, "services.app"),  # neither image nor build
    ],
)
def test_rejections_report_field_path(svc: dict[str, Any], path: str) -> None:
    p = _norm({"app": svc})
    assert path in _paths(p, "reject"), p.findings


def test_read_only_is_preserved_for_every_service() -> None:
    proxy = {"image": "kali", "entrypoint": ["python3", "/opt/model-api-proxy/proxy.py"], "read_only": True}
    p = _norm({"model-api-proxy": proxy})
    assert not p.rejected and p.services["model-api-proxy"].readonly_rootfs
    p = _norm({"model-api-proxy": {**proxy, "entrypoint": ["sh", "-c", "write-things"]}})
    assert not p.rejected and p.services["model-api-proxy"].readonly_rootfs


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
        "services.app.ports",
        "services.app.networks.default.gw_priority",
        "services.app.depends_on.main.restart",
    } <= _paths(p, "equivalent")
    assert "services.app.hostname" in _paths(p, "warn")
    app = p.services["app"]
    assert (app.restart, tuple(m.mount_path for m in app.tmpfs), app.udp_ports, app.hostname) == (
        "always",
        ("/tmp",),
        (53,),
        "kafka",
    )
    assert "services.app.tmpfs" not in _paths(p, "equivalent")


def test_unsized_tmpfs_keeps_docker_semantics_until_planning() -> None:
    p = _norm({"app": {"image": "a", "tmpfs": ["/tmp", "/cache:size=8m"]}})
    assert not p.rejected
    assert p.services["app"].tmpfs == (TmpfsMount("/tmp"), TmpfsMount("/cache", 8 * 1024**2))


def test_runtime_params_recorded_from_environment() -> None:
    p = _norm({"proxy": {"image": "p", "environment": {"UPSTREAM": "${EXAMPLE_API_UPSTREAM:-http://127.0.0.1:9}"}}})
    assert not p.rejected
    assert p.runtime_params == {"proxy": {"UPSTREAM": RuntimeExpression("${EXAMPLE_API_UPSTREAM:-http://127.0.0.1:9}")}}
    assert p.services["proxy"].environment == {"UPSTREAM": "http://127.0.0.1:9"}  # default now, overridden at runtime


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
