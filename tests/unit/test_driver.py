"""任务文件导出与 `flotilla build` 驱动（§4.2 第 4、6 步与整体编排）。

驱动的测试用假 registry / builder / 镜像导出器，不碰网络与 docker：验证的是顺序、处置、跳过与错误归属。
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.driver import BuildContext, build_task, write_report
from flotilla.build.files import ExportError, export_task_files
from flotilla.build.images import BuildSettings
from flotilla.build.prebuilt import PrebuiltMismatch, PrebuiltSettings
from flotilla.compose.task import load_task
from flotilla.platform.base import Capabilities

DIGEST = "sha256:" + "ab" * 32
PULLABLE = ("r.io",)


class FakeRegistry:
    def __init__(self, *, history: Sequence[Mapping[str, Any]] = (), missing: Sequence[str] = ()) -> None:
        self._history = list(history)
        self._missing = set(missing)
        self.configs = 0

    def digest(self, name: str, reference: str) -> str:
        return DIGEST

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.configs += 1
        return {"Cmd": ["/bin/bash"], "WorkingDir": "/app", "User": "0:0"}

    def image_history(self, pinned: str) -> Sequence[Mapping[str, Any]]:
        from flotilla.build.images import ImageError

        if any(m in pinned for m in self._missing):
            raise ImageError(f"not found: {pinned}")
        return self._history

    def copy(self, source: str, target: str) -> None:
        pass


class FakeBuilder:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def build(self, request: Any) -> str:
        self.requests.append(request)
        return DIGEST


class FakeExport:
    """镜像内容导出器：`contents` 是 挂载点 → {文件名: 内容}。"""

    def __init__(self, contents: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.contents = contents or {}

    def extract(self, image: str, path: str, dest: Path) -> bool:
        files = self.contents.get(path)
        if not files:
            return False
        for name, text in files.items():
            (dest / name).write_text(text)
        return True


def _caps() -> Capabilities:
    return Capabilities(
        max_label_value_len=63,
        list_visibility_s=30.0,
        max_ttl_seconds=21600,
        link=True,
        link_udp=True,
        link_max_members=10,
        inbound_isolation=True,
        external_forms=frozenset({"none", "allowlist"}),
        implicit_egress=(),
        internal_address="exec",
        shared_volume=True,
        volume_file=True,
        no_auto_mount=True,
        exec_auth=True,
        privileged_runtime=False,
        devices=frozenset(),
    )


def _task(root: Path, name: str, *, toml: str = "", compose: str | None = None, **files: str) -> Path:
    path = root / name
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text(f'[task]\nname = "ds/{name}"\n{toml}')
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    for rel, text in files.items():
        target = path / "environment" / rel.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return path


def _ctx(**kw: Any) -> BuildContext:
    defaults: dict[str, Any] = {
        "settings": BuildSettings(pullable_registries=PULLABLE, target="r.io/ns/out"),
        "caps": _caps(),
        "registry": FakeRegistry(),
        "builder": FakeBuilder(),
        "image_export": FakeExport(),
    }
    return BuildContext(**{**defaults, **kw})


# ───────────────────────────── 导出任务文件 ─────────────────────────────


def test_export_copies_platform_mounted_binds(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "binds",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n"
                "      - {type: bind, source: ./shared, target: /data, read_only: true}\n"
                "  web:\n    image: r.io/c:1\n"
                "    volumes:\n"
                "      - {type: bind, source: ./shared, target: /mirror, read_only: true}\n"
            ),
            **{"shared__f.txt": "payload"},
        )
    )
    files = tmp_path / "files"
    exported = export_task_files(task.project, task.env_dir, files)
    (key,) = exported.binds.values()
    assert (files / key / "f.txt").read_text() == "payload"


def test_export_skips_binds_written_into_the_image(tmp_path: Path) -> None:
    # 单写者单引用 → 写进镜像（mode="image"），不进任务文件。
    task = load_task(
        _task(
            tmp_path,
            "img",
            compose=(
                "services:\n  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: bind, source: ./seed, target: /seed}\n"
            ),
            **{"seed__f.txt": "x"},
        )
    )
    files = tmp_path / "files"
    assert export_task_files(task.project, task.env_dir, files).binds == {}


def test_export_writes_seed_tar_for_shared_volume(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "vol",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /var/lib/data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /mnt/data}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    files = tmp_path / "files"
    export = FakeExport({"/var/lib/data": {"seed.txt": "from-image"}})
    exported = export_task_files(
        task.project, task.env_dir, files, images={"main": "r.io/a@" + DIGEST}, image_export=export
    )
    key = exported.seeds["data"]
    with tarfile.open(files / key) as tar:
        assert tar.getnames() == ["seed.txt"]
    assert not (files / "_seed-staging").exists()  # 暂存目录清理掉


def test_export_no_seed_when_image_path_is_empty(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "empty",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    exported = export_task_files(
        task.project, task.env_dir, tmp_path / "files", images={"main": "x"}, image_export=FakeExport({})
    )
    assert exported.seeds == {}  # 空卷就是空的，与 Docker 一致


def test_export_requires_exporter_for_seeds(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "noexp",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    with pytest.raises(ExportError, match="data"):
        export_task_files(task.project, task.env_dir, tmp_path / "files")


# ───────────────────────────── 驱动 ─────────────────────────────


def test_build_links_pullable_image_without_building(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "linked", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    ctx = _ctx()
    out = build_task(task, tmp_path / "out", ctx)
    assert out.status == "built" and out.dispositions == {"main": "link"} and out.built == ()
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    assert manifest["units"]["main"]["image"] == f"r.io/ns/app@{DIGEST}"
    assert manifest["units"]["main"]["context"]["cwd"] == "/app"  # 来自镜像元数据
    assert ctx.builder.requests == []  # type: ignore[union-attr]


def test_build_runs_builder_for_build_service(tmp_path: Path) -> None:
    path = _task(tmp_path, "built")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\nRUN echo hi\n")
    ctx = _ctx()
    out = build_task(load_task(path), tmp_path / "out", ctx)
    assert out.status == "built" and out.dispositions == {"main": "build"} and out.built == ("main",)
    (request,) = ctx.builder.requests  # type: ignore[union-attr]
    assert request.dockerfile == "Dockerfile" and request.service == "main"


def test_build_skips_when_manifest_exists(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "idem", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    out_dir = tmp_path / "out"
    first = build_task(task, out_dir, _ctx())
    second = build_task(task, out_dir, _ctx())
    assert (first.status, second.status) == ("built", "skipped")
    assert second.build_key == first.build_key
    assert build_task(task, out_dir, _ctx(), force=True).status == "built"


def test_build_reports_rejected_task_without_building(tmp_path: Path) -> None:
    # no-network：扫描拒绝（首版不声明 disable_internet），不进入构建。
    task = load_task(
        _task(tmp_path, "offline", toml='[environment]\ndocker_image = "r.io/a:1"\nnetwork_mode = "no-network"\n')
    )
    ctx = _ctx()
    out = build_task(task, tmp_path / "out", ctx)
    assert out.status == "rejected" and out.manifest_path is None
    assert ctx.builder.requests == []  # type: ignore[union-attr]


def test_build_failure_is_reported_not_raised(tmp_path: Path) -> None:
    path = _task(tmp_path, "nobuilder")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\n")
    out = build_task(load_task(path), tmp_path / "out", _ctx(builder=None))
    assert out.status == "failed" and "构建器" in out.reason


def test_prebuilt_replaces_build_with_link(tmp_path: Path) -> None:
    path = _task(tmp_path, "pre")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\nWORKDIR /app\n")
    settings = BuildSettings(
        pullable_registries=PULLABLE,
        target="r.io/ns/out",
        prebuilt=PrebuiltSettings(reference="r.io/ns/pre:{task}-1"),
    )
    registry = FakeRegistry(history=[{"created_by": "WORKDIR /app"}])
    ctx = _ctx(settings=settings, registry=registry)
    out = build_task(load_task(path), tmp_path / "out", ctx)
    assert out.status == "built" and out.dispositions == {"main": "link"} and out.built == ()
    assert ctx.builder.requests == []  # type: ignore[union-attr]


def test_prebuilt_mismatch_fails_the_task(tmp_path: Path) -> None:
    path = _task(tmp_path, "drift")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\nWORKDIR /app\nRUN make\n")
    settings = BuildSettings(
        pullable_registries=PULLABLE,
        target="r.io/ns/out",
        prebuilt=PrebuiltSettings(reference="r.io/ns/pre:{task}-1"),
    )
    registry = FakeRegistry(history=[{"created_by": "WORKDIR /elsewhere"}])
    out = build_task(load_task(path), tmp_path / "out", _ctx(settings=settings, registry=registry))
    assert out.status == "failed" and PrebuiltMismatch.__name__ in out.reason


def test_missing_prebuilt_falls_back_to_building(tmp_path: Path) -> None:
    # 引用模板不覆盖这个数据集：照常构建，而不是整个任务失败。
    path = _task(tmp_path, "nopre")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\n")
    settings = BuildSettings(
        pullable_registries=PULLABLE,
        target="r.io/ns/out",
        prebuilt=PrebuiltSettings(reference="r.io/ns/pre:{task}-1"),
    )
    ctx = _ctx(settings=settings, registry=FakeRegistry(missing=["nopre"]))
    out = build_task(load_task(path), tmp_path / "out", ctx)
    assert out.status == "built" and out.built == ("main",)


def test_write_report_one_json_per_line(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "r1", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    outcomes = [build_task(task, tmp_path / "out", _ctx())]
    report = tmp_path / "report.jsonl"
    write_report(outcomes, report)
    (line,) = report.read_text().splitlines()
    assert json.loads(line)["status"] == "built"


def test_needs_publish_set_when_task_files_exported(tmp_path: Path) -> None:
    # 导出了 bind（平台挂载）→ 必须先 publish 才能运行（清单的 task_files 由发布写入，§4.7）。
    path = _task(
        tmp_path,
        "withfiles",
        compose=(
            "services:\n"
            "  main:\n    image: r.io/a:1\n"
            "  app:\n    image: r.io/b:1\n"
            "    volumes:\n      - {type: bind, source: ./shared, target: /data, read_only: true}\n"
            "  web:\n    image: r.io/c:1\n"
            "    volumes:\n      - {type: bind, source: ./shared, target: /mirror, read_only: true}\n"
        ),
        **{"shared__f.txt": "x"},
    )
    out_dir = tmp_path / "out"
    built = build_task(load_task(path), out_dir, _ctx())
    assert built.status == "built" and built.needs_publish is True
    # 跳过时也要照实报告，否则重跑会漏掉"还没发布"。
    assert build_task(load_task(path), out_dir, _ctx()).needs_publish is True


def test_needs_publish_false_without_task_files(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "nofiles", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    assert build_task(task, tmp_path / "out", _ctx()).needs_publish is False


def _shared_volume_task(root: Path, name: str) -> Path:
    return _task(
        root,
        name,
        compose=(
            "services:\n"
            "  main:\n    image: r.io/a:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data}\n"
            "  app:\n    image: r.io/b:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
            "volumes:\n  data: {}\n"
        ),
    )


def test_manifest_declares_seed_only_when_exported(tmp_path: Path) -> None:
    """清单声明的 seed 必须与实际导出的一致。

    真实部署上踩过：清单无条件声明 `seeds/<v>.tar`，而镜像在挂载点下没有内容时并不产出 tar，
    于是 trial 的 prepare 阶段去解包一个不存在的文件而失败。
    """
    path = _shared_volume_task(tmp_path, "noseed")
    out = build_task(load_task(path), tmp_path / "out", _ctx(image_export=FakeExport({})))
    assert out.status == "built"
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    (volume,) = manifest["trial_volumes"]
    assert volume["key"] == "data" and volume["seed"] is None  # 没有内容 → 不声明 seed


def test_manifest_declares_seed_when_image_has_content(tmp_path: Path) -> None:
    path = _shared_volume_task(tmp_path, "withseed")
    export = FakeExport({"/data": {"seed.txt": "x"}})
    out = build_task(load_task(path), tmp_path / "out", _ctx(image_export=export))
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    (volume,) = manifest["trial_volumes"]
    assert volume["seed"] == "seeds/data.tar"
