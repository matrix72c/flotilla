"""Preparation reuses base digests for config-only Dockerfiles and registry builds keyed by content."""

from __future__ import annotations

import json
from pathlib import Path

from flotilla.build.cache import build_key
from flotilla.build.driver import build_task
from flotilla.build.images import BuildSettings
from flotilla.compose.task import load_task
from flotilla.testing.build import DIGEST, DIGEST2, FakeBuilder, FakeRegistry, _ctx, _task

BASES = {"r.io/base:1": f"r.io/base@{DIGEST}"}


def _key(context: Path, **overrides: object) -> str:
    values: dict[str, object] = {
        "dockerfile": "Dockerfile",
        "dockerfile_text": (context / "Dockerfile").read_text(),
        "context": context,
        "args": {},
        "bases": BASES,
        "contexts": {},
    }
    values.update(overrides)
    return build_key(**values)  # type: ignore[arg-type]


def _context(root: Path) -> Path:
    root.mkdir()
    (root / "Dockerfile").write_text("FROM r.io/base:1\nCOPY app /app\n")
    (root / "app").write_text("payload")
    return root


def test_build_key_depends_on_content_not_location(tmp_path: Path) -> None:
    first, second = _context(tmp_path / "a"), _context(tmp_path / "b")
    assert _key(first) == _key(second)
    proxy = {"HTTP_PROXY": "http://proxy:3128", "no_proxy": "x"}
    assert _key(first, args=proxy) == _key(first)
    reference = _key(first)
    assert _key(first, args={"VERSION": "2"}) != reference
    assert _key(first, bases={"r.io/base:1": f"r.io/base@{DIGEST2}"}) != reference
    (first / "app").write_text("changed")
    assert _key(first) != reference
    (second / "app").chmod(0o755)
    assert _key(second) != reference


def test_config_only_dockerfile_reuses_base_digest(tmp_path: Path) -> None:
    path = _task(tmp_path, "folded", Dockerfile="FROM r.io/base:1\nWORKDIR /app\nENV MODE=task\n")
    builder = FakeBuilder()
    outcome = build_task(load_task(path), tmp_path / "out", _ctx(builder=builder))
    assert outcome.status == "built", outcome.reason
    assert outcome.dispositions == {"main": "config-only"} and outcome.built == ()
    assert builder.requests == []
    unit = json.loads(outcome.manifest_path.read_text())["units"]["main"]  # type: ignore[union-attr]
    assert unit["image"] == f"r.io/base@{DIGEST}"
    assert unit["context"]["cwd"] == "/app" and unit["context"]["env"] == {"MODE": "task"}


def test_config_only_on_unpullable_base_is_built(tmp_path: Path) -> None:
    path = _task(tmp_path, "public", Dockerfile="FROM docker.io/library/alpine:3\nENV A=1\n")
    builder = FakeBuilder()
    outcome = build_task(load_task(path), tmp_path / "out", _ctx(builder=builder))
    assert outcome.dispositions == {"main": "built"} and len(builder.requests) == 1


def test_config_only_on_mirrored_base_is_copied_not_built(tmp_path: Path) -> None:
    path = _task(tmp_path, "mirrored", Dockerfile="FROM r.io/base:1\n")
    builder, registry = FakeBuilder(), FakeRegistry()
    settings = BuildSettings(pullable_registries=("r.io",), target="r.io/ns/out", mirror_images=True)
    outcome = build_task(load_task(path), tmp_path / "out", _ctx(builder=builder, registry=registry, settings=settings))
    assert outcome.dispositions == {"main": "config-only"} and builder.requests == []
    assert registry.copied == [(f"r.io/base@{DIGEST}", f"r.io/ns/out:mirror-{DIGEST[7:19]}")]


def test_identical_builds_are_reused_from_the_registry(tmp_path: Path) -> None:
    dockerfile = "FROM r.io/base:1\nRUN make\n"
    builder = FakeBuilder(digests=[DIGEST2])
    ctx = _ctx(builder=builder)
    first = build_task(load_task(_task(tmp_path, "one", Dockerfile=dockerfile)), tmp_path / "out", ctx)
    second = build_task(load_task(_task(tmp_path, "two", Dockerfile=dockerfile)), tmp_path / "out", ctx)
    assert (first.dispositions, second.dispositions) == ({"main": "built"}, {"main": "cache-hit"})
    assert len(builder.requests) == 1 and ":bk-" in builder.requests[0].tag
    for outcome in (first, second):
        unit = json.loads(outcome.manifest_path.read_text())["units"]["main"]  # type: ignore[union-attr]
        assert unit["image"] == f"r.io/ns/out@{DIGEST2}"
    (tmp_path / "two" / "environment" / "Dockerfile").write_text(dockerfile + "RUN more\n")
    third = build_task(load_task(tmp_path / "two"), tmp_path / "out", ctx)
    assert third.dispositions == {"main": "built"} and len(builder.requests) == 2


def test_unpinnable_base_builds_without_cache(tmp_path: Path) -> None:
    dockerfile = "ARG BASE=r.io/base:1\nFROM $BASE\nRUN make\n"
    builder = FakeBuilder()
    ctx = _ctx(builder=builder)
    for name in ("one", "two"):
        outcome = build_task(load_task(_task(tmp_path, name, Dockerfile=dockerfile)), tmp_path / "out", ctx)
        assert outcome.dispositions == {"main": "built"}
    assert len(builder.requests) == 2 and not any(":bk-" in r.tag for r in builder.requests)


def test_user0_adaptation_is_reused_across_runs(tmp_path: Path) -> None:
    path = _task(tmp_path, "nonroot", toml='[environment]\ndocker_image = "r.io/app:1"\n')
    published: dict[str, str] = {}
    for expected_builds in (1, 0):
        builder = FakeBuilder(digests=[DIGEST2], published=published)
        registry = FakeRegistry(user="1000:1000", published=published)
        outcome = build_task(load_task(path), tmp_path / "out", _ctx(builder=builder, registry=registry))
        assert outcome.adapted == ("main",) and len(builder.requests) == expected_builds
        assert outcome.built == (("main",) if expected_builds else ())
        unit = json.loads(outcome.manifest_path.read_text())["units"]["main"]  # type: ignore[union-attr]
        assert unit["image"] == f"r.io/ns/out@{DIGEST2}"
