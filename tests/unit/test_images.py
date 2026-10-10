"""镜像处置：分类（link / mirror / build）、ImageRef 解析、digest 解析与 mirror 复制。"""

from __future__ import annotations

import pytest

from flotilla.build.images import (
    BuildSettings,
    ImageError,
    ImageRef,
    Registry,
    image_changes,
    plan_image,
    plan_images,
    resolve_images,
)
from flotilla.compose.model import MAIN, Build, Project, Service, VolumeMount

DIGEST = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
PULLABLE = ("registry.h.pjlab.org.cn",)


def _svc(name: str, **kw: object) -> Service:
    return Service(name=name, **kw)  # type: ignore[arg-type]


# ───────────────────────────── ImageRef ─────────────────────────────


@pytest.mark.parametrize(
    ("ref", "registry", "repo", "tag", "digest"),
    [
        ("registry.h.pjlab.org.cn/ns/repo:t1", "registry.h.pjlab.org.cn", "ns/repo", "t1", None),
        ("registry.h.pjlab.org.cn/ns/repo", "registry.h.pjlab.org.cn", "ns/repo", None, None),
        (f"reg.io/a/b@{DIGEST}", "reg.io", "a/b", None, DIGEST),
        (f"reg.io:5000/a/b:t@{DIGEST}", "reg.io:5000", "a/b", "t", DIGEST),
        ("localhost/x:1", "localhost", "x", "1", None),
    ],
)
def test_parse(ref: str, registry: str, repo: str, tag: str | None, digest: str | None) -> None:
    r = ImageRef.parse(ref)
    assert (r.registry, r.repository, r.tag, r.digest) == (registry, repo, tag, digest)
    assert r.name == f"{registry}/{repo}"


@pytest.mark.parametrize("ref", ["ubuntu:22.04", "postgres", "a/b:c", "reg.io/a@sha256:short", "reg.io/:t"])
def test_parse_rejects(ref: str) -> None:
    with pytest.raises(ImageError):
        ImageRef.parse(ref)


# ───────────────────────────── 分类 ─────────────────────────────


def test_pullable_matches_on_segment_boundary() -> None:
    s = BuildSettings(pullable_registries=("registry.h.pjlab.org.cn", "other.io/team"))
    assert s.pullable(ImageRef.parse("registry.h.pjlab.org.cn/ns/x:1"))
    assert s.pullable(ImageRef.parse("other.io/team/x:1"))
    assert not s.pullable(ImageRef.parse("other.io/teamster/x:1"))  # 不是子串匹配
    assert not s.pullable(ImageRef.parse("evil.registry.h.pjlab.org.cn/x:1"))


def test_plan_image_dispositions() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    link = plan_image("db", _svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"), s, needs_change=False)
    assert link.disposition == "link"
    pub = plan_image("x", _svc("x", image="docker.io/library/redis:7"), s, needs_change=False)
    assert pub.disposition == "mirror"  # 不在可拉前缀下
    built = plan_image(MAIN, _svc(MAIN, build=Build(context=".")), s, needs_change=False)
    assert built.disposition == "build" and built.ref is None
    changed = plan_image("db", _svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"), s, needs_change=True)
    assert changed.disposition == "build"  # 要改镜像 → build/derive，即使是可拉的 image


def test_mirror_images_forces_mirror() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, mirror_images=True, target="t.io/ns/repo")
    assert plan_image(
        "db", _svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"), s, needs_change=False
    ).disposition == ("mirror")


def test_plan_image_requires_image_or_build() -> None:
    with pytest.raises(ImageError):
        plan_image("x", _svc("x"), BuildSettings(), needs_change=False)


def test_image_changes_from_bind_and_nocopy() -> None:
    project = Project(
        services={
            "writes_bind": _svc(
                "writes_bind",
                image="r.io/a:1",
                volumes=(VolumeMount(kind="bind", source="./seed", target="/app", read_only=False),),
            ),
            "nocopy": _svc(
                "nocopy",
                image="r.io/b:1",
                volumes=(VolumeMount(kind="volume", source="cache", target="/var/cache", nocopy=True),),
            ),
            "plain": _svc("plain", image="r.io/c:1"),
        },
        networks={},
        volumes=frozenset({"cache"}),
    )
    assert image_changes(project) == frozenset({"writes_bind", "nocopy"})


# ───────────────────────────── 解析与 mirror ─────────────────────────────


class FakeRegistry(Registry):
    def __init__(self, digests: dict[str, str]) -> None:
        self._digests = digests  # name:tag → digest
        self.resolved: list[tuple[str, str]] = []
        self.copied: list[tuple[str, str]] = []

    def digest(self, name: str, reference: str) -> str:
        self.resolved.append((name, reference))
        return self._digests[f"{name}:{reference}"]

    def image_config(self, pinned: str) -> dict[str, object]:
        return {"Cmd": ["/bin/sh"]}

    def image_history(self, pinned: str) -> list[dict[str, object]]:
        return []

    def copy(self, source: str, target: str) -> None:
        self.copied.append((source, target))


def _project(**services: Service) -> Project:
    return Project(services=services, networks={}, volumes=frozenset())


def test_link_references_source_digest_without_copy() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    reg = FakeRegistry({"registry.h.pjlab.org.cn/ns/pg:16": DIGEST})
    plans = [plan_image("db", _svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"), s, needs_change=False)]
    res = resolve_images(plans, s, reg)
    assert res.images == {"db": f"registry.h.pjlab.org.cn/ns/pg@{DIGEST}"}
    assert reg.copied == [] and res.to_build == ()


def test_mirror_copies_once_and_references_target() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    reg = FakeRegistry({"docker.io/library/redis:7": DIGEST})
    # 两个服务引用同一个公共镜像：只解析一次、只复制一次。
    plans = [
        plan_image("a", _svc("a", image="docker.io/library/redis:7"), s, needs_change=False),
        plan_image("b", _svc("b", image="docker.io/library/redis:7"), s, needs_change=False),
    ]
    res = resolve_images(plans, s, reg)
    assert res.images == {"a": f"t.io/ns/repo@{DIGEST}", "b": f"t.io/ns/repo@{DIGEST}"}
    assert reg.copied == [(f"docker.io/library/redis@{DIGEST}", f"t.io/ns/repo:mirror-{'ab' * 6}")]
    assert reg.resolved == [("docker.io/library/redis", "7")]


def test_pinned_image_is_not_re_resolved() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({})
    plans = [plan_image("db", _svc("db", image=f"registry.h.pjlab.org.cn/ns/pg@{DIGEST}"), s, needs_change=False)]
    res = resolve_images(plans, s, reg)
    assert res.images == {"db": f"registry.h.pjlab.org.cn/ns/pg@{DIGEST}"} and reg.resolved == []


def test_mirror_without_target_rejected() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)  # 无 target
    reg = FakeRegistry({"docker.io/library/redis:7": DIGEST})
    plans = [plan_image("a", _svc("a", image="docker.io/library/redis:7"), s, needs_change=False)]
    with pytest.raises(ImageError, match="target"):
        resolve_images(plans, s, reg)


def test_build_services_go_to_to_build() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.h.pjlab.org.cn/ns/pg:16": DIGEST})
    project = _project(
        main=_svc(MAIN, build=Build(context=".")),
        db=_svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"),
    )
    res = resolve_images(plan_images(project, s), s, reg)
    assert res.to_build == (MAIN,)
    assert res.images == {"db": f"registry.h.pjlab.org.cn/ns/pg@{DIGEST}"}
    assert res.dispositions == {MAIN: "build", "db": "link"}


def test_resolver_rejects_bad_digest_from_registry() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.h.pjlab.org.cn/ns/pg:16": "not-a-digest"})
    plans = [plan_image("db", _svc("db", image="registry.h.pjlab.org.cn/ns/pg:16"), s, needs_change=False)]
    with pytest.raises(ImageError):
        resolve_images(plans, s, reg)
