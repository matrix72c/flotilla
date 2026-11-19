"""The owning task environment, driven the way Harbor's single-step trial drives it.

Each test builds the world Harbor would see: a deployment naming a published dataset, a task
under that dataset, and the two environment instances a trial creates. The calls follow
Harbor's own order -- agent phase, artifact collection, grading, stop -- so the environment's
registry, handoff, and cleanup are exercised on the paths the real caller takes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from flotilla import Flotilla
from flotilla.config import DatasetBinding, FlotillaConfig
from flotilla.core import labels
from flotilla.integrations import harbor as harbor_integration
from flotilla.integrations.harbor import FlotillaTaskEnvironment, shutdown
from flotilla.models.plan import TaskSource
from flotilla.platform.base import InstanceSpec
from flotilla.testing.attempt import AttemptHarness, prepared_source, write_task
from flotilla.testing.config import importable
from flotilla.testing.planning import DATASET_ROOT, FakeConfigs
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as runtime_deployment

deployment = runtime_deployment

IMAGE = "example.invalid/unit@sha256:" + "c" * 64
World = tuple[str, TaskSource, AttemptHarness]


@pytest.fixture(autouse=True)
def offline_image_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task resolution reads each image's config; these tests are about the Harbor lifecycle."""
    original = Flotilla.__init__

    def patched(self: Flotilla, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.image_configs = FakeConfigs()  # type: ignore[assignment]

    monkeypatch.setattr(Flotilla, "__init__", patched)


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    harbor_integration._registrations.clear()
    harbor_integration._fleets.clear()
    harbor_integration._configs.clear()
    yield
    harbor_integration._registrations.clear()
    harbor_integration._fleets.clear()
    harbor_integration._configs.clear()


def world(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    verifier: bool = False,
    shared_verifier: bool = False,
) -> World:
    """Write and publish one task, then declare its dataset in an importable deployment."""
    cfg, platform, tree = deployment
    harness = AttemptHarness(platform, tree, monkeypatch)
    source = prepared_source(
        tmp_path, platform, tree, name=name, image=IMAGE, verifier=verifier, shared_verifier=shared_verifier
    )
    bound = cfg.model_copy(
        update={"datasets": {name: DatasetBinding(local_root=tmp_path / name, platform_root=name, input=DATASET_ROOT)}}
    )
    return importable(bound), source, harness


def environment(
    target: str,
    source: TaskSource,
    trial: Path,
    session_id: str,
    *,
    environment_dir: str = "environment",
    **kwargs: Any,
) -> FlotillaTaskEnvironment:
    """Build one environment instance the way Harbor's factory would."""
    return FlotillaTaskEnvironment(
        environment_dir=source.local / environment_dir,
        environment_name="task",
        session_id=session_id,
        trial_paths=TrialPaths(trial),
        task_env_config=EnvironmentConfig(docker_image=IMAGE),
        deployment=target,
        **kwargs,
    )


def units(platform: FakePlatform) -> list[str]:
    return [iid for iid in platform.iids() if platform.spec_of(iid).labels.get(labels.ROLE) == labels.ROLE_UNIT]


def volume_of(spec: InstanceSpec, mount_path: str) -> Any:
    matches = [v for v in spec.volumes if v.mount_path == mount_path]
    return matches[0] if matches else None


def leave_answer(harness: AttemptHarness) -> bytes:
    """The agent's graded artifact, on the writable mount the task gave it."""
    answer = f"{harness.workspace_key}/artifacts/agent/answer.txt"
    harness.tree.files.add(answer)
    harness.platform.tree_contents[answer] = b"agent-owned-answer"
    return b"agent-owned-answer"


@pytest.mark.asyncio
async def test_shared_grading_enters_through_the_tests_upload(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, source, harness = world(tmp_path, deployment, monkeypatch, "tb1", shared_verifier=True)
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb1__env")
    await agent.start()
    registration = harbor_integration._registrations[trial]
    assert registration is not None
    harness.workspace_key = registration.attempt.workspace.key
    answer = leave_answer(harness)
    await agent.exec("true")

    # The tests mount is live from the start, but its content stays hidden while the agent works.
    main = harness.platform.spec_of(units(harness.platform)[0])
    tests = volume_of(main, "/tests")
    assert tests is not None and tests.read_only
    assert harness.platform.tree_contents.get(f"{harness.workspace_key}/tests/test.sh") is None

    # Harbor's shared verifier, in the order verify() issues it.
    await agent.exec("rm -f -- /logs/verifier/reward.txt /logs/verifier/reward.json", user="root")
    tests_copy = tmp_path / "tests-copy"
    tests_copy.mkdir()
    (tests_copy / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    await agent.upload_dir(tests_copy, "/tests")  # the handoff: staging supersedes the copy
    await agent.exec("chmod +x /tests/test.sh", user="root")
    with agent.with_default_user("root"):
        await agent.exec("/tests/test.sh > /logs/verifier/test-stdout.txt 2>&1")
    downloaded = tmp_path / "verifier-logs"
    await agent.download_dir("/logs/verifier", downloaded)
    assert (downloaded / "reward.txt").read_bytes() == b"reward:" + answer

    # Staging put the published tests on the hidden mount; the upload itself was superseded.
    assert harness.platform.tree_contents[f"{harness.workspace_key}/tests/test.sh"] is not None
    (spec,) = harness.verification_specs
    assert spec.argv[2].startswith("/tests/test.sh")

    await agent.stop()
    assert trial not in harbor_integration._registrations
    await shutdown()
    assert harness.platform.instances() == []


@pytest.mark.asyncio
async def test_separate_verifier_instance_stages_and_closes(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, source, harness = world(tmp_path, deployment, monkeypatch, "tb2", verifier=True)
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb2__env")
    await agent.start()
    registration = harbor_integration._registrations[trial]
    assert registration is not None
    harness.workspace_key = registration.attempt.workspace.key
    answer = leave_answer(harness)
    await agent.exec("true")

    # Separate mode: harbor stops main for sidecar evidence, then retires the agent environment.
    await agent.stop_service("main")
    await agent.stop()
    assert trial in harbor_integration._registrations  # the attempt waits for its verifier

    verifier = environment(target, source, trial, "tb2__verifier__trial", environment_dir="tests")
    await verifier.start()
    # Bundled tests: no upload; grading runs from the staged mount in the verifier environment.
    await verifier.exec("rm -f -- /logs/verifier/reward.txt /logs/verifier/reward.json", user="root")
    with verifier.with_default_user("root"):
        await verifier.exec("/tests/test.sh > /logs/verifier/test-stdout.txt 2>&1")
    downloaded = tmp_path / "verifier-logs"
    await verifier.download_dir("/logs/verifier", downloaded)
    assert (downloaded / "reward.txt").read_bytes() == b"reward:" + answer

    await verifier.stop()
    assert trial not in harbor_integration._registrations
    await shutdown()
    assert harness.platform.instances() == []


@pytest.mark.asyncio
async def test_stop_closes_an_ungraded_attempt(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier disabled or agent failure: no upload, no grading, and stop still cleans up."""
    target, source, harness = world(tmp_path, deployment, monkeypatch, "tb3", shared_verifier=True)
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb3__env")
    await agent.start()
    harness.workspace_key = harbor_integration._registrations[trial].attempt.workspace.key
    leave_answer(harness)
    await agent.exec("true")
    await agent.stop()
    assert trial not in harbor_integration._registrations
    await shutdown()
    assert harness.platform.instances() == []


@pytest.mark.asyncio
async def test_task_without_verification_stops_clean(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, source, harness = world(tmp_path, deployment, monkeypatch, "tb4")
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb4__env")
    await agent.start()
    await agent.exec("true")
    await agent.stop()
    assert trial not in harbor_integration._registrations
    await shutdown()
    assert harness.platform.instances() == []


@pytest.mark.asyncio
async def test_verifier_without_a_registered_attempt_is_rejected(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regrade or a cross-process verifier finds no attempt this process opened."""
    target, source, _ = world(tmp_path, deployment, monkeypatch, "tb5", verifier=True)
    verifier = environment(target, source, tmp_path / "trial", "tb5__verifier__trial", environment_dir="tests")
    with pytest.raises(RuntimeError, match="no flotilla attempt is registered"):
        await verifier.start()
    await verifier.stop()  # harbor stops the environment even when start failed
    await shutdown()


@pytest.mark.asyncio
async def test_verifier_instance_for_a_shared_task_is_rejected(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grading in the agent environment has no separate verifier instance to hand it to."""
    target, source, _ = world(tmp_path, deployment, monkeypatch, "tb6", shared_verifier=True)
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb6__env")
    await agent.start()
    verifier = environment(target, source, trial, "tb6__verifier__trial", environment_dir="tests")
    with pytest.raises(ValueError, match="grades in the agent environment"):
        await verifier.start()
    await agent.stop()
    await shutdown()


@pytest.mark.asyncio
async def test_task_outside_declared_datasets_is_rejected(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task Harbor copied to its workspace, or a hub/git task, never grades."""
    target, _, harness = world(tmp_path, deployment, monkeypatch, "tb7")
    rogue = write_task(tmp_path / "outside")
    agent = environment(target, TaskSource(local=rogue / "ds/task-a"), tmp_path / "trial", "rogue__env")
    with pytest.raises(ValueError, match="under no dataset the deployment declares"):
        await agent.start()
    await shutdown()
    assert harness.platform.instances() == []


@pytest.mark.asyncio
async def test_second_agent_instance_for_one_trial_is_rejected(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, source, _ = world(tmp_path, deployment, monkeypatch, "tb8", shared_verifier=True)
    trial = tmp_path / "trial"
    agent = environment(target, source, trial, "tb8__env")
    await agent.start()
    with pytest.raises(RuntimeError, match="already registered"):
        await environment(target, source, trial, "tb8__env-again").start()
    await agent.stop()
    await shutdown()


@pytest.mark.asyncio
async def test_cpu_and_memory_overrides_fold_into_the_trial(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, source, harness = world(tmp_path, deployment, monkeypatch, "tb9", shared_verifier=True)
    agent = environment(target, source, tmp_path / "trial", "tb9__env", override_cpus=2, override_memory_mb=2048)
    await agent.start()
    spec = harness.platform.spec_of(units(harness.platform)[0])
    assert spec.resources.cpu == "2"
    assert spec.resources.memory == "2048Mi"  # canonical form of 2048MiB
    with pytest.raises(ValueError, match="override_storage_mb is unsupported"):
        environment(target, source, tmp_path / "trial", "tb9__env2", override_storage_mb=1024)
    await agent.stop()
    await shutdown()


def test_missing_deployment_option_names_the_flag(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, _ = world(tmp_path, deployment, monkeypatch, "tb10")
    with pytest.raises(ValueError, match="deployment option is required"):
        FlotillaTaskEnvironment(
            environment_dir=source.local / "environment",
            environment_name="task",
            session_id="tb10__env",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(docker_image=IMAGE),
        )


def test_preflight_loads_the_deployment_and_names_bad_options(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, _, _ = world(tmp_path, deployment, monkeypatch, "tb11")
    FlotillaTaskEnvironment.preflight({"deployment": target})
    with pytest.raises(ValueError, match="Unknown option"):
        FlotillaTaskEnvironment.preflight({"deployment": target, "bogus": 1})
    with pytest.raises(ValueError, match="cannot import"):
        FlotillaTaskEnvironment.preflight({"deployment": "no.such.module:DEPLOYMENT"})
