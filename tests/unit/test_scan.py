"""Offline scan: findings about a task's own files, before anything is planned or read."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.platform.base import Capabilities
from flotilla.scan import scan


def _project(services: dict[str, Any], **top: Any) -> Any:
    return normalize({"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top})


def _rejects(findings: tuple[Any, ...]) -> set[str]:
    return {f.reason for f in findings if f.kind == "reject"}


def test_accepts_a_plain_project(caps: Capabilities) -> None:
    assert not _rejects(scan(_project({"db": {"image": "pg"}}), caps))


def test_share_mount_conflict(caps: Capabilities) -> None:
    svc = {"a": {"image": "a", "working_dir": "/.flotilla/x"}}
    assert _rejects(scan(_project(svc), caps))


@pytest.mark.parametrize("kind", ["volume", "bind"])
def test_read_only_tmp_is_rejected(caps: Capabilities, kind: str) -> None:
    source = "data" if kind == "volume" else "./data"
    mount = {"type": kind, "source": source, "target": "/tmp"}
    top: dict[str, Any] = {"volumes": {"data": {}}} if kind == "volume" else {}
    assert not _rejects(scan(_project({"a": {"image": "a", "volumes": [mount]}}, **top), caps))
    result = scan(_project({"a": {"image": "a", "volumes": [{**mount, "read_only": True}]}}, **top), caps)
    assert "/tmp must stay writable; mount it read-write" in _rejects(result)
    nested = {"type": kind, "source": source, "target": "/tmp/pwn", "read_only": True}
    assert not _rejects(scan(_project({"a": {"image": "a", "volumes": [nested]}}, **top), caps))


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
    r = scan(_project(services), caps, env_dir=str(tmp_path))
    assert any("missing.jar" in x and "empty directory" in x for x in _rejects(r))
    plain = {"a": {"image": "a", "volumes": [bind("missing.jar")]}}
    assert _rejects(scan(_project(plain), caps, env_dir=str(tmp_path)))
    # Deployment lacks file mounts: a read-only single-file bind that does exist is still rejected.
    nofile = {"a": {"image": "a", "volumes": [bind("conf.yml")]}}
    assert _rejects(scan(_project(nofile), replace(caps, volume_file=False), env_dir=str(tmp_path)))


def test_compose_rejections_carry_through(caps: Capabilities) -> None:
    assert _rejects(scan(_project({"a": {"image": "a", "pid": "service:main"}}), caps))


def test_undeclared_network_is_reported(caps: Capabilities) -> None:
    findings = scan(_project({"main": {"image": "m", "networks": ["missing"]}}), caps)
    assert any(f.path == "services.main.networks.missing" for f in findings)


def test_extra_findings_are_carried(caps: Capabilities) -> None:
    from flotilla.compose.model import Finding

    findings = scan(_project({}), caps, extra=(Finding("task.toml:environment.network_mode", "reject", "nope"),))
    assert any(f.path == "task.toml:environment.network_mode" for f in findings)
