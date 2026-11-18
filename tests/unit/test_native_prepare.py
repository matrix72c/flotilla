"""Host select/prepare: atomic all-row releases, source-image rules, failure cleanup."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("harbor")
from harbor.models.task.config import TaskConfig

from flotilla.integrations.native_import import SelectedTask
from flotilla.native import ReleaseReader, read_dataset
from flotilla.platform.base import Capabilities
from flotilla.prepare import PreparationError, prepare, select
from flotilla.testing.build import DIGEST, FakeRegistry, _caps, _ctx
from flotilla.testing.compose import OfflineComposeAdapter


def harbor_task(
    root: Path,
    name: str,
    *,
    image: str | None = "r.io/ns/app:1",
    compose: str | None = None,
    tests: bool = False,
    instruction: str = "Do it.\n",
    extra_toml: str = "",
    **files: str,
) -> Path:
    """A Harbor task directory; extra files land inside `environment/`."""
    path = root / name
    (path / "environment").mkdir(parents=True)
    image_toml = f'[environment]\ndocker_image = "{image}"\n' if image else ""
    flat = name.rsplit("/", 1)[-1]  # task.name must be org/name with a single slash
    (path / "task.toml").write_text(f'[task]\nname = "ds/{flat}"\n\n{image_toml}{extra_toml}')
    (path / "instruction.md").write_text(instruction)
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    if tests:
        (path / "tests").mkdir()
        (path / "tests" / "test.sh").write_text("#!/bin/bash\necho ok\n")
    for rel, text in files.items():
        target = path / "environment" / rel.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return path


ADAPTER = OfflineComposeAdapter


def _selection(tmp_path: Path, caps: Capabilities | None = None) -> Path:
    out = tmp_path / "selected.jsonl"
    if not out.exists():
        select(tmp_path / "harbor", caps=caps or _caps(), out=out, compose_adapter=ADAPTER())
    return out


# ───────────────────────────── select ─────────────────────────────


def test_select_writes_rows_and_rejects_unsupported_tasks(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "plain")
    harbor_task(root, "nested/deep")
    harbor_task(root, "multi", extra_toml='[[steps]]\nname = "one"\n')
    out, report = tmp_path / "selected.jsonl", tmp_path / "report.json"
    result = select(root, caps=_caps(), out=out, report=report, compose_adapter=ADAPTER())
    assert [row.task_key for row in result.rows] == ["nested/deep", "plain"]
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["task_key"] for row in rows] == ["nested/deep", "plain"]
    assert all(row["source"] == "harbor" and Path(row["task_path"]).is_absolute() for row in rows)
    reports = json.loads(report.read_text())
    assert {r["task_key"]: r["status"] for r in reports} == {
        "multi": "rejected",
        "nested/deep": "selected",
        "plain": "selected",
    }
    assert "multi-step" in next(r["reason"] for r in reports if r["task_key"] == "multi")


def test_select_zero_selection_and_input_failures_are_fatal(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "multi", extra_toml='[[steps]]\nname = "one"\n')
    with pytest.raises(PreparationError, match="zero tasks"):
        select(root, caps=_caps(), compose_adapter=ADAPTER())
    harbor_task(root, "plain")
    broken = harbor_task(root, "broken")
    (broken / "task.toml").write_text("[[[not toml")  # parser failure, not an unsupported feature
    with pytest.raises(PreparationError, match="selection failed"):
        select(root, caps=_caps(), report=tmp_path / "r.json", compose_adapter=ADAPTER())
    reports = json.loads((tmp_path / "r.json").read_text())
    assert next(r for r in reports if r["task_key"] == "broken")["code"] == "input_or_tool_error"


def test_select_rejects_dockerfile_tasks_as_out_of_scope(tmp_path: Path) -> None:
    """Building an image is a separate job: preparation only consumes published ones."""
    root = tmp_path / "harbor"
    harbor_task(root, "src", image=None, Dockerfile="FROM r.io/base:1\n")
    report = tmp_path / "r.json"
    with pytest.raises(PreparationError, match="zero tasks"):
        select(root, caps=_caps(), report=report, compose_adapter=ADAPTER())
    entry = json.loads(report.read_text())[0]
    assert entry["status"] == "rejected"
    assert "building an image is a different job" in entry["reason"]


def test_select_rejects_multi_step_tasks_as_a_protocol_constraint(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "multi", extra_toml='[[steps]]\nname = "one"\n')
    report = tmp_path / "r.json"
    with pytest.raises(PreparationError, match="zero tasks"):  # rejection empties the selection
        select(root, caps=_caps(), report=report, compose_adapter=ADAPTER())
    reason = json.loads(report.read_text())[0]["reason"]
    assert "multi-step" in reason and "native protocol" in reason


# ───────────────────────────── prepare: happy path ─────────────────────────────


def test_prepare_publishes_one_atomic_release_for_all_rows(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "plain", instruction="Solve the task.\n")
    harbor_task(
        root,
        "seeded",
        tests=True,
        compose="""services:
  main: {}
  db:
    image: r.io/b:1
    volumes:
      - {type: volume, source: data, target: /var/lib/data}
volumes:
  data: {}
""",
    )
    out, report = tmp_path / "release", tmp_path / "report.json"
    result = prepare(
        _selection(tmp_path),
        out=out,
        name="rel",
        report=report,
        context=_ctx(),
        compose_adapter=ADAPTER(),
    )
    # Complete release layout published by a single rename; staging is gone.
    assert sorted(p.name for p in out.iterdir()) == ["dataset.jsonl", "tasks"]
    assert not list(tmp_path.glob(f".{out.name}-*"))
    rows = read_dataset(out / "dataset.jsonl")
    assert len(rows) == 2
    assert all(row.release == result.name == "rel" for row in rows)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["harbor", "release", "report.json", "selected.jsonl"]
    assert all(r["status"] == "prepared" for r in result.report)
    assert json.loads(report.read_text())["rows"] == [dict(r) for r in result.report]
    assert json.loads(report.read_text())["metrics"] == result.metrics
    phases = result.metrics["phases"]
    assert phases["import_normalize"]["calls"] == 2
    assert phases["image_config"]["calls"] == 2  # two distinct images, not three services
    assert phases["package"]["calls"] == 3  # two tasks, final publication
    assert all(phase["seconds"] >= 0 and phase["failed"] == 0 for phase in phases.values())
    assert result.metrics["elapsed_s"] >= phases["package"]["seconds"]

    reader = ReleaseReader(out)
    tasks = {task.key: task for task in (reader.read_task(row.task) for row in rows)}
    assert set(tasks) == {"plain", "seeded"}
    assert sorted(reader.instruction(row.task) for row in rows) == ["Do it.\n", "Solve the task.\n"]
    plain_task = tasks["plain"]
    assert set(plain_task.roles) == {"main"}  # no verifier environment was declared
    assert plain_task.verification is None and plain_task.artifacts == ()
    assert plain_task.roles["main"].units["main"].image == f"r.io/ns/app@{DIGEST}"
    assert set(result.images) == {f"r.io/ns/app@{DIGEST}", f"r.io/b@{DIGEST}"}

    seeded_task = tasks["seeded"]
    verification = seeded_task.verification
    assert verification is not None
    assert verification.role == "main" and verification.command == ("bash", "/tests/test.sh")
    assert verification.require_output and verification.env == {} and verification.timeout_s == 600.0
    assert verification.tests and (out / verification.tests / "test.sh").is_file()  # hidden tests staged
    assert [(a.name, a.role, a.target) for a in seeded_task.artifacts] == [("agent", "main", "/logs/agent")]
    assert verification.tests == "tasks/seeded/tests"
    # A named volume is declared, and starts empty: nothing is copied out of the image.
    assert [(v.key, v.copy_from) for v in seeded_task.roles["main"].trial_volumes] == [("data", None)]
    assert not (out / "tasks" / "seeded" / "files").exists()
    # Harbor presentation metadata: the original task configuration, no host paths.
    config = TaskConfig.model_validate_toml((root / "seeded" / "task.toml").read_text())
    provenance = seeded_task.provenance
    assert provenance["importer"] == "harbor-v1"
    assert provenance["harbor"]["config"] == config.model_dump(mode="json")
    assert provenance["source_images"] == {
        "main": {"main": f"r.io/ns/app@{DIGEST}", "db": f"r.io/b@{DIGEST}"},
    }
    assert str(tmp_path) not in json.dumps(provenance)


# ───────────────────────────── prepare: failed rows ─────────────────────────────


def test_prepare_leaves_out_failed_rows_and_reports_every_row(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "alpha", tests=True)
    broken = harbor_task(root, "broken", tests=True)
    (broken / "tests" / "test.sh").unlink()  # fails after preflight passed
    out, report = tmp_path / "release", tmp_path / "report.json"
    result = prepare(
        _selection(tmp_path),
        out=out,
        name="rel",
        report=report,
        context=_ctx(),
        compose_adapter=ADAPTER(),
    )
    assert (result.prepared, result.failed) == (1, 1)
    assert [row.task for row in read_dataset(out / "dataset.jsonl")] == ["alpha"]
    assert not (out / "tasks" / "broken").exists() and not list(tmp_path.glob(f".{out.name}-*"))
    by_key = {r["task_key"]: r for r in json.loads(report.read_text())["rows"]}
    assert by_key["alpha"]["status"] == "prepared"
    assert by_key["broken"]["status"] == "failed" and "test.sh" in by_key["broken"]["reason"]
    assert json.loads(report.read_text())["images"] == [f"r.io/ns/app@{DIGEST}"]


def test_prepare_publishes_nothing_when_every_row_fails(tmp_path: Path) -> None:
    broken = harbor_task(tmp_path / "harbor", "broken", tests=True)
    (broken / "tests" / "test.sh").unlink()
    out, report = tmp_path / "release", tmp_path / "report.json"
    with pytest.raises(PreparationError, match="no release published"):
        prepare(_selection(tmp_path), out=out, name="rel", report=report, context=_ctx(), compose_adapter=ADAPTER())
    assert not out.exists() and not list(tmp_path.glob(f".{out.name}-*"))
    assert json.loads(report.read_text())["rows"][0]["status"] == "failed"


def test_prepare_output_collision_and_identical_row_dedup(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    task = harbor_task(root, "plain")
    out = tmp_path / "release"
    row = SelectedTask(
        task_path=task,
        task_key="plain",
        annotations={"split": "train"},
        parameters={"SEED": "7"},
    )
    result = prepare([row, row], out=out, name="rel", context=_ctx(), compose_adapter=ADAPTER())
    assert len(list((out / "tasks").iterdir())) == 1  # identical rows share one task entry
    rows = read_dataset(out / "dataset.jsonl")
    assert len(rows) == 2
    assert all(r.annotations == {"split": "train"} and r.parameters == {"SEED": "7"} for r in rows)
    assert all(r["status"] == "prepared" for r in result.report)
    with pytest.raises(FileExistsError):
        prepare([row], out=out, name="rel", context=_ctx(), compose_adapter=ADAPTER())


# ───────────────────────────── non-root source images ─────────────────────────────


def test_prepare_rejects_a_nonroot_image_instead_of_adapting_it(tmp_path: Path) -> None:
    """flotilla no longer rewrites and republishes images; a non-root source is the publisher's to fix."""
    harbor_task(tmp_path / "harbor", "plain")
    out, report = tmp_path / "release", tmp_path / "report.json"
    with pytest.raises(PreparationError, match="no release published"):
        prepare(
            _selection(tmp_path),
            out=out,
            name="rel",
            report=report,
            context=_ctx(registry=FakeRegistry(user="1000:1001")),
            compose_adapter=ADAPTER(),
        )
    assert not out.exists()
    reason = next(r for r in json.loads(report.read_text())["rows"] if r["status"] == "failed")["reason"]
    assert "USER '1000:1001'" in reason and "USER 0:0" in reason
    assert json.loads(report.read_text())["metrics"]["phases"]["package"]["calls"] == 0


# ───────────────────────────── tmpfs mount targets ─────────────────────────────


def test_prepare_keeps_tmpfs_mount_paths(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(
        root,
        "ephemeral",
        compose="""services:
  main:
    image: r.io/ns/app:1
    read_only: true
    tmpfs: /cache
    volumes:
      - {type: tmpfs, target: /scratch}
""",
    )
    enhanced = replace(_caps(), native_tmpfs=True, readonly_rootfs=True)
    result = prepare(
        _selection(tmp_path, caps=enhanced),
        out=tmp_path / "release",
        name="rel",
        context=_ctx(caps=enhanced),
        compose_adapter=ADAPTER(),
    )
    reader = ReleaseReader(result.output)
    task = reader.read_task(read_dataset(result.output / "dataset.jsonl")[0].task)
    unit = task.roles["main"].units["main"]
    assert [m.mount_path for m in unit.tmpfs_mounts] == ["/cache", "/scratch"]


class _RefusingAdapter(OfflineComposeAdapter):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        object.__setattr__(self, "error", error)

    def load(self, project_dir: Path, **kwargs: Any) -> Any:
        if project_dir.parent.name == "refused":
            raise self.error  # type: ignore[attr-defined]
        return super().load(project_dir, **kwargs)


def test_select_reports_tasks_compose_refuses_and_stops_on_tool_failure(tmp_path: Path) -> None:
    from flotilla.compose.adapter import ComposeToolError

    root = tmp_path / "harbor"
    harbor_task(root, "plain")
    harbor_task(root, "refused")
    invalid = _RefusingAdapter(ValueError("official Compose config failed: services.db.volumes must be a array"))
    result = select(root, caps=_caps(), compose_adapter=invalid)
    assert [row.task_key for row in result.rows] == ["plain"]
    refused = next(r for r in result.report if r["task_key"] == "refused")
    assert (refused["status"], refused["code"]) == ("rejected", "invalid_task")
    with pytest.raises(PreparationError, match="selection failed"):
        select(root, caps=_caps(), compose_adapter=_RefusingAdapter(ComposeToolError("Compose unavailable")))


# ───────────────────────────── cache across runs ─────────────────────────────


_NAMED_USER_COMPOSE = """services:
  main:
    user: app
    volumes:
      - {type: volume, source: data, target: /data}
volumes:
  data: {}
"""


def test_prepare_cache_serves_a_second_run_without_reading_images(tmp_path: Path) -> None:
    from flotilla.build.meta import ImageAccounts

    harbor_task(tmp_path / "harbor", "alpha", compose=_NAMED_USER_COMPOSE)
    cache = tmp_path / "cache"
    reads: list[str] = []

    def accounts(image: str) -> ImageAccounts:
        reads.append(image)
        return ImageAccounts("app:x:1000:1001::/home/app:/bin/sh\n", "staff:x:1001:\n")

    def run(out: str) -> tuple[Any, FakeRegistry]:
        registry = FakeRegistry()
        result = prepare(
            _selection(tmp_path),
            out=tmp_path / out,
            name="rel",
            cache=cache,
            context=_ctx(registry=registry, image_accounts=accounts),
            compose_adapter=ADAPTER(),
        )
        return result, registry

    _, registry = run("first")
    assert registry.configs > 0 and reads == [f"r.io/ns/app@{DIGEST}"]
    second, registry = run("second")
    assert registry.configs == 0 and len(reads) == 1
    assert second.metrics["counts"]["stored_hits"] >= 2  # config, accounts
    path = Path("tasks/alpha") / "task.json"
    assert (tmp_path / "first" / path).read_bytes() == (tmp_path / "second" / path).read_bytes()
    unit = ReleaseReader(tmp_path / "second").read_task("alpha").roles["main"].units["main"]
    assert (unit.context.uid, unit.context.gid) == (1000, 1001)


# ───────────────────────────── concurrency ─────────────────────────────


def test_concurrent_tasks_read_a_shared_image_once_and_keep_row_order(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    names = ("alpha", "beta", "gamma")
    for name in names:
        harbor_task(root, name)  # one source image shared by every task
    registry = FakeRegistry()
    result = prepare(
        _selection(tmp_path),
        out=tmp_path / "release",
        name="rel",
        concurrency=len(names),
        context=_ctx(registry=registry),
        compose_adapter=ADAPTER(),
    )
    assert [row.task for row in read_dataset(tmp_path / "release" / "dataset.jsonl")] == list(names)
    reader = ReleaseReader(tmp_path / "release")
    assert {reader.read_task(n).roles["main"].units["main"].image for n in names} == {f"r.io/ns/app@{DIGEST}"}
    assert result.metrics["counts"]["registry_cache_hits"] >= len(names) - 1  # read once, reused
