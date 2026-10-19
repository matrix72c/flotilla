"""Real public client + background loop + fake platform, with cancellation and resource ownership."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.manifest import Context, Manifest, Resources, Unit
from flotilla.platform.base import ExecResult, ProcessSpec
from flotilla.platform.clock import SystemClock
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import Task
from tests.support.config import config_data
from tests.support.platform import FakePlatform, SharedTree
from tests.support.reports import report


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FlotillaConfig, FakePlatform, SharedTree]:
    caps = report(**{"network.implicit_egress": [], "lifecycle.list_visibility_s": 0.01})
    report_path = tmp_path / "caps.json"
    report_path.write_text(caps.dump_json())
    data = config_data(
        network={"external": []}, limits={"create_rate": 10000, "exec_rate": 10000}, timeouts={"close": 1}
    )
    data["opensandbox"]["capabilities"] = str(report_path)
    busybox = b"fake-busybox"
    blob = json.dumps(
        {
            "release": "test",
            "files": {
                "bin/busybox": {
                    "size": len(busybox),
                    "mode": "0755",
                    "sha256": hashlib.sha256(busybox).hexdigest(),
                }
            },
        }
    ).encode()
    data["share"] = {"release": "test", "release_sha256": hashlib.sha256(blob).hexdigest()}
    cfg = FlotillaConfig.from_mapping(data)
    clock = SystemClock()
    platform = FakePlatform(caps.to_capabilities(), clock=clock)
    platform.initial_files["/etc/hosts"] = b"127.0.0.1 localhost\n"
    tree = SharedTree(platform)
    tree.dirs.update({"", "share", "share/releases", "share/releases/test", "share/releases/test/bin"})
    for path, content in [("share/releases/test/RELEASE.json", blob), ("share/releases/test/bin/busybox", busybox)]:
        tree.files.add(path)
        platform.tree_contents[path] = content
    tree.modes["share/releases/test/bin/busybox"] = "0755"
    tree_exec = platform.exec_handler

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if platform.spec_of(iid).labels.get(labels.ROLE) == labels.ROLE_ANCHOR:
            return tree_exec(iid, proc)
        return ExecResult(0, json.dumps({"argv": proc.argv, "env": dict(proc.env)}).encode(), b"")

    platform.set_exec_handler(execute)

    def manager(config: FlotillaConfig) -> Manager:
        return Manager(config, platform=platform, clock=clock)

    monkeypatch.setattr("flotilla.runtime.client.Manager", manager)
    from tests.support.preparation import fake_build_process

    monkeypatch.setattr("flotilla.preparation.service.Preparer._build_process", fake_build_process)
    return cfg, platform, tree


def task() -> Task:
    manifest = Manifest(
        task="test",
        build_key="a" * 64,
        agent_service="main",
        units={
            "main": Unit(
                image="image@sha256:" + "b" * 64,
                resources=Resources(cpu="1", memory="512Mi"),
                context=Context(env={"PATH": "/bin"}, uid=0, gid=0, cwd="/"),
                command=None,
            )
        },
    )
    return Task(manifest)
