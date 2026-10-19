"""Image metadata, build keys, content hashes, derived Dockerfiles, image naming."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.builder import (
    MAX_TAG,
    BuildRequest,
    base_images,
    build_contexts,
    derive_dockerfile,
    image_tag,
)
from flotilla.build.images import BuildSettings, ImageError, plan_image
from flotilla.build.keys import files_json
from flotilla.build.meta import parse_config, parse_user
from flotilla.compose.model import Project, Service

# ───────────────────────────── USER -> numeric ─────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1000", (1000, 0)), ("1000:1001", (1000, 1001)), ("0:0", (0, 0))],
)
def test_parse_user_numeric_and_root(spec: str, expected: tuple[int, int]) -> None:
    assert parse_user(spec) == expected


def test_parse_user_name_needs_lookup() -> None:
    with pytest.raises(ImageError, match="/etc/passwd"):
        parse_user("appuser")
    assert parse_user("appuser", lookup=lambda _: (1500, 1600)) == (1500, 1600)
    # Named groups need a lookup too, and gid comes from the resolved gid.
    assert parse_user("1000:staff", group_lookup=lambda _: 8) == (1000, 8)


def test_parse_user_rejects_empty() -> None:
    with pytest.raises(ImageError):
        parse_user(":1000")


# ───────────────────────────── image config ─────────────────────────────


def test_parse_config_fields() -> None:
    m = parse_config(
        {
            "Entrypoint": ["/entrypoint.sh"],
            "Cmd": ["/bin/bash"],
            "User": "1000:1001",
            "WorkingDir": "/app",
            "Env": ["PATH=/usr/bin", "EMPTY=", "NO_EQUALS", "=novalue"],
            "Healthcheck": {
                "Test": ["CMD-SHELL", "curl -f localhost || exit 1"],
                "Interval": 5_000_000_000,
                "Timeout": 2_000_000_000,
                "Retries": 10,
            },
        }
    )
    assert m.entrypoint == ("/entrypoint.sh",) and m.cmd == ("/bin/bash",)
    assert (m.uid, m.gid, m.workdir) == (1000, 1001, "/app")
    assert m.env == {"PATH": "/usr/bin", "EMPTY": ""}  # entries without `=` or with an empty key are ignored
    assert m.healthcheck is not None
    assert m.healthcheck.test == ("/bin/sh", "-c", "curl -f localhost || exit 1")
    assert (m.healthcheck.interval_s, m.healthcheck.timeout_s, m.healthcheck.retries) == (5.0, 2.0, 10)
    assert m.healthcheck.start_period_s == 0.0 and m.healthcheck.start_interval_s == 5.0  # missing uses Docker defaults


def test_parse_config_defaults_and_none_healthcheck() -> None:
    m = parse_config({})
    assert (m.entrypoint, m.cmd, m.uid, m.gid, m.workdir, dict(m.env)) == (None, None, 0, 0, "/", {})
    assert m.healthcheck is None
    # The image explicitly disables the healthcheck, and the CMD form.
    assert parse_config({"Healthcheck": {"Test": ["NONE"]}}).healthcheck is None
    cmd = parse_config({"Healthcheck": {"Test": ["CMD", "/health.sh", "-v"]}}).healthcheck
    assert cmd is not None and cmd.test == ("/health.sh", "-v")


def test_parse_config_empty_entrypoint_differs_from_missing() -> None:
    # An empty list means "the image cleared ENTRYPOINT", unlike missing (None, inherited).
    assert parse_config({"Entrypoint": []}).entrypoint == ()
    assert parse_config({}).entrypoint is None


# ───────────────────────────── Content hash ─────────────────────────────


def _tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hello")
    (root / "empty").mkdir()
    os.symlink("sub/a.txt", root / "link")


def test_file_manifest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _tree(a)
    _tree(b)
    assert files_json(a)[1] == files_json(b)[1]
    # mtime is not counted.
    os.utime(a / "sub" / "a.txt", (0, 0))
    assert files_json(a)[1] == files_json(b)[1]
    # Content, mode, symlink targets and empty directories are counted.
    (a / "sub" / "a.txt").write_bytes(b"world")
    assert files_json(a)[1] != files_json(b)[1]
    (a / "sub" / "a.txt").write_bytes(b"hello")
    (a / "sub" / "a.txt").chmod(0o700)
    assert files_json(a)[1] != files_json(b)[1]
    (a / "sub" / "a.txt").chmod((b / "sub" / "a.txt").stat().st_mode & 0o7777)
    assert files_json(a)[1] == files_json(b)[1]
    (a / "empty").rmdir()
    assert files_json(a)[1] != files_json(b)[1]


# ───────────────────────────── Build key ─────────────────────────────


def _project(**services: Any) -> Project:
    return Project(services={n: Service(name=n, **kw) for n, kw in services.items()}, networks={}, volumes=frozenset())


# ───────────────────────────── Derived Dockerfile ─────────────────────────────


def test_derive_dockerfile_clears_then_copies() -> None:
    df = derive_dockerfile(
        "r.io/base@sha256:ab", copy=[("entrypoint.sh", "/secret/entrypoint.sh")], clear=["/var/cache"]
    )
    lines = df.splitlines()
    assert lines[0] == "FROM r.io/base@sha256:ab"
    assert lines[1] == lines[-1] == "USER 0:0"  # minimal images without passwd/group can still build.
    assert any("rm -rf /var/cache/*" in x for x in lines)
    # bind writes delete the target first so directory shadowing holds; COPY uses exec form.
    assert lines.index('RUN ["/bin/sh", "-c", "rm -rf /secret/entrypoint.sh"]') < lines.index(
        'COPY ["entrypoint.sh", "/secret/entrypoint.sh"]'
    )


def test_derive_dockerfile_quotes_odd_paths() -> None:
    df = derive_dockerfile("b", copy=[("a b.txt", "/opt/a b.txt")], clear=["/x y"])
    assert '"a b.txt"' in df and '"/opt/a b.txt"' in df and "'/x y'" in df


# ───────────────────────────── Image naming ─────────────────────────────


def test_image_tag_shape() -> None:
    tag = image_tag("r.io/ns/repo", "cve-bench/apereo-cas", "secrets_init", "abc123def4567890")
    assert tag == "r.io/ns/repo:cve-bench-apereo-cas--secrets_init--abc123def456"


def test_image_tag_truncates_long_task_with_hash() -> None:
    long_a, long_b = "x" * 300 + "-a", "x" * 300 + "-b"
    ta = image_tag("r.io/ns/repo", long_a, "svc", "k" * 16)
    tb = image_tag("r.io/ns/repo", long_b, "svc", "k" * 16)
    assert len(ta.split(":", 1)[1]) <= MAX_TAG and ta != tb  # still no collision after truncation


def test_build_request_requires_exactly_one_dockerfile(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        BuildRequest(service="s", tag="t:1", context=tmp_path)
    with pytest.raises(ImageError):
        BuildRequest(service="s", tag="t:1", context=tmp_path, dockerfile="Dockerfile", dockerfile_text="FROM x")
    assert BuildRequest(service="s", tag="t:1", context=tmp_path, dockerfile="Dockerfile").dockerfile == "Dockerfile"


# ───────────────────────────── Public image replacement ─────────────────────────────

REPLACEMENTS = {
    "ubuntu:24.04": "registry.example.com/ns/ubuntu:24.04",
    "docker.io/library/redis:7": "registry.example.com/ns/redis:7",
}
SETTINGS = BuildSettings(pullable_registries=("registry.example.com",), image_replacements=REPLACEMENTS)

DOCKERFILE = """
# FROM nope:1 in a comment does not count
FROM ubuntu:24.04 AS base
RUN ["/bin/sh", "-c", "true"]
FROM base AS second
FROM python:3.13-slim-bookworm
FROM scratch
FROM ${BASE_ARG}
"""


def test_base_images_skips_stages_scratch_and_dedups() -> None:
    assert base_images(DOCKERFILE) == ["ubuntu:24.04", "python:3.13-slim-bookworm", "${BASE_ARG}"]


def test_build_contexts_only_maps_replaced() -> None:
    assert build_contexts(DOCKERFILE, SETTINGS) == {"ubuntu:24.04": "registry.example.com/ns/ubuntu:24.04"}


def test_replacement_applies_before_disposition() -> None:
    # Under a pullable prefix after replacement -> mirror becomes link (the disposition is decided after replacement).
    svc = Service(name="cache", image="docker.io/library/redis:7")
    assert plan_image("cache", svc, SETTINGS, needs_change=False).disposition == "link"
    plain = BuildSettings(pullable_registries=("registry.example.com",), target="t.io/ns/r")
    assert plan_image("cache", svc, plain, needs_change=False).disposition == "mirror"


def test_named_root_uses_the_image_database() -> None:
    with pytest.raises(ImageError, match="/etc/passwd"):
        parse_user("root")
    assert parse_user("root", lookup=lambda _: (1234, 5678)) == (1234, 5678)
    with pytest.raises(ImageError, match="group database"):
        parse_user("1000:staff", lookup=lambda _: (7, 8))
