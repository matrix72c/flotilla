"""Real Harbor Trial and verifier over Flotilla's public adapter and fake resources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("harbor")
import toml  # type: ignore[import-untyped]
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial

from flotilla.build.driver import build_task
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.integrations.harbor_source import task_environments
from flotilla.platform.base import ErrorCategory, FlotillaError, InstanceSpec
from tests.contract.harbor_local import attach_local_files
from tests.support.build import _ctx
from tests.support.platform import FakePlatform, SharedTree
from tests.support.runtime import deployment as deployment


@pytest.mark.filterwarnings("ignore:Task.checksum will be deprecated:DeprecationWarning")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,delete_failure",
    [
        ("shared", False),
        ("separate", False),
        ("explicit", False),
        ("explicit", True),
        ("artifacts", False),
        ("artifacts", True),
    ],
)
async def test_native_harbor_trial(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    mode: str,
    delete_failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, platform, _ = deployment
    attach_local_files(platform, tmp_path / "instances")
    deployment_file = tmp_path / "deployment.toml"
    deployment_file.write_text(toml.dumps(cfg.model_dump(exclude_none=True, exclude={"base_dir"})))
    source = tmp_path / "neutral"
    (source / "environment").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "instruction.md").write_text("Do nothing. The verifier checks a neutral fixture.")
    verifier = '[verifier]\nenvironment_mode="separate"\n' if mode != "shared" else "[verifier]\n"
    if mode in ("explicit", "artifacts"):
        verifier += '[verifier.environment]\ndocker_image="r.io/verifier:1"\n'
    verifier += '[verifier.env]\nFIXTURE_REWARD_PATH="/logs/verifier/reward.txt"\n'
    task_config = '[environment]\ndocker_image="r.io/main:1"\n' + verifier
    if mode == "artifacts":
        (source / "environment/docker-compose.yaml").write_text(
            "services:\n  main:\n    image: r.io/main:1\n  db:\n    image: r.io/db:1\n"
        )
        task_config += (
            '[[artifacts]]\nsource="/evidence/main.txt"\ndestination="main.txt"\n'
            '[[artifacts]]\nsource="/evidence/db.txt"\ndestination="db.txt"\nservice="db"\n'
        )
    (source / "task.toml").write_text(task_config)
    (source / "tests/test.sh").write_text('#!/bin/sh\nprintf "1\\n" > "$FIXTURE_REWARD_PATH"\n')
    output = tmp_path / "build"
    definitions = task_environments(source)
    assert len(definitions) == (1 if mode == "shared" else 2)
    for definition in definitions:
        outcome = build_task(definition, output, _ctx())
        assert outcome.status == "built", outcome.reason
        assert definition.environment_config is not None

    # Seed the separate verifier's prebuilt /tests, which Harbor intentionally
    # does not upload. Main/shared receives tests through real tar transfers.
    platform.initial_files["/tests/test.sh"] = (source / "tests/test.sh").read_bytes()
    old_create = platform.on_create
    failed_instances: set[str] = set()
    unit_count = 0
    original_delete = platform.delete

    async def delete(iid: str) -> None:
        if iid in failed_instances:
            raise FlotillaError("fixture deletion denied", stage="run", category=ErrorCategory.INVALID, retryable=False)
        await original_delete(iid)

    monkeypatch.setattr(platform, "delete", delete)

    def prepare_instance(iid: str, spec: InstanceSpec) -> None:
        nonlocal unit_count
        if spec.labels.get(labels.ROLE) != labels.ROLE_ANCHOR:
            unit_count += 1
            if delete_failure and (
                (mode == "artifacts" and spec.labels.get(labels.UNIT) == "main" and unit_count <= 2)
                or (mode != "artifacts" and unit_count == 2)
            ):
                failed_instances.add(iid)
        if old_create:
            old_create(iid, spec)
        root = tmp_path / "instances" / iid
        (root / "tests").mkdir(parents=True)
        (root / "tests/test.sh").write_bytes(platform.initial_files["/tests/test.sh"])
        (root / "tests/test.sh").chmod(0o755)
        if mode == "artifacts" and spec.labels.get(labels.ROLE) != labels.ROLE_ANCHOR:
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "main.txt").write_text("main-owned\n")
            (evidence / "db.txt").write_text("sidecar-owned\n")

    platform.on_create = prepare_instance
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(source)},
            "trial_name": "neutral",
            "trials_dir": str(tmp_path / "trials"),
            "agent": {"name": "nop"},
            "environment": {
                "import_path": "flotilla.integrations.harbor:FlotillaEnvironment",
                "kwargs": {"deployment": str(deployment_file)},
            },
        }
    )
    trial = await Trial.create(config)
    result = await trial.run()
    if delete_failure:
        records = list((tmp_path / "trials/neutral/flotilla").glob("*/cleanup.json"))
        # Cleanup reporting is independent of the verifier's scoring result.
        assert any(not json.loads(path.read_text())["clean"] for path in records)
        if mode == "explicit":
            # A platform deletion failure must not invalidate completed scoring.
            assert result.exception_info is None
            assert result.verifier_result is not None
            assert result.verifier_result.rewards == {"reward": 1.0}
            persisted = json.loads((tmp_path / "trials/neutral/result.json").read_text())
            assert persisted["exception_info"] is None
            assert persisted["verifier_result"]["rewards"] == {"reward": 1.0}
            assert failed_instances <= {instance.iid for instance in await platform.list({})}
            return
        assert result.exception_info is not None
        if mode == "artifacts":
            assert result.exception_info.exception_type == "TimeoutError"
            assert "service=main" in result.exception_info.exception_message
            assert "instance=" in result.exception_info.exception_message
        else:
            assert result.exception_info.exception_type == "CleanupError"
        if mode != "artifacts":
            assert result.verifier_result is None
        # Harbor preserves scoring evidence after main cleanup failure, but its
        # exception_info makes the Trial unsuccessful. Training admission must
        # check that field instead of treating the presence of rewards as success.
        assert failed_instances <= {instance.iid for instance in await platform.list({})}
        return
    assert result.exception_info is None, result.exception_info
    assert result.verifier_result.rewards == {"reward": 1.0}
    if mode == "artifacts":
        artifacts = tmp_path / "trials/neutral/artifacts"
        assert (artifacts / "main.txt").read_text() == "main-owned\n"
        assert (artifacts / "db.txt").read_text() == "sidecar-owned\n"
    assert not await platform.list({})
    records = list((tmp_path / "trials/neutral/flotilla").glob("*/cleanup.json"))
    assert len(records) == len(definitions)
    assert all(json.loads(path.read_text())["clean"] for path in records)
