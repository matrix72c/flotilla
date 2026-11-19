"""Dataset selection and runtime contracts for Harbor extensions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import toml  # type: ignore[import-untyped]
from harbor.models.job.config import JobConfig
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import Task as HarborTask
from harbor.models.trial.config import TaskConfig as TrialTaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.verifier import RewardFileEmptyError, RewardFileNotFoundError, VerifierOutputParseError

from flotilla.config import FlotillaConfig
from flotilla.integrations.harbor_native import (
    FlotillaTaskSource,
    NativeFlotillaEnvironment,
    NativeFlotillaRuntime,
)
from flotilla.native import VerificationResult
from flotilla.testing.config import importable

pytestmark = pytest.mark.asyncio
LOGGER = logging.getLogger(__name__)

Selection = tuple["FlotillaTaskSource", Path, str, str]

IMAGE = "r.io/ns/main@sha256:" + "b" * 64
TASK_KEY = "datasets/task"
#: What `verification_of` computes for a task that ships a `tests/` directory.
VERIFY_COMMAND = ("bash", "/tests/test.sh")
ARTIFACTS = [{"source": "/evidence/main.txt", "destination": "main.txt"}]


def build_dataset(base: Path, *, mode: str = "separate", steps: bool = False) -> Path:
    """A published dataset holding one task: hidden tests, an instruction, a Harbor presentation."""
    task = base / "dataset" / TASK_KEY
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "instruction.md").write_text("A neutral native instruction.\n")
    config: dict[str, Any] = {
        "environment": {"docker_image": IMAGE},
        "artifacts": ARTIFACTS,
        "verifier": {"env": {"TASK_TOKEN": "native"}, "timeout_sec": 600},
    }
    if mode == "separate":
        config["verifier"]["environment_mode"] = "separate"
        (task / "tests/docker-compose.yaml").write_text(f"services:\n  main:\n    image: {IMAGE}\n")
    if steps:
        config["steps"] = [{"name": "step-one"}]
    (task / "task.toml").write_text(toml.dumps(config))
    return base / "dataset"


def write_deployment(root: Path, *, name: str = "fixture") -> str:
    """A spec declaring one dataset, published where the task source can import it."""
    return importable(
        FlotillaConfig.from_mapping(
            {
                "opensandbox": {
                    "endpoint": "http://os.test/",
                    "credential_env": "FLOTILLA_CRED",
                    "network": {"sandbox_cidrs": ["10.0.0.0/8"], "platform_cidrs": ["169.254.169.254/32"]},
                },
                "storage": {"host_path": "/mnt/shared"},
                "share": {"release": "test", "release_sha256": "a" * 64},
                "datasets": {name: {"local_root": str(root), "platform_root": f"/mnt/shared/datasets/{name}"}},
            }
        )
    )


@pytest.fixture
def selection(tmp_path: Path) -> Selection:
    root = build_dataset(tmp_path)
    deployment = write_deployment(root)
    source = FlotillaTaskSource(dataset="fixture", deployment=deployment, metadata_workers=2)
    return source, root, deployment, TASK_KEY


# --------------------------------------------------------------------------- #
# Task source: dataset rows become Harbor trial tasks verbatim.
# --------------------------------------------------------------------------- #


def job_config(source: FlotillaTaskSource) -> JobConfig:
    kwargs: dict[str, Any] = {
        "dataset": source.dataset,
        "deployment": str(source.deployment),
        "metadata_workers": source.metadata_workers,
    }
    if source.selection is not None:
        kwargs["selection"] = str(source.selection)
    return JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": kwargs,
            }
        }
    )


async def test_a_selection_file_preserves_its_order_and_duplicates(tmp_path: Path, selection: Selection) -> None:
    source, root, _deployment_file, key = selection
    chosen = tmp_path / "selection.txt"
    chosen.write_text(f"# chosen tasks\n{key}\n{key}\n\n{key}\n")
    source.selection = chosen
    resolved = await source.resolve_task_configs(job_config(source))
    # Duplicates are separate trials, not deduplicated; comments and blank lines are skipped.
    assert [task.path for task in resolved] == [root / key] * 3
    assert [task.source for task in resolved] == ["fixture"] * 3
    # Every selected key was resolved against the dataset, not just the first.
    assert set(source.sources) == {(root / key).absolute()}


async def test_without_a_selection_file_the_whole_dataset_is_selected(selection: Selection) -> None:
    source, root, _deployment_file, key = selection
    resolved = await source.resolve_task_configs(job_config(source))
    assert [task.path for task in resolved] == [root / key]


async def test_resolve_rejects_harbor_task_selection(selection: Selection) -> None:
    source, _, _, _ = selection
    config = job_config(source).model_copy(update={"tasks": [TrialTaskConfig(path=Path("/somewhere/else"))]})
    with pytest.raises(ValueError, match="replaces Harbor task/dataset discovery"):
        await source.resolve_task_configs(config)


async def test_resolve_rejects_empty_selections_and_undeclared_datasets(tmp_path: Path, selection: Selection) -> None:
    source, _root, deployment, key = selection
    empty = tmp_path / "empty.txt"
    empty.write_text("\n# nothing\n")
    source.selection = empty
    with pytest.raises(ValueError, match="selects no tasks"):
        await source.resolve_task_configs(job_config(source))
    absent = tmp_path / "absent.txt"
    absent.write_text("datasets/missing\n")
    other = FlotillaTaskSource(dataset="fixture", deployment=deployment, selection=absent)
    with pytest.raises(ValueError, match="not a task directory"):
        await other.resolve_task_configs(job_config(other))
    foreign = FlotillaTaskSource(dataset="absent-name", deployment=deployment)
    with pytest.raises(ValueError, match="declares no dataset"):
        await foreign.resolve_task_configs(job_config(foreign))
    del key


async def test_load_builds_the_harbor_task_from_the_tasks_own_config(selection: Selection) -> None:
    source, _root, _, key = selection
    resolved = await source.resolve_task_configs(job_config(source))
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "extra_instructions": ["Run-specific instruction."],
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": source.dataset, "deployment": str(source.deployment)},
            },
        }
    )
    task, download = await source.load_task(config)
    assert isinstance(task, HarborTask)
    assert task.task_dir == resolved[0].path
    assert "A neutral native instruction." in task.instruction
    assert "Run-specific instruction." in task.instruction
    assert task.config.environment.docker_image == IMAGE
    assert download.cached and download.download_time_sec == 0
    assert source.source_for(task).key == f"datasets/fixture/{key}"


async def test_load_rejects_unsupported_and_foreign_tasks(tmp_path: Path, selection: Selection) -> None:
    source, _, _deployment_file, _key = selection
    resolved = await source.resolve_task_configs(job_config(source))
    good = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": source.dataset, "deployment": str(source.deployment)},
            },
        }
    )
    with pytest.raises(ValueError, match="remote downloads"):
        await source.load_task(good.model_copy(update={"task": TrialTaskConfig(name="org/task")}))
    with pytest.raises(ValueError, match="not a task directory"):
        await source.load_task(good.model_copy(update={"task": TrialTaskConfig(path=tmp_path / "elsewhere")}))

    multi = build_dataset(tmp_path / "multi", steps=True)
    many = FlotillaTaskSource(dataset="fixture", deployment=write_deployment(multi))
    many_resolved = await many.resolve_task_configs(job_config(many))
    many_config = TrialConfig.model_validate(
        {
            "task": {"path": str(many_resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": many.dataset, "deployment": many.deployment},
            },
        }
    )
    with pytest.raises(ValueError, match="single-step"):
        await many.load_task(many_config)


async def test_a_relative_selection_resolves_against_the_config_and_snapshots_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment is an import target, so the selection file is the one input still named by path."""
    origin = tmp_path / "data"
    root = build_dataset(origin)
    (origin / "selection.txt").write_text(f"{TASK_KEY}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {
                    "dataset": "fixture",
                    "deployment": write_deployment(root),
                    "selection": "selection.txt",
                },
                "kwargs_base_dirs": {"selection": origin},
            }
        }
    )
    source = config.task_source.resolve()
    resolved = await source.resolve_task_configs(config)
    assert config.task_source.kwargs["selection"] == str(origin / "selection.txt")
    # Snapshots taken after resolution load from any working directory.
    restored = JobConfig.model_validate_json(config.model_dump_json())
    task, _ = await restored.task_source.resolve().load_task(
        TrialConfig(task=TrialTaskConfig(path=resolved[0].path), task_source=restored.task_source)
    )
    assert "A neutral native instruction." in task.instruction


async def test_a_relative_selection_fails_without_a_local_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_dataset(tmp_path / "data")
    config = JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {
                    "dataset": "fixture",
                    "deployment": write_deployment(root),
                    "selection": "selection.txt",
                },
                "kwargs_base_dirs": {"selection": None},
            }
        }
    )
    with pytest.raises(ValueError, match="relative selection requires a local config file"):
        await config.task_source.resolve().resolve_task_configs(config)


# --------------------------------------------------------------------------- #
# Trial runtime: one NativeAttempt per trial, Harbor reward semantics.
# --------------------------------------------------------------------------- #


async def make_runtime(
    tmp_path: Path,
    selection: Selection,
    *,
    artifacts: tuple[Any, ...] = (),
    attempt: SimpleNamespace | None = None,
    main: AsyncMock | None = None,
) -> NativeFlotillaRuntime:
    await selection[0].resolve_task_configs(job_config(selection[0]))

    source, root, deployment, key = selection
    config = SimpleNamespace(
        task_source=None,
        verifier=SimpleNamespace(import_path=None, env={}),
        environment=SimpleNamespace(delete=True),
        artifacts=list(artifacts),
    )
    task_config = TaskConfig.model_validate({"environment": {"docker_image": IMAGE}, "artifacts": ARTIFACTS})
    task = HarborTask.from_metadata(root, config=task_config, instruction="A neutral native instruction.\n")
    runtime = NativeFlotillaRuntime.__new__(NativeFlotillaRuntime)
    runtime.source = source.sources[(root / key).absolute()]
    runtime.config = config
    runtime.task = task
    runtime.paths = paths_of(tmp_path)
    runtime.context_id = None  # type: ignore[assignment]
    runtime.deployment = deployment
    runtime.run_timeout = 600.0
    runtime.fleet = SimpleNamespace(  # type: ignore[assignment]
        close=AsyncMock(return_value=SimpleNamespace(clean=True))
    )
    runtime.attempt = attempt
    runtime._main = main
    runtime._creation_lock = None  # type: ignore[assignment]
    runtime._close_task = None
    runtime._main_ready = main is not None
    runtime._collection_started = False
    from harbor.trial.artifact_handler import ArtifactHandler

    runtime.artifacts = [*task.config.artifacts, *config.artifacts]
    runtime._artifact_handler = ArtifactHandler(artifacts=runtime.artifacts, logger=LOGGER)
    runtime._artifact_handler.begin_collection()
    return runtime


def paths_of(tmp_path: Path) -> TrialPaths:
    return TrialPaths(trial_dir=tmp_path / "trial")


def fake_main(events: list[str] | None = None) -> AsyncMock:
    main = AsyncMock()
    main.os = __import__("harbor.models.task.config", fromlist=["TaskOS"]).TaskOS.LINUX
    if events is not None:
        main.service_download_dir.side_effect = lambda *a, **k: events.append("download")
        main.service_is_dir.return_value = True
    return main


def fake_attempt(result: VerificationResult | None = None, events: list[str] | None = None) -> SimpleNamespace:
    attempt = SimpleNamespace()
    attempt.verify = AsyncMock(return_value=result)
    attempt.close = AsyncMock()
    if events is not None:

        async def verify(**kwargs: Any) -> VerificationResult | None:
            events.append("verify")
            return result

        attempt.verify.side_effect = verify
    return attempt


async def test_verify_parses_each_reward_contract(tmp_path: Path, selection: Selection) -> None:
    cases = [
        ({"reward.json": b'{"accuracy": 1, "style": 0.5}'}, {"accuracy": 1, "style": 0.5}),
        ({"reward.txt": b"0.75\n"}, {"reward": 0.75}),
    ]
    for outputs, expected in cases:
        runtime = await make_runtime(
            tmp_path, selection, attempt=fake_attempt(VerificationResult(0, "", "", outputs)), main=fake_main()
        )
        result = await runtime.verify(runtime._main, None, logger=LOGGER, log_context=None)
        assert result.rewards == expected

    runtime = await make_runtime(
        tmp_path,
        selection,
        attempt=fake_attempt(VerificationResult(2, "out", "err", {"reward.txt": b"0"})),
        main=fake_main(),
    )
    # A failing test script that still writes a reward is a graded zero, not an infrastructure error.
    assert (await runtime.verify(runtime._main, None, logger=LOGGER, log_context=None)).rewards == {"reward": 0.0}


async def test_verify_reward_failures_mirror_harbor(tmp_path: Path, selection: Selection) -> None:
    cases = [
        ({}, RewardFileNotFoundError),
        ({"reward.json": b""}, RewardFileEmptyError),
        ({"reward.json": b"{nope"}, VerifierOutputParseError),
        ({"reward.json": b"0"}, VerifierOutputParseError),
        ({"reward.json": b"[]"}, VerifierOutputParseError),
        ({"reward.json": b'{"r": "high"}'}, VerifierOutputParseError),
        ({"reward.json": b'{"r": NaN}'}, VerifierOutputParseError),
        ({"reward.txt": b""}, RewardFileEmptyError),
        ({"reward.txt": b"high"}, VerifierOutputParseError),
    ]
    for outputs, error in cases:
        runtime = await make_runtime(
            tmp_path, selection, attempt=fake_attempt(VerificationResult(0, "", "", outputs)), main=fake_main()
        )
        with pytest.raises(error):
            await runtime.verify(runtime._main, None, logger=LOGGER, log_context=None)


async def test_verify_persists_outputs_and_collects_before_releasing_main(tmp_path: Path, selection: Selection) -> None:
    events: list[str] = []
    main = fake_main(events)
    attempt = fake_attempt(
        VerificationResult(1, "stdout\n", "stderr\n", {"reward.txt": b"1\n", "../escape": b"x"}), events
    )
    runtime = await make_runtime(tmp_path, selection, attempt=attempt, main=main)
    with pytest.raises(ValueError, match="escapes"):
        await runtime.verify(runtime._main, 60.0, logger=LOGGER, log_context=None)
    # Collected agent results survive a failing verification.
    assert events == ["download", "verify"]
    assert attempt.verify.call_args.kwargs["timeout"] == 60.0

    safe = fake_attempt(VerificationResult(1, "stdout\n", "stderr\n", {"reward.txt": b"1\n"}), events)
    runtime.attempt = safe
    result = await runtime.verify(runtime._main, None, logger=LOGGER, log_context=None)
    assert result.rewards == {"reward": 1.0}
    verifier_dir = runtime.paths.verifier_dir
    assert (verifier_dir / "reward.txt").read_bytes() == b"1\n"
    assert (verifier_dir / "test-stdout.txt").read_text() == "stdout\n"
    assert (verifier_dir / "test-stderr.txt").read_text() == "stderr\n"


async def test_verify_requires_this_trials_environment(tmp_path: Path, selection: Selection) -> None:
    runtime = await make_runtime(tmp_path, selection, attempt=fake_attempt(), main=fake_main())
    with pytest.raises(RuntimeError, match="this trial's started main environment"):
        await runtime.verify(fake_main(), None, logger=LOGGER, log_context=None)


async def test_close_collects_without_verification_and_is_idempotent(tmp_path: Path, selection: Selection) -> None:
    events: list[str] = []
    main = fake_main(events)
    attempt = fake_attempt(events=events)
    runtime = await make_runtime(tmp_path, selection, attempt=attempt, main=main)
    await runtime.close()
    assert events == ["download"]
    attempt.close.assert_awaited_once()
    runtime.fleet.close.assert_awaited_once()  # type: ignore[attr-defined]
    await runtime.close()  # idempotent
    assert attempt.close.await_count == 1


async def test_native_initialization_preserves_declared_readonly_filesystem() -> None:
    @dataclass
    class Info:
        environment_id: str = "native-readonly"

    environment = NativeFlotillaEnvironment.__new__(NativeFlotillaEnvironment)
    handle = SimpleNamespace(info=AsyncMock(return_value=Info()), agent=SimpleNamespace(exec=AsyncMock()))
    environment._environment = handle  # type: ignore[assignment]
    environment._save_metadata = Mock()  # type: ignore[method-assign]
    await environment.initialize()
    environment._save_metadata.assert_called_once_with("environment", {"environment_id": "native-readonly"})
    # NativeAttempt already owns output/artifact creation and numeric permissions.
    # In particular, don't create an isolated verifier's /logs/verifier in main,
    # or chmod the user's declared artifact directories to world-writable.
    handle.agent.exec.assert_not_called()


async def test_constructor_rejects_incompatible_trials(
    tmp_path: Path, selection: Selection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _root, deployment, _key = selection
    resolved = await source.resolve_task_configs(job_config(source))
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": source.dataset, "deployment": deployment},
            },
            "runtime": {
                "import_path": "flotilla.integrations.harbor_native:NativeFlotillaRuntime",
                "kwargs": {"deployment": deployment},
            },
            "environment": {"import_path": "flotilla.integrations.harbor_native:NativeFlotillaEnvironment"},
            "trials_dir": tmp_path / "trials",
            "trial_name": "native",
        }
    )
    task, _ = await source.load_task(config)

    def build(**overrides: Any) -> NativeFlotillaRuntime:
        values = {
            "task": task,
            "config": config,
            "paths": paths_of(tmp_path),
            "context_id": None,
            "deployment": deployment,
        }
        values.update(overrides)
        monkeypatch.setattr(NativeFlotillaRuntime, "__init__", NativeFlotillaRuntime.__init__)
        return NativeFlotillaRuntime(**values)

    config.task_source._instance = source
    custom_verifier = config.model_copy(deep=True)
    custom_verifier.verifier.import_path = "custom:Verifier"
    with pytest.raises(ValueError, match="import_path cannot replace it"):
        build(config=custom_verifier)
    custom_env = config.model_copy(deep=True)
    custom_env.verifier.env = {"OVERRIDE": "1"}
    with pytest.raises(ValueError, match="cannot be overridden"):
        build(config=custom_env)
    retain = config.model_copy(deep=True)
    retain.environment.delete = False
    with pytest.raises(ValueError, match="always releases trial resources"):
        build(config=retain)
    with pytest.raises(ValueError, match="configured FlotillaTaskSource"):
        build(config=config.model_copy(update={"task_source": None}))
