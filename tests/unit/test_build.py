"""镜像元数据解析、构建键与内容哈希、派生 Dockerfile 与镜像命名（§4.1、§4.2 第 3、5、7 步、§4.3）。"""

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
    shell_dockerfile,
    unreplaced_bases,
)
from flotilla.build.images import BuildSettings, ImageError, plan_image
from flotilla.build.keys import build_key, tree_hash
from flotilla.build.meta import parse_config, parse_user
from flotilla.compose.model import Project, Service

# ───────────────────────────── USER → 数字 ─────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1000", (1000, 1000)), ("1000:1001", (1000, 1001)), ("root", (0, 0)), ("root:root", (0, 0)), ("0:0", (0, 0))],
)
def test_parse_user_numeric_and_root(spec: str, expected: tuple[int, int]) -> None:
    assert parse_user(spec) == expected


def test_parse_user_name_needs_lookup() -> None:
    with pytest.raises(ImageError, match="/etc/passwd"):
        parse_user("appuser")
    assert parse_user("appuser", lookup=lambda _: (1500, 1600)) == (1500, 1600)
    # 名字组也要 lookup，且 gid 取解析结果的 gid。
    assert parse_user("1000:staff", lookup=lambda _: (7, 8)) == (1000, 8)


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
    assert m.env == {"PATH": "/usr/bin", "EMPTY": ""}  # 没有 `=` 的、键为空的条目都忽略
    assert m.healthcheck is not None
    assert m.healthcheck.test == ("/bin/sh", "-c", "curl -f localhost || exit 1")
    assert (m.healthcheck.interval_s, m.healthcheck.timeout_s, m.healthcheck.retries) == (5.0, 2.0, 10)
    assert m.healthcheck.start_period_s == 0.0 and m.healthcheck.start_interval_s == 5.0  # 缺失用 Docker 默认


def test_parse_config_defaults_and_none_healthcheck() -> None:
    m = parse_config({})
    assert (m.entrypoint, m.cmd, m.uid, m.gid, m.workdir, dict(m.env)) == (None, None, 0, 0, "/", {})
    assert m.healthcheck is None
    # 镜像显式关掉健康检查，以及 CMD 形式。
    assert parse_config({"Healthcheck": {"Test": ["NONE"]}}).healthcheck is None
    cmd = parse_config({"Healthcheck": {"Test": ["CMD", "/health.sh", "-v"]}}).healthcheck
    assert cmd is not None and cmd.test == ("/health.sh", "-v")


def test_parse_config_empty_entrypoint_differs_from_missing() -> None:
    # 空列表是"镜像把 ENTRYPOINT 清掉了"，与缺失（None，沿用）不同。
    assert parse_config({"Entrypoint": []}).entrypoint == ()
    assert parse_config({}).entrypoint is None


# ───────────────────────────── 内容哈希 ─────────────────────────────


def _tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hello")
    (root / "empty").mkdir()
    os.symlink("sub/a.txt", root / "link")


def test_tree_hash_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _tree(a)
    _tree(b)
    assert tree_hash(a) == tree_hash(b)
    # mtime 不计入。
    os.utime(a / "sub" / "a.txt", (0, 0))
    assert tree_hash(a) == tree_hash(b)
    # 内容、mode、符号链接目标、空目录都计入。
    (a / "sub" / "a.txt").write_bytes(b"world")
    assert tree_hash(a) != tree_hash(b)
    (a / "sub" / "a.txt").write_bytes(b"hello")
    (a / "sub" / "a.txt").chmod(0o700)
    assert tree_hash(a) != tree_hash(b)
    (a / "sub" / "a.txt").chmod((b / "sub" / "a.txt").stat().st_mode & 0o7777)
    assert tree_hash(a) == tree_hash(b)
    (a / "empty").rmdir()
    assert tree_hash(a) != tree_hash(b)


def test_tree_hash_excludes_top_level_names(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _tree(root)
    base = tree_hash(root)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref")
    assert tree_hash(root, exclude=[".git"]) == base
    assert tree_hash(root) != base


# ───────────────────────────── 构建键 ─────────────────────────────


def _project(**services: Any) -> Project:
    return Project(services={n: Service(name=n, **kw) for n, kw in services.items()}, networks={}, volumes=frozenset())


def test_build_key_changes_with_every_input() -> None:
    project = _project(main={"image": "r.io/a:1"})
    base = build_key(project=project, context_hashes={"main": "h1"}, deployment_inputs={"needs_shell": False})
    assert base == build_key(project=project, context_hashes={"main": "h1"}, deployment_inputs={"needs_shell": False})
    # Compose 变、上下文变、部署输入变，键都变（同一任务两种部署 → 两个键，§4.1）。
    assert base != build_key(
        project=_project(main={"image": "r.io/a:2"}),
        context_hashes={"main": "h1"},
        deployment_inputs={"needs_shell": False},
    )
    assert base != build_key(project=project, context_hashes={"main": "h2"}, deployment_inputs={"needs_shell": False})
    assert base != build_key(project=project, context_hashes={"main": "h1"}, deployment_inputs={"needs_shell": True})


def test_build_key_is_order_independent() -> None:
    p = _project(a={"image": "r.io/a:1"}, b={"image": "r.io/b:1"})
    assert build_key(project=p, context_hashes={"a": "1", "b": "2"}, deployment_inputs={}) == build_key(
        project=p, context_hashes={"b": "2", "a": "1"}, deployment_inputs={}
    )


# ───────────────────────────── 派生 Dockerfile ─────────────────────────────


def test_derive_dockerfile_clears_then_copies() -> None:
    df = derive_dockerfile(
        "r.io/base@sha256:ab", copy=[("entrypoint.sh", "/secret/entrypoint.sh")], clear=["/var/cache"]
    )
    lines = df.splitlines()
    assert lines[0] == "FROM r.io/base@sha256:ab"
    assert any("rm -rf /var/cache/*" in x for x in lines)
    # bind 写入前先删目标，使目录遮蔽成立（§4.4）；COPY 用 exec 形式。
    assert lines.index('RUN ["/bin/sh", "-c", "rm -rf /secret/entrypoint.sh"]') < lines.index(
        'COPY ["entrypoint.sh", "/secret/entrypoint.sh"]'
    )


def test_derive_dockerfile_quotes_odd_paths() -> None:
    df = derive_dockerfile("b", copy=[("a b.txt", "/opt/a b.txt")], clear=["/x y"])
    assert '"a b.txt"' in df and '"/opt/a b.txt"' in df and "'/x y'" in df


def test_shell_dockerfile_copies_busybox() -> None:
    df = shell_dockerfile("r.io/base@sha256:ab", "r.io/busybox@sha256:cd")
    assert "FROM r.io/busybox@sha256:cd AS shell" in df
    assert 'COPY --from=shell ["/bin/busybox", "/bin/sh"]' in df


# ───────────────────────────── 镜像命名 ─────────────────────────────


def test_image_tag_shape() -> None:
    tag = image_tag("r.io/ns/repo", "cve-bench/apereo-cas", "secrets_init", "abc123def4567890")
    assert tag == "r.io/ns/repo:cve-bench-apereo-cas--secrets_init--abc123def456"


def test_image_tag_truncates_long_task_with_hash() -> None:
    long_a, long_b = "x" * 300 + "-a", "x" * 300 + "-b"
    ta = image_tag("r.io/ns/repo", long_a, "svc", "k" * 16)
    tb = image_tag("r.io/ns/repo", long_b, "svc", "k" * 16)
    assert len(ta.split(":", 1)[1]) <= MAX_TAG and ta != tb  # 截断后仍不相撞


def test_build_request_requires_exactly_one_dockerfile(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        BuildRequest(service="s", tag="t:1", context=tmp_path)
    with pytest.raises(ImageError):
        BuildRequest(service="s", tag="t:1", context=tmp_path, dockerfile="Dockerfile", dockerfile_text="FROM x")
    assert BuildRequest(service="s", tag="t:1", context=tmp_path, dockerfile="Dockerfile").dockerfile == "Dockerfile"


# ───────────────────────────── 公共镜像替换（§4.2 第 1 步）─────────────────────────────

REPLACEMENTS = {
    "ubuntu:24.04": "registry.h.pjlab.org.cn/ns/ubuntu:24.04",
    "docker.io/library/redis:7": "registry.h.pjlab.org.cn/ns/redis:7",
}
SETTINGS = BuildSettings(pullable_registries=("registry.h.pjlab.org.cn",), image_replacements=REPLACEMENTS)

DOCKERFILE = """
# 注释里的 FROM nope:1 不算
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
    assert build_contexts(DOCKERFILE, SETTINGS) == {"ubuntu:24.04": "registry.h.pjlab.org.cn/ns/ubuntu:24.04"}


def test_unreplaced_bases_flags_unreachable_only() -> None:
    # ubuntu 有替换、ARG 插值跳过；python 既没替换也不可拉 → 必须报出来，而不是等构建超时。
    assert unreplaced_bases(DOCKERFILE, SETTINGS) == ["python:3.13-slim-bookworm"]
    # 已经指向可拉 registry 的基础镜像不算缺失。
    assert unreplaced_bases("FROM registry.h.pjlab.org.cn/ns/x:1\n", SETTINGS) == []


def test_replacement_applies_before_disposition() -> None:
    # 替换后落到可拉前缀下 → 从 mirror 变 link（§4.2 第 3 步在替换之后判定）。
    svc = Service(name="cache", image="docker.io/library/redis:7")
    assert plan_image("cache", svc, SETTINGS, needs_change=False).disposition == "link"
    plain = BuildSettings(pullable_registries=("registry.h.pjlab.org.cn",), target="t.io/ns/r")
    assert plan_image("cache", svc, plain, needs_change=False).disposition == "mirror"
