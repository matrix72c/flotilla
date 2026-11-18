"""Image metadata parsing and Compose identity resolution."""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.build.images import BuildSettings, ImageError, plan_image
from flotilla.build.meta import parse_config, parse_user
from flotilla.compose.model import Project, Service

# ───────────────────────────── USER -> numeric ─────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1000", (1000, 0)), ("1000:1001", (1000, 1001)), ("0:0", (0, 0))],
)
def test_parse_user_numeric_and_root(spec: str, expected: tuple[int, int]) -> None:
    assert parse_user(spec) == expected


def test_parse_user_name_needs_lookup() -> None:
    with pytest.raises(ImageError, match="/etc/passwd"):
        parse_user("appuser")
    assert parse_user("appuser", lookup=lambda _: (1500, 1600)) == (1500, 1600)
    # Named groups need a lookup too, and gid comes from the resolved gid.
    assert parse_user("1000:staff", group_lookup=lambda _: 8) == (1000, 8)


def test_parse_user_rejects_empty() -> None:
    with pytest.raises(ImageError):
        parse_user(":1000")


# ───────────────────────────── image config ─────────────────────────────


def test_parse_config_fields() -> None:
    m = parse_config(
        {
            "Entrypoint": ["/entrypoint.sh"],
            "Cmd": ["/bin/bash"],
            "User": "1000:1001",
            "WorkingDir": "/app",
            "Healthcheck": {
                "Test": ["CMD-SHELL", "curl -f localhost || exit 1"],
                "Interval": 5_000_000_000,
                "Timeout": 2_000_000_000,
                "Retries": 10,
            },
        }
    )
    assert m.entrypoint == ("/entrypoint.sh",) and m.cmd == ("/bin/bash",)
    assert (m.uid, m.gid, m.workdir) == (1000, 1001, "/app")
    assert m.healthcheck is not None
    assert m.healthcheck.test == ("/bin/sh", "-c", "curl -f localhost || exit 1")
    assert (m.healthcheck.interval_s, m.healthcheck.timeout_s, m.healthcheck.retries) == (5.0, 2.0, 10)
    assert m.healthcheck.start_period_s == 0.0 and m.healthcheck.start_interval_s == 5.0  # missing uses Docker defaults


def test_parse_config_defaults_and_none_healthcheck() -> None:
    m = parse_config({})
    assert (m.entrypoint, m.cmd, m.uid, m.gid, m.workdir) == (None, None, 0, 0, "/")
    assert m.healthcheck is None
    # The image explicitly disables the healthcheck, and the CMD form.
    assert parse_config({"Healthcheck": {"Test": ["NONE"]}}).healthcheck is None
    cmd = parse_config({"Healthcheck": {"Test": ["CMD", "/health.sh", "-v"]}}).healthcheck
    assert cmd is not None and cmd.test == ("/health.sh", "-v")


def test_parse_config_empty_entrypoint_differs_from_missing() -> None:
    # An empty list means "the image cleared ENTRYPOINT", unlike missing (None, inherited).
    assert parse_config({"Entrypoint": []}).entrypoint == ()
    assert parse_config({}).entrypoint is None


# ───────────────────────────── Content hash ─────────────────────────────


# ───────────────────────────── Build key ─────────────────────────────


def _project(**services: Any) -> Project:
    return Project(services={n: Service(name=n, **kw) for n, kw in services.items()}, networks={}, volumes=frozenset())


# ───────────────────────────── Public image replacement ─────────────────────────────

REPLACEMENTS = {
    "ubuntu:24.04": "registry.example.com/ns/ubuntu:24.04",
    "docker.io/library/redis:7": "registry.example.com/ns/redis:7",
}
SETTINGS = BuildSettings(pullable_registries=("registry.example.com",), image_replacements=REPLACEMENTS)


def test_replacement_applies_before_the_pullable_check() -> None:
    svc = Service(name="cache", image="docker.io/library/redis:7")
    assert plan_image("cache", svc, SETTINGS).ref.name == "registry.example.com/ns/redis"
    plain = BuildSettings(pullable_registries=("registry.example.com",))
    with pytest.raises(ImageError, match="not pullable"):
        plan_image("cache", svc, plain)


def test_named_root_uses_the_image_database() -> None:
    with pytest.raises(ImageError, match="/etc/passwd"):
        parse_user("root")
    assert parse_user("root", lookup=lambda _: (1234, 5678)) == (1234, 5678)
    with pytest.raises(ImageError, match="group database"):
        parse_user("1000:staff", lookup=lambda _: (7, 8))
