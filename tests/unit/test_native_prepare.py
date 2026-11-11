"""Host select/prepare: atomic all-row releases, USER0 dedup, explicit mirror, resume."""

from __future__ import annotations

import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("harbor")
from harbor.models.task.config import TaskConfig

from flotilla.build.builder import BuildRequest
from flotilla.build.driver import BuildContext
from flotilla.build.images import BuildSettings, ImageError
from flotilla.integrations.native_import import SelectedTask
from flotilla.native import ReleaseReader, read_dataset
from flotilla.platform.base import Capabilities
from flotilla.prepare import PreparationError, prepare, select
from flotilla.testing.build import DIGEST, FakeBuilder, FakeExport, FakeRegistry, _caps, _ctx
from flotilla.testing.compose import OfflineComposeAdapter

RUNTIME_A = "sha256:" + "ef" * 32
RUNTIME_B = "sha256:" + "9a" * 32


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


def test_select_require_prebuilt_images_rejects_dockerfile_services(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "src", image=None, Dockerfile="FROM r.io/base:1\n")
    report = tmp_path / "r.json"
    with pytest.raises(PreparationError, match="zero tasks"):
        select(
            root,
            caps=_caps(),
            report=report,
            compose_adapter=ADAPTER(),
            require_prebuilt_images=True,
        )
    reason = json.loads(report.read_text())[0]["reason"]
    assert "must be published first" in reason and "main" in reason
    result = select(root, caps=_caps(), compose_adapter=ADAPTER())  # generic Flotilla may build
    assert [row.task_key for row in result.rows] == ["src"]


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
    builder = FakeBuilder()
    result = prepare(
        _selection(tmp_path),
        out=out,
        image_repo="r.io/ns/out",
        report=report,
        context=_ctx(
            builder=builder,
            image_export=FakeExport({"/var/lib/data": {"seed.txt": "from-image"}}),
        ),
        compose_adapter=ADAPTER(),
    )
    # Complete release layout published by a single rename; staging is gone.
    assert (out / "manifest.json").is_file() and (out / "dataset.jsonl").is_file()
    assert not list(tmp_path.glob(f".{out.name}-*"))
    rows = read_dataset(out / "dataset.jsonl")
    assert len(rows) == 2
    assert all(row.release_id == result.release_id for row in rows)
    assert all(r["status"] == "prepared" for r in result.report)
    assert json.loads(report.read_text())["rows"] == [dict(r) for r in result.report]
    assert json.loads(report.read_text())["metrics"] == result.metrics
    phases = result.metrics["phases"]
    assert phases["import_normalize"]["calls"] == 2
    assert phases["image_config"]["calls"] == 2  # two distinct images, not three services
    assert phases["image_extract"]["calls"] == 1
    assert phases["user0_build_push"]["calls"] == phases["application_build_push"]["calls"] == 0
    assert phases["registry_copy"]["calls"] == 0
    assert phases["package"]["calls"] == 5  # two tasks, two assets, final publication
    assert all(phase["seconds"] >= 0 and phase["failed"] == 0 for phase in phases.values())
    assert result.metrics["elapsed_s"] >= phases["package"]["seconds"]

    reader = ReleaseReader(out)
    tasks = {task.key: task for task in (reader.read_task(row) for row in rows)}
    assert set(tasks) == {"plain", "seeded"}
    assert sorted(reader.instruction(row) for row in rows) == ["Do it.\n", "Solve the task.\n"]
    # Root-user images are linked by digest; nothing was built or copied.
    assert builder.requests == []
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
    # Volume seed becomes a static asset with preserved ownership metadata.
    asset = next(a for a in seeded_task.assets if (out / a / "seeds").is_dir())
    volumes = seeded_task.roles["main"].trial_volumes
    assert any(v.seed and v.seed.startswith(f"{asset}/seeds/") for v in volumes)
    with tarfile.open(next((out / asset / "seeds").glob("*.tar"))) as archive:
        stream = archive.extractfile("seed.txt")
        assert stream and stream.read() == b"from-image"
    # Harbor presentation metadata: the original task configuration, no host paths.
    config = TaskConfig.model_validate_toml((root / "seeded" / "task.toml").read_text())
    provenance = seeded_task.provenance
    assert provenance["importer"] == "harbor-v1"
    assert provenance["harbor"]["config"] == config.model_dump(mode="json")
    assert provenance["source_images"] == {
        "main": {"main": f"r.io/ns/app@{DIGEST}", "db": f"r.io/b@{DIGEST}"},
    }
    assert provenance["adaptation"] == "user0-v1"
    assert str(tmp_path) not in json.dumps(provenance)


# ───────────────────────────── prepare: atomicity ─────────────────────────────


def test_prepare_failure_publishes_nothing_but_reports_every_row(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "alpha", tests=True)
    broken = harbor_task(root, "broken", tests=True)
    (broken / "tests" / "test.sh").unlink()  # fails at packaging, after preflight passed
    out, report = tmp_path / "release", tmp_path / "report.json"
    with pytest.raises(PreparationError, match="no release published"):
        prepare(
            _selection(tmp_path),
            out=out,
            image_repo="r.io/ns/out",
            report=report,
            context=_ctx(),
            compose_adapter=ADAPTER(),
        )
    assert not out.exists() and not list(tmp_path.glob(f".{out.name}-*"))
    by_key = {r["task_key"]: r for r in json.loads(report.read_text())["rows"]}
    assert by_key["alpha"]["status"] == "prepared"
    assert by_key["broken"]["status"] == "failed" and "test.sh" in by_key["broken"]["reason"]
    assert json.loads(report.read_text())["images"] == [f"r.io/ns/app@{DIGEST}"]


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
    result = prepare([row, row], out=out, context=_ctx(), compose_adapter=ADAPTER())
    assert len(list((out / "tasks").iterdir())) == 1  # identical rows share one task entry
    rows = read_dataset(out / "dataset.jsonl")
    assert len(rows) == 2
    assert all(r.annotations == {"split": "train"} and r.parameters == {"SEED": "7"} for r in rows)
    assert all(r["status"] == "prepared" for r in result.report)
    with pytest.raises(FileExistsError):
        prepare([row], out=out, context=_ctx(), compose_adapter=ADAPTER())


# ───────────────────────────── USER0 adaptation ─────────────────────────────


def test_prepare_adapts_nonroot_images_once_per_digest(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(
        root,
        "alpha",
        tests=True,
        extra_toml='[verifier]\n[verifier.environment]\ndocker_image = "r.io/ns/app:1"\n',
    )
    harbor_task(root, "beta")  # shares the source digest with alpha
    harbor_task(root, "gamma", image="r.io/other:1")
    builder = FakeBuilder(digests=[RUNTIME_A, RUNTIME_B])
    out = tmp_path / "release"
    result = prepare(
        _selection(tmp_path),
        out=out,
        image_repo="r.io/ns/out",
        context=_ctx(builder=builder, registry=FakeRegistry(user="1000:1001", published=builder.published)),
        compose_adapter=ADAPTER(),
    )
    assert len(builder.requests) == 2  # one per distinct (target, image) pair
    assert result.metrics["phases"]["user0_build_push"]["calls"] == 2
    assert result.metrics["phases"]["role_prepare"]["calls"] == 4  # three main, one verifier
    assert all(r.tag.startswith("r.io/ns/out:user0-") for r in builder.requests)
    assert len({r.tag for r in builder.requests}) == 2
    reader = ReleaseReader(out)
    tasks = {t.key: t for t in (reader.read_task(row) for row in read_dataset(out / "dataset.jsonl"))}
    shared = f"r.io/ns/out@{RUNTIME_A}"
    alpha = tasks["alpha"]
    assert set(alpha.roles) == {"main", "verifier"}  # verifier role declared via task.toml
    for role in ("main", "verifier"):
        assert alpha.roles[role].units["main"].image == shared  # cross-role dedup
        assert alpha.roles[role].units["main"].context.uid == 1000  # app identity preserved
        assert alpha.roles[role].units["main"].context.gid == 1001
    assert alpha.verification is not None and alpha.verification.role == "verifier"
    assert tasks["beta"].roles["main"].units["main"].image == shared  # cross-task dedup
    assert tasks["gamma"].roles["main"].units["main"].image == f"r.io/ns/out@{RUNTIME_B}"
    for task in tasks.values():
        assert task.provenance["adaptation"] == "user0-v1"
        assert task.provenance["source_images"]["main"]["main"] == (
            f"r.io/other@{DIGEST}" if task.key == "gamma" else f"r.io/ns/app@{DIGEST}"
        )
    assert set(result.images) == {shared, f"r.io/ns/out@{RUNTIME_B}"}


def test_prepare_requires_image_repo_before_any_publication(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "plain")
    builder, registry = FakeBuilder(), FakeRegistry(user="1000:1001")
    out, report = tmp_path / "release", tmp_path / "report.json"
    with pytest.raises(PreparationError, match="prepare preflight failed"):
        prepare(
            _selection(tmp_path),
            out=out,
            report=report,
            context=_ctx(
                builder=builder,
                registry=registry,
                settings=BuildSettings(pullable_registries=("r.io",)),
            ),
            compose_adapter=ADAPTER(),
        )
    assert builder.requests == [] and registry.copied == [] and not out.exists()
    reason = next(r for r in json.loads(report.read_text())["rows"] if r["status"] == "failed")["reason"]
    assert "--image-repo" in reason
    phases = json.loads(report.read_text())["metrics"]["phases"]
    assert phases["image_preflight"]["failed"] == 1
    assert phases["user0_build_push"]["calls"] == phases["package"]["calls"] == 0


def test_prepare_image_repo_overrides_context_target(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "plain")
    builder = FakeBuilder(digests=[RUNTIME_A])
    prepare(
        _selection(tmp_path),
        out=tmp_path / "release",
        image_repo="r.io/x/y",
        context=_ctx(builder=builder, registry=FakeRegistry(user="1000:1001", published=builder.published)),
        compose_adapter=ADAPTER(),
    )
    assert [r.tag.split(":")[0] for r in builder.requests] == ["r.io/x/y"]


# ───────────────────────────── explicit mirror ─────────────────────────────


def test_prepare_mirrors_only_when_explicitly_configured(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "public", image="docker.io/library/redis:7")
    builder, registry = FakeBuilder(), FakeRegistry()
    out, report = tmp_path / "release", tmp_path / "report.json"
    with pytest.raises(PreparationError, match="prepare preflight failed"):
        prepare(
            _selection(tmp_path),
            out=out,
            report=report,
            context=_ctx(builder=builder, registry=registry),
            compose_adapter=ADAPTER(),
        )
    failure = next(r for r in json.loads(report.read_text())["rows"] if r["status"] == "failed")
    assert "mirror_images" in failure["reason"]
    assert registry.copied == [] and builder.requests == [] and not out.exists()

    mirror, mirror_builder = FakeRegistry(), FakeBuilder()
    ctx = _ctx(
        builder=mirror_builder,
        registry=mirror,
        settings=BuildSettings(pullable_registries=("r.io",), mirror_images=True, target="r.io/ns/out"),
    )
    result = prepare(
        _selection(tmp_path),
        out=tmp_path / "mirrored",
        context=ctx,
        compose_adapter=ADAPTER(),
    )
    assert mirror_builder.requests == []  # mirroring is a registry copy, not a build
    assert len(mirror.copied) == 1
    assert result.metrics["phases"]["registry_copy"]["calls"] == 1
    assert result.metrics["phases"]["user0_build_push"]["calls"] == 0
    source, target = mirror.copied[0]
    assert source == f"docker.io/library/redis@{DIGEST}" and target.startswith("r.io/ns/out:mirror-")
    reader = ReleaseReader(result.output)
    task = reader.read_task(read_dataset(result.output / "dataset.jsonl")[0])
    assert task.roles["main"].units["main"].image == f"r.io/ns/out@{DIGEST}"  # remote digest read-back
    assert result.images == (f"r.io/ns/out@{DIGEST}",)


# ───────────────────────────── mount-path inspection ─────────────────────────────


def test_prepare_inspects_tmpfs_paths_without_building(tmp_path: Path) -> None:
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
    builder, export = FakeBuilder(), FakeExport()
    enhanced = replace(_caps(), native_tmpfs=True, readonly_rootfs=True)
    result = prepare(
        _selection(tmp_path, caps=enhanced),
        out=tmp_path / "release",
        context=_ctx(builder=builder, image_export=export, caps=enhanced),
        compose_adapter=ADAPTER(),
    )
    assert builder.requests == []  # mount directories are runtime responsibilities
    assert export.inspected == [(f"r.io/ns/app@{DIGEST}", ("/cache", "/scratch", "/.flotilla-runtime"))]
    reader = ReleaseReader(result.output)
    task = reader.read_task(read_dataset(result.output / "dataset.jsonl")[0])
    unit = task.roles["main"].units["main"]
    assert [m.mount_path for m in unit.tmpfs_mounts] == ["/cache", "/scratch"]


# ───────────────────────────── resume ─────────────────────────────


class PushOutageBuilder(FakeBuilder):
    """Serves the first build, then simulates a registry outage mid-prepare."""

    def build(self, request: BuildRequest) -> str:
        if self.requests:
            raise ImageError("registry unavailable during push")
        return super().build(request)


def _nonroot_context(builder: FakeBuilder) -> BuildContext:
    return _ctx(builder=builder, registry=FakeRegistry(user="1000:1001", published=builder.published))


def test_prepare_resume_reuses_pushed_adaptations(tmp_path: Path) -> None:
    root = tmp_path / "harbor"
    harbor_task(root, "alpha")
    harbor_task(root, "beta", image="r.io/other:1")
    selected = _selection(tmp_path)
    out, report = tmp_path / "release", tmp_path / "report.json"
    work = tmp_path / f".{out.name}.prepare"
    with pytest.raises(PreparationError, match="no release published"):
        prepare(
            selected,
            out=out,
            image_repo="r.io/ns/out",
            report=report,
            context=_nonroot_context(PushOutageBuilder(digests=[RUNTIME_A])),
            compose_adapter=ADAPTER(),
        )
    assert not out.exists()
    state = json.loads((work / "state.json").read_text())
    assert len(state["adaptations"]) == 1  # alpha's adaptation was pushed before the outage
    assert json.loads(report.read_text())["images"] == [f"r.io/ns/out@{RUNTIME_A}"]
    interrupted = json.loads(report.read_text())["metrics"]["phases"]["user0_build_push"]
    assert interrupted["calls"] == 2 and interrupted["failed"] == 1

    # Interrupted work is never silently reused: resume is explicit or rejected.
    with pytest.raises(ValueError, match="explicit resume"):
        prepare(
            selected,
            out=out,
            image_repo="r.io/ns/out",
            context=_nonroot_context(FakeBuilder()),
            compose_adapter=ADAPTER(),
        )
    with pytest.raises(ValueError, match="resume requires existing evidence"):
        prepare(
            selected,
            out=tmp_path / "fresh",
            resume=True,
            image_repo="r.io/ns/out",
            context=_nonroot_context(FakeBuilder()),
            compose_adapter=ADAPTER(),
        )
    empty_work = tmp_path / ".partial.prepare"
    empty_work.mkdir()
    with pytest.raises(PreparationError, match="resume requires work evidence"):
        prepare(
            selected,
            out=tmp_path / "partial",
            resume=True,
            work_dir=empty_work,
            context=_nonroot_context(FakeBuilder()),
            compose_adapter=ADAPTER(),
        )
    (root / "alpha" / "instruction.md").write_text("Changed.\n")
    with pytest.raises(PreparationError, match="resume inputs changed"):
        prepare(
            selected,
            out=out,
            resume=True,
            image_repo="r.io/ns/out",
            context=_nonroot_context(FakeBuilder()),
            compose_adapter=ADAPTER(),
        )
    (root / "alpha" / "instruction.md").write_text("Do it.\n")

    resumed = FakeBuilder(digests=[RUNTIME_B])
    result = prepare(
        selected,
        out=out,
        resume=True,
        image_repo="r.io/ns/out",
        context=_nonroot_context(resumed),
        compose_adapter=ADAPTER(),
    )
    assert len(resumed.requests) == 1 and resumed.requests[0].tag.startswith("r.io/ns/out:user0-")
    assert result.metrics["counts"]["adaptations_resumed"] == 1
    assert result.metrics["phases"]["user0_build_push"]["calls"] == 1
    assert result.metrics["phases"]["user0_build_push"]["failed"] == 0
    reader = ReleaseReader(out)
    tasks = {t.key: t for t in (reader.read_task(row) for row in read_dataset(out / "dataset.jsonl"))}
    assert tasks["alpha"].roles["main"].units["main"].image == f"r.io/ns/out@{RUNTIME_A}"  # reused, not rebuilt
    assert tasks["beta"].roles["main"].units["main"].image == f"r.io/ns/out@{RUNTIME_B}"
    assert set(result.images) == {f"r.io/ns/out@{RUNTIME_A}", f"r.io/ns/out@{RUNTIME_B}"}


def test_prepare_metrics_measure_real_calls_not_seed_cache_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import flotilla.prepare.release as module

    clock = [10.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class TimedExport(FakeExport):
        def extract(
            self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
        ) -> bool:
            clock[0] += 3.5
            return super().extract(image, path, dest, identities=identities)

    root = tmp_path / "harbor"
    for name in ("alpha", "beta"):
        harbor_task(
            root,
            name,
            compose="""services:
  main:
    volumes:
      - {type: volume, source: data, target: /data}
volumes:
  data: {}
""",
        )
    result = prepare(
        _selection(tmp_path),
        out=tmp_path / "release",
        context=_ctx(image_export=TimedExport({"/data": {"seed": "same"}})),
        compose_adapter=ADAPTER(),
    )
    phases = result.metrics["phases"]
    assert phases["image_extract"] == {"calls": 1, "failed": 0, "seconds": 3.5}
    assert phases["seed_cache_copy"]["calls"] == 2
    assert result.metrics["counts"]["seed_cache_hits"] == 1
    assert result.metrics["counts"]["registry_cache_hits"] > 0
    assert phases["role_prepare"]["seconds"] == 3.5  # inclusive, not a second 3.5 s cost
    assert result.metrics["elapsed_s"] == 3.5
    assert "metrics" not in (result.output / "manifest.json").read_text()


def test_prepare_metrics_separate_application_build_from_user0(tmp_path: Path) -> None:
    harbor_task(tmp_path / "harbor", "application", image=None, Dockerfile="FROM r.io/base:1\n")
    builder = FakeBuilder()
    result = prepare(
        _selection(tmp_path),
        out=tmp_path / "release",
        context=_ctx(builder=builder),
        compose_adapter=ADAPTER(),
    )
    assert len(builder.requests) == 1
    assert result.metrics["phases"]["application_build_push"]["calls"] == 1
    assert result.metrics["phases"]["user0_build_push"]["calls"] == 0
