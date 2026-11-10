"""Native release selection and runtime contracts for Harbor extensions."""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor.models.extensions")
import toml
from harbor.models.job.config import JobConfig
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import Task as HarborTask
from harbor.models.trial.config import TaskConfig as TrialTaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.verifier import RewardFileEmptyError, RewardFileNotFoundError, VerifierOutputParseError

from flotilla.integrations.harbor_native import (
    FlotillaTaskSource,
    NativeFlotillaRuntime,
)
from flotilla.manifest import Context, Manifest, Resources, Unit
from flotilla.native import (
    Artifact,
    ReleaseWriter,
    Task,
    Verification,
    VerificationResult,
    read_dataset,
)

pytestmark = pytest.mark.asyncio
LOGGER = logging.getLogger(__name__)

VERIFY_COMMAND = ("verify", "run")
PRESENTATION = {
    "environment": {"docker_image": "r.io/main:1"},
    "artifacts": [{"source": "/evidence/main.txt", "destination": "main.txt"}],
}


def unit() -> Unit:
    return Unit(
        image="image@sha256:" + "b" * 64,
        resources=Resources(cpu="1", memory="512Mi"),
        context=Context(env={}, uid=0, gid=0, cwd="/"),
        command=None,
    )


def role(name: str, *, services: tuple[str, ...] = ("main",)) -> Manifest:
    return Manifest(task=name, agent_service="main", units={service: unit() for service in services})


def build_release(
    base: Path, *, mode: str = "separate", presentation: dict | None = None, importer: str = "harbor-v1"
) -> Path:
    """A real release: baked verification, agent artifact handoff, Harbor provenance."""
    tests = base / "tests"
    tests.mkdir(parents=True)
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    roles: dict[str, Manifest] = {"main": role("datasets/task")}
    if mode == "separate":
        roles["verifier"] = role("datasets/task#verifier")
    with ReleaseWriter(base / "release") as writer:
        asset = writer.add_asset(tests)
        task = Task(
            key="datasets/task",
            roles=roles,
            assets=(asset,),
            verification=Verification(
                role="verifier" if mode == "separate" else "main",
                command=VERIFY_COMMAND,
                tests=asset,
                env={"TASK_TOKEN": "native"},
                optional_output_paths={"reward.txt": "/logs/verifier/reward.txt"},
                require_output=True,
                timeout_s=600.0,
            ),
            artifacts=(Artifact(name="agent", role="main", target="/logs/agent", verifier_target="/handoff/agent"),),
            provenance={
                "importer": importer,
                "harbor": {"config": PRESENTATION if presentation is None else presentation},
            },
        )
        record = writer.add_task(task, "A neutral native instruction.\n")
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest, "annotations": {}}])
    return base / "release"


def write_deployment(path: Path, root: Path, *, name: str = "fixture") -> Path:
    path.write_text(
        toml.dumps(
            {
                "opensandbox": {
                    "endpoint": "http://os.test/",
                    "credential_env": "FLOTILLA_CRED",
                    "capabilities": "caps.json",
                    "network": {"sandbox_cidrs": ["10.0.0.0/8"], "platform_cidrs": ["169.254.169.254/32"]},
                },
                "storage": {"host_path": "/mnt/shared"},
                "share": {"release": "test", "release_sha256": "a" * 64},
                "task_releases": {name: {"local_root": str(root), "platform_root": f"/mnt/shared/releases/{name}"}},
            }
        )
    )
    return path


@pytest.fixture
def selection(tmp_path: Path) -> tuple[FlotillaTaskSource, Path, Path, dict]:
    root = build_release(tmp_path)
    deployment_file = write_deployment(tmp_path / "deployment.toml", root)
    source = FlotillaTaskSource(dataset=root / "dataset.jsonl", deployment=deployment_file, metadata_workers=2)
    return source, root, deployment_file, read_dataset(root / "dataset.jsonl")[0].model_dump()


# --------------------------------------------------------------------------- #
# Task source: dataset rows become Harbor trial tasks verbatim.
# --------------------------------------------------------------------------- #


def job_config(source: FlotillaTaskSource) -> JobConfig:
    return JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {
                    "dataset": str(source.dataset),
                    "deployment": str(source.deployment),
                    "metadata_workers": source.metadata_workers,
                },
            }
        }
    )


async def test_resolve_preserves_row_order_and_duplicates(tmp_path, selection):
    source, root, _deployment_file, row = selection
    duplicates = tmp_path / "duplicates.jsonl"
    duplicates.write_text(
        "".join(json.dumps(payload) + "\n" for payload in (row, dict(row, annotations={"repeat": 1}), row))
    )
    source.dataset = duplicates
    resolved = await source.resolve_task_configs(job_config(source))
    assert [task.path for task in resolved] == [
        root / PurePosixPath(row["task_ref"]).parent
    ] * 3  # duplicates are separate trials, not deduplicated
    assert [task.source for task in resolved] == [row["release_id"]] * 3
    # Every row was validated against the release, not just the first.
    assert set(source.bounds) == {(root / PurePosixPath(row["task_ref"]).parent).absolute()}


async def test_resolve_rejects_harbor_task_selection(selection):
    source, _, _, _ = selection
    config = job_config(source).model_copy(update={"tasks": [TrialTaskConfig(path=Path("/somewhere/else"))]})
    with pytest.raises(ValueError, match="replace Harbor task/dataset discovery"):
        await source.resolve_task_configs(config)


async def test_resolve_rejects_empty_and_unbound_selections(tmp_path, selection):
    source, _root, _deployment_file, row = selection
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    source.dataset = empty
    with pytest.raises(ValueError, match="selects no tasks"):
        await source.resolve_task_configs(job_config(source))
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text(json.dumps(dict(row, release_id="0" * 64)) + "\n")
    source.dataset = foreign
    with pytest.raises(ValueError, match="without an explicit binding"):
        await source.resolve_task_configs(job_config(source))


async def test_load_rebuilds_the_harbor_task_from_provenance(selection):
    source, _root, _, row = selection
    resolved = await source.resolve_task_configs(job_config(source))
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "extra_instructions": ["Run-specific instruction."],
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": str(source.dataset), "deployment": str(source.deployment)},
            },
        }
    )
    task, download = await source.load_task(config)
    assert isinstance(task, HarborTask)
    assert task.task_dir == resolved[0].path
    assert "A neutral native instruction." in task.instruction
    assert "Run-specific instruction." in task.instruction
    assert task.config.environment.docker_image == "r.io/main:1"
    assert download.cached and download.download_time_sec == 0
    bound = source.bound_for(task)
    assert bound.row.task_digest == row["task_digest"]
    assert bound.task.provenance["harbor"]["config"]["environment"]["docker_image"] == "r.io/main:1"


async def test_load_rejects_unprovenanced_unsupported_and_foreign_tasks(tmp_path, selection):
    source, _, _deployment_file, _row = selection
    resolved = await source.resolve_task_configs(job_config(source))
    good = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": str(source.dataset), "deployment": str(source.deployment)},
            },
        }
    )
    with pytest.raises(ValueError, match="remote downloads"):
        await source.load_task(good.model_copy(update={"task": TrialTaskConfig(name="org/task")}))
    with pytest.raises(ValueError, match="not a task payload"):
        await source.load_task(good.model_copy(update={"task": TrialTaskConfig(path=tmp_path / "elsewhere")}))

    unprovenanced = build_release(tmp_path / "bare", importer="unknown-v9")
    write_deployment(tmp_path / "bare.toml", unprovenanced)
    bare = FlotillaTaskSource(dataset=unprovenanced / "dataset.jsonl", deployment=tmp_path / "bare.toml")
    bare_resolved = await bare.resolve_task_configs(job_config(bare))
    assert bare_resolved  # presentation validation happens at load time
    bare_config = TrialConfig.model_validate(
        {
            "task": {"path": str(bare_resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": str(bare.dataset), "deployment": str(bare.deployment)},
            },
        }
    )
    with pytest.raises(ValueError, match="presentation provenance"):
        await bare.load_task(bare_config)

    multi = build_release(
        tmp_path / "multi", presentation={"environment": {"docker_image": "r.io/x:1"}, "steps": [{"name": "step-one"}]}
    )
    write_deployment(tmp_path / "multi.toml", multi)
    many = FlotillaTaskSource(dataset=multi / "dataset.jsonl", deployment=tmp_path / "multi.toml")
    many_resolved = await many.resolve_task_configs(job_config(many))
    many_config = TrialConfig.model_validate(
        {
            "task": {"path": str(many_resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": str(many.dataset), "deployment": str(many.deployment)},
            },
        }
    )
    with pytest.raises(ValueError, match="single-step"):
        await many.load_task(many_config)


async def test_relative_inputs_resolve_against_the_config_and_freeze_absolute(tmp_path, monkeypatch):
    root = build_release(tmp_path / "data")
    deployment_file = write_deployment(tmp_path / "data" / "deployment.toml", root)
    origin = tmp_path / "data"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": "release/dataset.jsonl", "deployment": "deployment.toml"},
                "kwargs_base_dirs": {"dataset": origin, "deployment": origin},
            }
        }
    )
    source = config.task_source.resolve()
    resolved = await source.resolve_task_configs(config)
    assert config.task_source.kwargs["dataset"] == str(origin / "release/dataset.jsonl")
    assert config.task_source.kwargs["deployment"] == str(deployment_file)
    # Snapshots taken after resolution load from any working directory.
    restored = JobConfig.model_validate_json(config.model_dump_json())
    task, _ = await restored.task_source.resolve().load_task(
        TrialConfig(task=TrialTaskConfig(path=resolved[0].path), task_source=restored.task_source)
    )
    assert "A neutral native instruction." in task.instruction


async def test_relative_inputs_fail_without_a_local_config_file(tmp_path, monkeypatch):
    root = build_release(tmp_path / "release")
    write_deployment(tmp_path / "deployment.toml", root)
    config = JobConfig.model_validate(
        {
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": "release/dataset.jsonl", "deployment": "deployment.toml"},
                "kwargs_base_dirs": {"dataset": None, "deployment": None},
            }
        }
    )
    with pytest.raises(ValueError, match="relative dataset requires a local config file"):
        await config.task_source.resolve().resolve_task_configs(config)


# --------------------------------------------------------------------------- #
# Trial runtime: one NativeAttempt per trial, Harbor reward semantics.
# --------------------------------------------------------------------------- #


async def make_runtime(tmp_path, selection, *, artifacts=(), attempt=None, main=None):
    await selection[0].resolve_task_configs(job_config(selection[0]))

    source, root, deployment_file, row = selection
    config = SimpleNamespace(
        task_source=None,
        verifier=SimpleNamespace(import_path=None, env={}),
        environment=SimpleNamespace(delete=True),
        artifacts=list(artifacts),
    )
    task_config = TaskConfig.model_validate(PRESENTATION)
    task = HarborTask.from_metadata(root, config=task_config, instruction="A neutral native instruction.\n")
    runtime = NativeFlotillaRuntime.__new__(NativeFlotillaRuntime)
    runtime.bound = source.bounds[(root / PurePosixPath(row["task_ref"]).parent).absolute()]
    runtime.config = config
    runtime.task = task
    runtime.paths = paths_of(tmp_path)
    runtime.context_id = None
    runtime.deployment = str(deployment_file)
    runtime.run_timeout = 600.0
    runtime.runtime_params = {"SEED": "2"}
    runtime.fleet = SimpleNamespace(close=AsyncMock(return_value=SimpleNamespace(clean=True)))
    runtime.attempt = attempt
    runtime._main = main
    runtime._creation_lock = None
    runtime._close_task = None
    runtime._main_ready = main is not None
    runtime._collection_started = False
    from harbor.trial.artifact_handler import ArtifactHandler

    runtime.artifacts = [*task.config.artifacts, *config.artifacts]
    runtime._artifact_handler = ArtifactHandler(artifacts=runtime.artifacts, logger=LOGGER)
    runtime._artifact_handler.begin_collection()
    return runtime


def paths_of(tmp_path):
    return TrialPaths(trial_dir=tmp_path / "trial")


def fake_main(events: list | None = None):
    main = AsyncMock()
    main.os = __import__("harbor.models.task.config", fromlist=["TaskOS"]).TaskOS.LINUX
    if events is not None:
        main.service_download_dir.side_effect = lambda *a, **k: events.append("download")
        main.service_is_dir.return_value = True
    return main


def fake_attempt(result: VerificationResult | None = None, events: list | None = None):
    attempt = SimpleNamespace()
    attempt.verify = AsyncMock(return_value=result)
    attempt.close = AsyncMock()
    if events is not None:

        async def verify(**kwargs):
            events.append("verify")
            return result

        attempt.verify.side_effect = verify
    return attempt


async def test_verify_parses_each_reward_contract(tmp_path, selection):
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


async def test_verify_reward_failures_mirror_harbor(tmp_path, selection):
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


async def test_verify_persists_outputs_and_collects_before_releasing_main(tmp_path, selection):
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


async def test_verify_requires_this_trials_environment(tmp_path, selection):
    runtime = await make_runtime(tmp_path, selection, attempt=fake_attempt(), main=fake_main())
    with pytest.raises(RuntimeError, match="this trial's started main environment"):
        await runtime.verify(fake_main(), None, logger=LOGGER, log_context=None)


async def test_close_collects_without_verification_and_is_idempotent(tmp_path, selection):
    events: list[str] = []
    main = fake_main(events)
    attempt = fake_attempt(events=events)
    runtime = await make_runtime(tmp_path, selection, attempt=attempt, main=main)
    await runtime.close()
    assert events == ["download"]
    attempt.close.assert_awaited_once()
    runtime.fleet.close.assert_awaited_once()
    await runtime.close()  # idempotent
    assert attempt.close.await_count == 1


async def test_constructor_rejects_incompatible_trials(tmp_path, selection, monkeypatch):
    source, _root, deployment_file, _row = selection
    resolved = await source.resolve_task_configs(job_config(source))
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(resolved[0].path)},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {"dataset": str(source.dataset), "deployment": str(deployment_file)},
            },
            "runtime": {
                "import_path": "flotilla.integrations.harbor_native:NativeFlotillaRuntime",
                "kwargs": {"deployment": str(deployment_file)},
            },
            "environment": {"import_path": "flotilla.integrations.harbor_native:NativeFlotillaEnvironment"},
            "trials_dir": tmp_path / "trials",
            "trial_name": "native",
        }
    )
    task, _ = await source.load_task(config)

    def build(**overrides):
        values = {
            "task": task,
            "config": config,
            "paths": paths_of(tmp_path),
            "context_id": None,
            "deployment": str(deployment_file),
        }
        values.update(overrides)
        monkeypatch.setattr(NativeFlotillaRuntime, "__init__", NativeFlotillaRuntime.__init__)
        return NativeFlotillaRuntime(**values)

    config.task_source._instance = source
    custom_verifier = config.model_copy(deep=True)
    custom_verifier.verifier.import_path = "custom:Verifier"
    with pytest.raises(ValueError, match=r"verifier\.import_path"):
        build(config=custom_verifier)
    custom_env = config.model_copy(deep=True)
    custom_env.verifier.env = {"OVERRIDE": "1"}
    with pytest.raises(ValueError, match="baked into the release"):
        build(config=custom_env)
    retain = config.model_copy(deep=True)
    retain.environment.delete = False
    with pytest.raises(ValueError, match="always releases trial resources"):
        build(config=retain)
    with pytest.raises(ValueError, match="configured FlotillaTaskSource"):
        build(config=config.model_copy(update={"task_source": None}))
