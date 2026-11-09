"""Acceptance for Harbor's explicit source/runtime entry points, without injection.

Only fixed neutral fixture commands run through the local FakePlatform transport.
The real Task, Trial, environment/verifier factories and workspace ownership run.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

pytest.importorskip("harbor.models.extensions")
import toml
from harbor.agents.nop import NopAgent
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial

from flotilla.core import labels
from flotilla.integrations.harbor_native import FlotillaTaskSource, NativeFlotillaEnvironment, NativeFlotillaRuntime
from flotilla.integrations.harbor_source import task_environments
from flotilla.integrations.harbor_source_reader import NativeTaskSource, SourceMapping, metadata_identity
from flotilla.integrations.workspace import describe
from flotilla.platform.base import ExecResult
from flotilla.runtime.workspace import Workspace
from flotilla.testing.build import prepared_task
from flotilla.testing.harbor_local import attach_local_files
from flotilla.testing.runtime import deployment as deployment


class FailingAgent(NopAgent):
    """Native factory-loaded neutral agent for output recovery acceptance."""

    async def run(self, instruction, environment, context):
        raise RuntimeError("neutral agent failed after producing artifacts")


def task_files(root: Path, name: str, *, instruction: bool = True) -> Path:
    task = root / name
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\n')
    if instruction:
        (task / "instruction.md").write_text("A neutral native instruction.")
    return task


def task_source(root: Path) -> FlotillaTaskSource:
    return FlotillaTaskSource(sources=[SourceMapping(local_root=root, platform_prefix="datasets")], metadata_workers=1)


@pytest.mark.asyncio
async def test_source_discovers_shallow_candidates_and_filters_before_config_reads(tmp_path, monkeypatch):
    root = tmp_path / "dataset"
    first = task_files(root, "keep-a", instruction=False)
    task_files(root, "keep-b", instruction=False)
    bad = task_files(root, "excluded", instruction=False)
    (bad / "task.toml").write_text("invalid TOML = [")
    task_files(root, "container/nested", instruction=False)
    (root / "ordinary-file").write_text("not a task")
    source = task_source(root)
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        assert path.name == "task.toml", "discovery must not hydrate instructions or scan task payloads"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    config = JobConfig.model_validate(
        {"datasets": [{"path": root, "task_names": ["keep-*"], "exclude_task_names": ["keep-b"], "n_tasks": 1}]}
    )
    selected = await source.resolve_task_configs(config)
    assert [task.path for task in selected] == [first]
    assert [task.source for task in selected] == ["dataset"]
    assert reads == [first / "task.toml"]
    assert source.identities == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_source_selected_bad_config_is_not_silently_dropped(tmp_path, explicit):
    bad = task_files(tmp_path, "broken")
    (bad / "task.toml").write_text("not = [valid")
    config = JobConfig.model_validate({"tasks": [{"path": bad}]} if explicit else {"datasets": [{"path": tmp_path}]})
    with pytest.raises(ValueError):
        await task_source(tmp_path).resolve_task_configs(config)


@pytest.mark.asyncio
async def test_explicit_task_without_config_fails(tmp_path):
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(FileNotFoundError):
        await task_source(tmp_path).resolve_task_configs(JobConfig.model_validate({"tasks": [{"path": missing}]}))


@pytest.mark.asyncio
async def test_source_uses_real_metadata_task_and_excludes_run_instructions_from_identity(tmp_path):
    path = task_files(tmp_path, "task", instruction=False)
    source = task_source(tmp_path)
    selected = await source.resolve_task_configs(JobConfig.model_validate({"tasks": [{"path": path}]}))
    # Discovery/config validation succeeds without instruction.md; trial loading does not.
    config = TrialConfig(task=selected[0], extra_instructions=["Run-specific inline instruction."])
    with pytest.raises(FileNotFoundError):
        await source.load_task(config)
    (path / "instruction.md").write_text("Original task instruction.")
    extra = tmp_path / "extra.txt"
    extra.write_text("Run-specific file instruction.")
    config.extra_instruction_paths = [extra]
    task, download = await source.load_task(config)
    assert isinstance(task, Task)
    assert task.task_dir == path
    assert "Original task instruction." in task.instruction
    assert "Run-specific inline instruction." in task.instruction
    assert "Run-specific file instruction." in task.instruction
    assert download.path == path and download.cached and download.download_time_sec == 0
    original = source.reader.read_metadata("datasets/task")
    assert source.identities["datasets/task"] == metadata_identity(original)
    _, _ = await source.load_task(TrialConfig(task=selected[0], extra_instructions=["Different run instructions."]))
    assert source.identities["datasets/task"] == metadata_identity(original)
    (path / "instruction.md").write_text("Edited original task instruction.")
    await source.load_task(config)
    assert source.identities["datasets/task"] != metadata_identity(original)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("shared", "success"),
        ("separate", "success"),
        ("bundled", "success"),
        ("sidecar", "success"),
        ("separate", "missing_reward"),
        ("separate", "disabled"),
        ("separate", "agent_failure"),
    ],
)
async def test_real_native_trial_uses_source_runtime_and_platform_verification(
    tmp_path, deployment, monkeypatch, mode, outcome
):
    missing_reward = outcome == "missing_reward"
    verification_runs = outcome not in {"disabled", "agent_failure"}
    cfg, platform, tree = deployment
    instances = tmp_path / "instances"
    attach_local_files(platform, instances)
    deployment_file = tmp_path / "deployment.toml"
    deployment_file.write_text(toml.dumps(cfg.model_dump(exclude_none=True, exclude={"base_dir"})))
    root = tmp_path / "originals"
    task = task_files(root, "task")
    (task / "environment").mkdir()
    (task / "tests").mkdir()
    separate = mode != "shared"
    bundled = mode in {"bundled", "sidecar"}
    artifacts = '[[artifacts]]\nsource="/evidence/main.txt"\ndestination="main.txt"\n'
    if mode == "sidecar":
        artifacts = (
            '[[artifacts]]\nsource="/evidence/main.txt"\ndestination="main.txt"\n'
            '[[artifacts]]\nsource="/evidence/db.txt"\ndestination="db.txt"\nservice="db"\n'
        )
        (task / "environment/docker-compose.yaml").write_text(
            "services:\n  main:\n    image: r.io/main:1\n  db:\n    image: r.io/db:1\n"
        )
    content = artifacts + '[environment]\ndocker_image="r.io/main:1"\n'
    content += f'[verifier]\nenvironment_mode="{"separate" if separate else "shared"}"\n'
    if bundled:
        content += '[verifier.environment]\ndocker_image="r.io/verifier:1"\n'
    content += '[verifier.env]\nFIXTURE_REWARD_PATH="/logs/verifier/reward.txt"\n'
    content += 'FIXTURE_MAIN_PATH="/evidence/main.txt"\nFIXTURE_DB_PATH="/evidence/db.txt"\n'
    content += 'FIXTURE_EXTRA_PATH="/evidence/run-extra.txt"\n'
    (task / "task.toml").write_text(content)
    script = '#!/bin/sh\nset -e\ntest "$(cat "$FIXTURE_MAIN_PATH")" = main-owned\n'
    script += 'test "$(cat "$FIXTURE_EXTRA_PATH")" = run-extra-owned\n'
    if mode == "sidecar":
        script += 'test "$(cat "$FIXTURE_DB_PATH")" = sidecar-owned\n'
    if not missing_reward:
        script += 'echo 1 > "$FIXTURE_REWARD_PATH"\n'
    (task / "tests/test.sh").write_text(script)
    row = prepared_task(task, cfg, environments=task_environments).to_row()
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    metadata = {**describe(task, metadata_dir), "environments": row["env"]}
    tree.dirs.update({"datasets", "datasets/task"})
    prepared = []

    async def compile_workspace(workspace, runtime_params=None):
        prepared.append((workspace.key, runtime_params))
        tree.dirs.update({workspace.key + "/public", workspace.key + "/private-tests"})
        for name, value, permissions in (
            ("public/instruction.md", (task / "instruction.md").read_bytes(), "0644"),
            ("private-tests/test.sh", script.encode(), "0755"),
        ):
            key = workspace.key + "/" + name
            tree.files.add(key)
            platform.tree_contents[key] = value
            tree.modes[key] = permissions
        return metadata

    monkeypatch.setattr(Workspace, "prepare", compile_workspace)
    events = []
    units = []
    original_delete = platform.delete

    async def delete(iid):
        if iid in units:
            events.append(("delete", iid))
        await original_delete(iid)

    monkeypatch.setattr(platform, "delete", delete)

    def initialize(iid, spec):
        if spec.labels.get(labels.ROLE) != labels.ROLE_UNIT:
            return
        units.append(iid)
        events.append(("create", iid))
        local = instances / iid
        evidence = local / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        # Only main-role instances receive artifacts. Separate verifiers must
        # restore the platform handoff rather than relying on fixture seeding.
        if len(units) <= (2 if mode == "sidecar" else 1):
            (evidence / "main.txt").write_text("main-owned\n")
            (evidence / "run-extra.txt").write_text("run-extra-owned\n")
            if spec.labels.get(labels.UNIT) == "db":
                (evidence / "db.txt").write_text("sidecar-owned\n")
        elif bundled:
            (local / "tests").mkdir(parents=True)
            (local / "tests/test.sh").write_text(script)
            (local / "tests/test.sh").chmod(0o755)

    platform.on_create = initialize
    original_exec = platform.exec

    async def execute(handle, proc, **kwargs):
        spec = platform.spec_of(handle.iid)
        argv = proc.argv
        if spec.labels.get(labels.ROLE) == labels.ROLE_ANCHOR:
            if argv[:3] == ("/.flotilla/bin/busybox", "stat", "-c"):
                key = argv[-1].removeprefix(tree.mount)
                return ExecResult(0, b"directory\n" if key in tree.dirs else b"regular file\n", b"")
            if argv[0] == "cp" and argv[-2].endswith("/."):
                src = argv[-2].removeprefix(tree.mount)[:-2]
                dst = argv[-1].removeprefix(tree.mount)
                assert src in tree.dirs and dst in tree.dirs
                for paths in (tree.dirs, tree.files):
                    paths.update(dst + p[len(src) :] for p in list(paths) if p.startswith(src + "/"))
                for values in (tree.modes, tree.owners, platform.tree_contents):
                    values.update({dst + k[len(src) :]: v for k, v in list(values.items()) if k.startswith(src + "/")})
                return ExecResult(0, b"", b"")
            return await original_exec(handle, proc, **kwargs)
        mounted = [volume for volume in spec.volumes if "/attempts/" in volume.key]
        for volume in mounted:
            target = "/tmp" + volume.mount_path if volume.mount_path.startswith("/harbor-") else volume.mount_path
            directory = instances / handle.iid / target.lstrip("/")
            directory.mkdir(parents=True, exist_ok=True)
            for key in tree.files:
                if key.startswith(volume.key + "/"):
                    local = directory / key[len(volume.key) + 1 :]
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(platform.tree_contents[key])
                    local.chmod(int(tree.modes.get(key, "0644"), 8))
        translated = tuple(
            arg.replace("/harbor-handoff", "/tmp/harbor-handoff").replace(
                "/harbor-instruction", "/tmp/harbor-instruction"
            )
            for arg in argv
        )
        env = dict(proc.env)
        for name in ("FIXTURE_MAIN_PATH", "FIXTURE_DB_PATH", "FIXTURE_EXTRA_PATH"):
            if name in env:
                env[name] = str(instances / handle.iid / env[name].lstrip("/"))
        result = await original_exec(handle, dataclasses.replace(proc, argv=translated, env=env), **kwargs)
        for volume in mounted:
            if volume.read_only:
                continue
            target = "/tmp" + volume.mount_path if volume.mount_path.startswith("/harbor-") else volume.mount_path
            directory = instances / handle.iid / target.lstrip("/")
            for path in directory.rglob("*"):
                if path.is_file():
                    key = volume.key + "/" + path.relative_to(directory).as_posix()
                    tree.files.add(key)
                    platform.tree_contents[key] = path.read_bytes()
                    tree.modes[key] = oct(path.stat().st_mode & 0o777)
        return result

    monkeypatch.setattr(platform, "exec", execute)
    config = TrialConfig.model_validate(
        {
            "task": {"path": task},
            "task_source": {
                "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
                "kwargs": {
                    "sources": [{"local_root": str(root), "platform_prefix": "datasets"}],
                    "metadata_workers": 1,
                },
            },
            "runtime": {
                "import_path": "flotilla.integrations.harbor_native:NativeFlotillaRuntime",
                "kwargs": {"deployment": str(deployment_file), "runtime_params": {"SEED": "2"}},
            },
            "environment": {"import_path": "flotilla.integrations.harbor_native:NativeFlotillaEnvironment"},
            "agent": {"import_path": f"{__name__}:FailingAgent"} if outcome == "agent_failure" else {"name": "nop"},
            "verifier": {"disable": outcome == "disabled"},
            "artifacts": [{"source": "/evidence/run-extra.txt", "destination": "run-extra.txt"}],
            "extra_instructions": ["Run-only extra instructions must not change platform metadata identity."],
            "trial_name": "native",
            "trials_dir": tmp_path / "trials",
        }
    )
    trial = await Trial.create(config)
    assert isinstance(trial.task, Task)
    assert isinstance(trial.runtime_context, NativeFlotillaRuntime)
    assert isinstance(trial.agent_environment, NativeFlotillaEnvironment)
    assert trial.runtime_context.expected_identity == metadata_identity(
        NativeTaskSource([SourceMapping(local_root=root, platform_prefix="datasets")]).read_metadata("datasets/task")
    )
    assert prepared == [] and units == []
    result = await trial.run()
    assert len(prepared) == 1 and prepared[0][1] == {"SEED": "2"}
    if missing_reward:
        assert result.exception_info is not None
        assert "Reward" in result.exception_info.exception_type
        assert result.verifier_result is None
    elif outcome == "agent_failure":
        assert result.exception_info is not None
        assert result.exception_info.exception_type == "RuntimeError"
        assert "neutral agent failed" in result.exception_info.exception_message
        assert result.verifier_result is None
    else:
        assert result.exception_info is None, result.exception_info
        if verification_runs:
            assert result.verifier_result.rewards == {"reward": 1.0}
        else:
            assert result.verifier_result is None
    persisted = json.loads((tmp_path / "trials/native/result.json").read_text())
    assert (persisted["exception_info"] is not None) == (missing_reward or outcome == "agent_failure")
    if outcome == "success":
        assert persisted["verifier_result"]["rewards"] == {"reward": 1.0}
    assert len(units) == (3 if mode == "sidecar" else 2 if separate and verification_runs else 1)
    if separate and verification_runs:
        verifier_iid = units[-1]
        for main_iid in units[:-1]:
            assert events.index(("delete", main_iid)) < events.index(("create", verifier_iid))
    workspace_key = prepared[0][0]
    assert not any(key == workspace_key or key.startswith(workspace_key + "/") for key in tree.dirs | tree.files)
    assert not await platform.list({})
    # Trial artifacts outlive both platform resources and scratch handoff archives,
    # including artifacts declared only on TrialConfig rather than in task.toml.
    artifacts_dir = tmp_path / "trials/native/artifacts"
    assert (artifacts_dir / "main.txt").read_text() == "main-owned\n"
    assert (artifacts_dir / "run-extra.txt").read_text() == "run-extra-owned\n"
    manifest = json.loads((artifacts_dir / "manifest.json").read_text())
    successful = {
        (entry["source"], entry["destination"], entry["service"]) for entry in manifest if entry["status"] == "ok"
    }
    assert ("/evidence/main.txt", "artifacts/main.txt", None) in successful
    assert ("/evidence/run-extra.txt", "artifacts/run-extra.txt", None) in successful
    if mode == "sidecar":
        assert (artifacts_dir / "db.txt").read_text() == "sidecar-owned\n"
        assert ("/evidence/db.txt", "artifacts/db.txt", "db") in successful
