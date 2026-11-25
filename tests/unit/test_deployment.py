"""Deployment: the single consumer entry point for platform-side initialization."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.deployment import Deployment, RuntimeLayers, memory_gib
from flotilla.models.plan import ExecContext, TaskSource, TrialPlan, Unit
from flotilla.native.runtime import NativeAttempt
from flotilla.platform.base import Resources
from flotilla.testing.config import importable
from flotilla.testing.planning import FakeConfigs
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

IMAGE = "example.invalid/unit@sha256:" + "c" * 64


def unit(cpu: str = "1", memory: str = "128Mi") -> Unit:
    return Unit(
        image=IMAGE,
        resources=Resources(cpu=cpu, memory=memory),
        context=ExecContext(env={}, uid=1000, gid=1000, cwd="/"),
        command=None,
    )


def build_dataset(base: Path, *, services: int = 1, cpu: str = "1", shared: bool = False) -> Path:
    """A published dataset holding one task, shaped the way flotilla reads one."""
    task = base / "dataset" / "ds" / "task-a"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "instruction.md").write_text("Solve the task.\n")
    config = f'[environment]\ndocker_image = "{IMAGE}"\ncpus = {cpu}\nmemory_mb = 128\n'
    if not shared:
        config += '[verifier]\nenvironment_mode = "separate"\n'
    config += '[verifier]\ncommand = ["bash", "/tests/test.sh"]\n' if shared else ""
    (task / "task.toml").write_text(config)
    if services > 1:
        peers = "".join(
            f'  svc{i}:\n    image: {IMAGE}\n    command: ["/bin/true"]\n    cpus: {cpu}\n    mem_limit: 128Mi\n'
            for i in range(1, services)
        )
        (task / "environment/docker-compose.yaml").write_text(f"services:\n  main: {{}}\n{peers}")
    return base / "dataset"


def write_deployment(dataset: Path, *, runtime: bool = False, datasets: Mapping[str, Path] = {}) -> str:
    """A spec a consumer constructed, published where `Deployment.load` can import it."""
    bindings = {"rel": dataset, **datasets}
    spec: dict[str, Any] = {
        "opensandbox": {
            "endpoint": "http://fixture.test",
            "credential_env": "TEST_CREDENTIAL",
            "network": {"link": "sandbox"},
        },
        "storage": {"host_path": "/srv/shared"},
        "reaper": {"anchor_image": "fixture/anchor:1"},
        "datasets": {
            name: {"local_root": str(path), "platform_root": f"datasets/{name}"} for name, path in bindings.items()
        },
    }
    if runtime:
        spec["inputs"] = {"runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"}}
        spec["agent_runtime"] = {
            "input": "runtimes",
            "layers": {"node": {"platform_root": "node/n1"}, "lagent": {"platform_root": "lagent/l1"}},
        }
    return importable(FlotillaConfig.from_mapping(spec))


@pytest.fixture(autouse=True)
def offline_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planning reads each image's config; these tests are about the deployment, not the registry."""
    original = Flotilla.__init__

    def patched(self: Flotilla, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.image_configs = FakeConfigs()  # type: ignore[assignment]

    monkeypatch.setattr(Flotilla, "__init__", patched)


@pytest.fixture
def published(tmp_path: Path) -> tuple[str, Path]:
    dataset = build_dataset(tmp_path / "data")
    return write_deployment(dataset, runtime=True), dataset


def test_credentials_bind_only_the_platform_variable(
    tmp_path: Path, published: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = published
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


def test_budgets_and_limits(published: tuple[str, Path]) -> None:
    deployment = Deployment.load(published[0])
    config = deployment.config
    t = config.timeouts
    phase_sum = t.create + t.address + t.wire + t.hosts + t.prepare + t.start_max
    assert deployment.setup_budget() == min(phase_sum, config.runtime.startup_timeout_s)
    assert deployment.run_timeout() == config.runtime.run_timeout_s
    assert deployment.trial_concurrency == config.limits.trial_concurrency
    assert deployment.instance_budget == config.limits.instance_budget
    overhead = deployment.client_overhead()
    assert overhead["cpu"] > 0 and overhead["memory_gib"] > 0


@pytest.mark.parametrize("startup_timeout_s", [100.0, 100_000.0])
def test_setup_budget_is_capped_by_the_startup_timeout(published: tuple[str, Path], startup_timeout_s: float) -> None:
    """The phase timeouts sum to the documented worst case, but one open is bounded by the
    runtime's startup timeout; the phase sum only wins when it is the smaller of the two."""
    deployment = Deployment.load(published[0])
    config = deployment.config
    t = config.timeouts
    phase_sum = t.create + t.address + t.wire + t.hosts + t.prepare + t.start_max
    runtime = config.runtime.model_copy(update={"startup_timeout_s": startup_timeout_s})
    bounded = Deployment(config.model_copy(update={"runtime": runtime}))

    assert bounded.setup_budget() == min(phase_sum, startup_timeout_s)


def test_declared_datasets_are_reachable_by_name(published: tuple[str, Path]) -> None:
    deployment = Deployment.load(published[0])
    datasets = deployment.datasets()
    assert sorted(datasets) == ["rel"]
    assert datasets["rel"].tasks() == ["ds/task-a"]
    assert datasets["rel"].source("ds/task-a").key == "datasets/rel/ds/task-a"


@pytest.mark.parametrize("value,expected", [("512Mi", 0.5), ("1.5Gi", 1.5), ("2g", 2), ("1073741824", 1)])
def test_memory_units(value: str, expected: float) -> None:
    assert memory_gib(value) == expected


def test_runtime_layers_and_mounts(published: tuple[str, Path]) -> None:
    deployment = Deployment.load(published[0])
    layers = deployment.runtime_layers()
    assert layers.root == "runtimes"
    assert layers.root_identity == deployment.storage_identity("runtimes") != deployment.storage_identity()
    assert dict(layers.keys) == {"node": "node/n1", "lagent": "lagent/l1"}
    plan = TrialPlan(task="t", task_files=None, agent_service="work", units={"work": unit(), "db": unit()})
    options = deployment.runtime_options(
        plan, layers, {"node": "/opt/a/node", "lagent": "/opt/a/lagent"}, request_id="one"
    )
    assert options.request_id == "one"
    (volumes,) = options.external_mounts.values()
    assert list(options.external_mounts) == ["work"]
    assert [(v.key, v.mount_path, v.read_only, v.root) for v in volumes] == [
        ("node/n1", "/opt/a/node", True, "runtimes"),
        ("lagent/l1", "/opt/a/lagent", True, "runtimes"),
    ]
    with pytest.raises(ValueError, match="exactly the runtime layers"):
        deployment.runtime_options(plan, layers, {"node": "/opt/a/node"})
    moved = RuntimeLayers(root=layers.root, root_identity="elsewhere", keys=layers.keys)
    with pytest.raises(ValueError, match="does not match"):
        deployment.runtime_options(plan, moved, {"node": "/n", "lagent": "/l"})


@pytest.mark.asyncio
async def test_management_anchor_claims_no_task_capabilities_and_leaves_no_launch(
    published: tuple[str, Path],
    platform: FakePlatform,
    clock: ManualClock,
    tree: SharedTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Share publication reaches shared storage through this, and must not look like a launch to gc."""
    monkeypatch.setattr("flotilla.platform.clock.SystemClock", lambda: clock)
    monkeypatch.setattr(FlotillaConfig, "build_platform", lambda *_, **caps: platform)
    monkeypatch.setattr(platform, "aclose", AsyncMock(), raising=False)
    deployment = Deployment.load(published[0])
    caps = deployment.config.management_capabilities()
    assert not (caps.link or caps.shared_volume or caps.privileged_runtime)

    async def use() -> str:
        async with deployment.management_anchor() as anchor:
            assert anchor.iid is not None
            return anchor.launch_id

    launch = await clock.run(use())
    assert launch.startswith("mgmt-")
    assert platform.instances() == []  # the anchor is deleted on exit
    assert not any(key.startswith("launches") for key in tree.dirs | tree.files)


def test_runtime_layers_require_a_section(tmp_path: Path) -> None:
    path = write_deployment(build_dataset(tmp_path / "data"))
    with pytest.raises(ValueError, match="no \\[agent_runtime\\]"):
        Deployment.load(path).runtime_layers()


class FakeAttempt:
    """Real resolution, fake sandboxes: the point is which roles `validate` opens, not that it can."""

    opened: ClassVar[list[str]] = []
    resolve: ClassVar[Any] = NativeAttempt.resolve

    @classmethod
    async def create(cls, fleet: Any, source: Any, options: Any = None) -> FakeAttempt:
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
async def test_validate_reports_tasks_and_never_opens_the_verifier(
    published: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt", FakeAttempt)
    monkeypatch.setattr(Flotilla, "close", _clean_close)
    FakeAttempt.opened = []
    seen: list[str] = []

    def options(role: str, plan: TrialPlan) -> Any:
        seen.append(role)
        return OpenOptions()

    path, dataset = published
    deployment = Deployment.load(path)
    good = deployment.datasets()["rel"].source("ds/task-a")
    broken = TaskSource(local=dataset / "ds" / "absent", key="datasets/rel/ds/absent")
    results = await deployment.validate([good, broken, good], sandboxes=True, options=options)
    assert [r.passed for r in results] == [True, False, True]
    assert results[0].task == "datasets/rel/ds/task-a"
    assert results[0].roles == [{"role": "main", "uid": 0, "gid": 0}]
    assert len(results[0].environments) == 2  # both roles are still sized for admission
    assert results[1].error is not None
    assert FakeAttempt.opened == ["main", "main"] and seen == ["main", "main"]


@pytest.mark.asyncio
async def test_validate_starts_main_when_it_is_also_the_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt", FakeAttempt)
    monkeypatch.setattr(Flotilla, "close", _clean_close)
    FakeAttempt.opened = []
    path = write_deployment(build_dataset(tmp_path / "data", shared=True))
    deployment = Deployment.load(path)
    (result,) = await deployment.validate([deployment.datasets()["rel"].source("ds/task-a")], sandboxes=True)
    assert result.passed and FakeAttempt.opened == ["main"]


def test_dataset_peak_keeps_duplicates_and_is_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ordinary = build_dataset(tmp_path / "data")
    hot = build_dataset(tmp_path / "hot", services=3, cpu="2")
    path = write_deployment(ordinary, datasets={"hot": hot})

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("capacity inspection must not create attempts")

    monkeypatch.setattr("flotilla.native.runtime.NativeAttempt.create", forbidden)
    deployment = Deployment.load(path)
    declared = deployment.datasets()
    sources = [declared["rel"].source("ds/task-a")] * 2 + [declared["hot"].source("ds/task-a")]
    peak = deployment.dataset_peak(sources)
    assert (peak.records, peak.services, peak.cpu) == (3, 3, 6.0)
    assert peak.reserved_instances == 3 * 2 and peak.reserved_cpu == 12.0
    with pytest.raises(ValueError, match="empty dataset"):
        deployment.dataset_peak([])
