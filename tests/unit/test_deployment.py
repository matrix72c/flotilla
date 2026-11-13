"""Deployment: the single consumer entry point for platform-side initialization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from flotilla import Flotilla, OpenOptions
from flotilla.deployment import Deployment, RuntimeLayers, memory_gib
from flotilla.manifest import Context, Manifest, Resources, Unit
from flotilla.native import DatasetRow, ReleaseWriter, Task, Verification, read_dataset
from flotilla.prepared import PreparedEnvironment

IMAGE = "example.invalid/unit@sha256:" + "c" * 64


def unit(cpu: str = "1", memory: str = "128Mi") -> Unit:
    return Unit(
        image=IMAGE,
        resources=Resources(cpu=cpu, memory=memory),
        context=Context(env={}, uid=1000, gid=1000, cwd="/"),
        command=None,
        mounts=[],
    )


def build_release(base: Path, *, name: str = "rel", services: int = 1, cpu: str = "1", shared: bool = False) -> Path:
    tests = base / "tests"
    tests.mkdir(parents=True)
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    key = "ds/task-a"
    units = {"main": unit(cpu)} | {f"svc{i}": unit(cpu) for i in range(1, services)}
    directory = ReleaseWriter.directory(key)
    roles = {"main": Manifest(task=key, agent_service="main", units=units)}
    if not shared:
        roles["verifier"] = Manifest(task=f"{key}#verifier", agent_service="main", units={"main": unit()})
    task = Task(
        key=key,
        roles=roles,
        verification=Verification(
            role="main" if shared else "verifier",
            command=("bash", "/tests/test.sh"),
            tests=f"{directory}/tests",
            output_paths={"reward.txt": "/logs/verifier/reward.txt"},
        ),
    )
    with ReleaseWriter(base / "release", name) as writer:
        writer.add_task(task, "Solve the task.\n", {"tests": tests})
        writer.publish([{"task": key}])
    return base / "release"


def write_deployment(root: Path, release: Path, *, runtime: str = "", extra: str = "") -> Path:
    (root / "capabilities.json").write_text('{"report": "fixture"}\n')
    path = root / "deployment.toml"
    path.write_text(
        '[opensandbox]\nendpoint="http://fixture.test"\ncredential_env="TEST_CREDENTIAL"\n'
        'capabilities="capabilities.json"\n[opensandbox.network]\nlink="sandbox"\n'
        '[storage]\nhost_path="/srv/shared"\n'
        '[reaper]\nanchor_image="fixture/anchor:1"\n'
        f'[task_releases.rel]\nlocal_root="{os.path.relpath(release, root)}"\nplatform_root="releases/rel"\n'
        + runtime
        + extra
    )
    return path


RUNTIME = (
    '[inputs.runtimes]\nhost_path="@user:owner"\nroot_subpath="runtimes"\n'
    '[agent_runtime]\ninput="runtimes"\n'
    '[agent_runtime.layers.node]\nplatform_root="node/n1"\n'
    '[agent_runtime.layers.lagent]\nplatform_root="lagent/l1"\n'
)


@pytest.fixture
def released(tmp_path: Path) -> tuple[Path, Path]:
    release = build_release(tmp_path / "data")
    return write_deployment(tmp_path, release, runtime=RUNTIME), release


def test_credentials_bind_only_the_platform_variable(
    tmp_path: Path, released: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = released
    monkeypatch.delenv("TEST_CREDENTIAL", raising=False)
    monkeypatch.delenv("CONSUMER_KEY", raising=False)
    secrets = tmp_path / "private.env"
    secrets.write_text("TEST_CREDENTIAL=ak:sk\nCONSUMER_KEY=other\n")
    Deployment.load(path, credentials=secrets)
    assert os.environ["TEST_CREDENTIAL"] == "ak:sk"
    assert "CONSUMER_KEY" not in os.environ
    monkeypatch.setenv("TEST_CREDENTIAL", "different")
    with pytest.raises(ValueError, match="conflicts"):
        Deployment.load(path, credentials=secrets)
    secrets.write_text("CONSUMER_KEY=other\n")
    monkeypatch.delenv("TEST_CREDENTIAL")
    with pytest.raises(ValueError, match="does not set TEST_CREDENTIAL"):
        Deployment.load(path, credentials=secrets)


def test_budgets_and_limits(released: tuple[Path, Path]) -> None:
    deployment = Deployment.load(released[0])
    config = deployment.config
    t = config.timeouts
    assert deployment.setup_budget() == t.create + t.address + t.wire + t.hosts + t.prepare + t.start_max
    assert deployment.run_timeout() == config.runtime.run_timeout_s
    assert deployment.trial_concurrency == config.limits.trial_concurrency
    assert deployment.instance_budget == config.limits.instance_budget
    overhead = deployment.client_overhead()
    assert overhead["cpu"] > 0 and overhead["memory_gib"] > 0


def test_dataset_peak_keeps_duplicates_and_is_local(
    released: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = released
    hot = build_release(tmp_path / "hot", name="hot", services=3, cpu="2")
    path.write_text(
        path.read_text()
        + f'[task_releases.hot]\nlocal_root="{os.path.relpath(hot, tmp_path)}"\nplatform_root="releases/hot"\n'
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("capacity inspection must not create attempts")

    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt.create", forbidden)
    rows = [*read_dataset(released[1] / "dataset.jsonl")] * 2 + read_dataset(hot / "dataset.jsonl")
    peak = Deployment.load(path).dataset_peak(rows)
    assert (peak.records, peak.services, peak.cpu) == (3, 3, 6.0)
    assert peak.reserved_instances == 3 * 2 and peak.reserved_cpu == 12.0
    with pytest.raises(ValueError, match="empty dataset"):
        Deployment.load(path).dataset_peak([])


@pytest.mark.parametrize("value,expected", [("512Mi", 0.5), ("1.5Gi", 1.5), ("2g", 2), ("1073741824", 1)])
def test_memory_units(value: str, expected: float) -> None:
    assert memory_gib(value) == expected


def test_runtime_layers_and_mounts(released: tuple[Path, Path]) -> None:
    deployment = Deployment.load(released[0])
    layers = deployment.runtime_layers()
    assert layers.root == "runtimes"
    assert layers.root_identity == deployment.storage_identity("runtimes") != deployment.storage_identity()
    assert dict(layers.keys) == {"node": "node/n1", "lagent": "lagent/l1"}
    manifest = Manifest(task="t", agent_service="work", units={"work": unit(), "db": unit()})
    prepared = PreparedEnvironment(manifest)
    options = deployment.runtime_options(
        prepared, layers, {"node": "/opt/a/node", "lagent": "/opt/a/lagent"}, request_id="one"
    )
    assert options.request_id == "one"
    (volumes,) = options.external_mounts.values()
    assert list(options.external_mounts) == ["work"]
    assert [(v.key, v.mount_path, v.read_only, v.root) for v in volumes] == [
        ("node/n1", "/opt/a/node", True, "runtimes"),
        ("lagent/l1", "/opt/a/lagent", True, "runtimes"),
    ]
    with pytest.raises(ValueError, match="exactly the runtime layers"):
        deployment.runtime_options(prepared, layers, {"node": "/opt/a/node"})
    moved = RuntimeLayers(root=layers.root, root_identity="elsewhere", keys=layers.keys)
    with pytest.raises(ValueError, match="does not match"):
        deployment.runtime_options(prepared, moved, {"node": "/n", "lagent": "/l"})


def test_runtime_layers_require_a_section(tmp_path: Path) -> None:
    path = write_deployment(tmp_path, build_release(tmp_path / "data"))
    with pytest.raises(ValueError, match="no \\[agent_runtime\\]"):
        Deployment.load(path).runtime_layers()


def test_freeze_is_portable(released: tuple[Path, Path], tmp_path: Path) -> None:
    path, release = released
    frozen = Deployment.load(path).freeze(tmp_path / "frozen")
    assert frozen == tmp_path / "frozen" / "deployment.json"
    assert (tmp_path / "frozen" / "capabilities.json").read_text() == '{"report": "fixture"}\n'
    data = json.loads(frozen.read_text())
    assert data["opensandbox"]["capabilities"] == "capabilities.json"
    assert data["task_releases"]["rel"]["local_root"] == str(release.absolute())
    restored = Deployment.load(frozen)
    assert restored.config.capabilities_path() == tmp_path / "frozen" / "capabilities.json"
    assert restored.runtime_layers() == Deployment.load(path).runtime_layers()
    row = DatasetRow(release="rel", task="ds/task-a")
    assert restored.release_bindings().resolve(row).instruction() == "Solve the task.\n"


class FakeAttempt:
    opened: ClassVar[list[str]] = []

    @classmethod
    async def create(cls, fleet: Any, bound: Any) -> FakeAttempt:
        return cls()

    async def open(self, role: str, options: Any) -> Any:
        FakeAttempt.opened.append(role)

        async def check() -> None:
            return None

        return SimpleNamespace(check=check)

    async def release(self, role: str) -> None:
        return None

    async def close(self) -> None:
        return None


async def _clean_close(self: Any, timeout: float | None = None) -> Any:
    return SimpleNamespace(clean=True)


@pytest.mark.asyncio
async def test_validate_reports_rows_and_never_opens_the_verifier(
    released: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt", FakeAttempt)
    monkeypatch.setattr(Flotilla, "close", _clean_close)
    FakeAttempt.opened = []
    seen: list[str] = []

    def options(role: str, prepared: PreparedEnvironment) -> Any:
        seen.append(role)
        return OpenOptions()

    good = {"release": "rel", "task": "ds/task-a"}
    rows = [good, {"release": "rel", "task": "ds/missing"}, {"task_key": "legacy"}, good]
    results = await Deployment.load(released[0]).validate(rows, sandboxes=True, options=options)
    assert [r.passed for r in results] == [True, False, False, True]
    assert results[0].task == "rel:ds/task-a"
    assert results[0].roles == [{"role": "main", "uid": 1000, "gid": 1000}]
    assert len(results[0].environments) == 2  # both roles are still sized for admission
    assert results[2].error is not None and "ValidationError" in results[2].error
    assert FakeAttempt.opened == ["main", "main"] and seen == ["main", "main"]


@pytest.mark.asyncio
async def test_validate_starts_main_when_it_is_also_the_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt", FakeAttempt)
    monkeypatch.setattr(Flotilla, "close", _clean_close)
    FakeAttempt.opened = []
    path = write_deployment(tmp_path, build_release(tmp_path / "data", shared=True))
    (result,) = await Deployment.load(path).validate([{"release": "rel", "task": "ds/task-a"}], sandboxes=True)
    assert result.passed and FakeAttempt.opened == ["main"]
