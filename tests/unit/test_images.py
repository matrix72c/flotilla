"""Image reference parsing, pullability and digest pinning."""

from __future__ import annotations

import pytest

from flotilla.build.images import (
    BuildSettings,
    ImageError,
    ImageRef,
    Registry,
    pin_images,
    pinned_images,
    plan_image,
    plan_images,
)
from flotilla.compose.model import Project, Service, VolumeMount
from flotilla.platform.filesystem import TmpfsMount

DIGEST = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
PULLABLE = ("registry.example.com",)


def _svc(name: str, **kw: object) -> Service:
    return Service(name=name, **kw)  # type: ignore[arg-type]


# ───────────────────────────── ImageRef ─────────────────────────────


@pytest.mark.parametrize(
    ("ref", "registry", "repo", "tag", "digest"),
    [
        ("registry.example.com/ns/repo:t1", "registry.example.com", "ns/repo", "t1", None),
        ("registry.example.com/ns/repo", "registry.example.com", "ns/repo", None, None),
        (f"reg.io/a/b@{DIGEST}", "reg.io", "a/b", None, DIGEST),
        (f"reg.io:5000/a/b:t@{DIGEST}", "reg.io:5000", "a/b", "t", DIGEST),
        ("localhost/x:1", "localhost", "x", "1", None),
        ("ubuntu:22.04", "docker.io", "library/ubuntu", "22.04", None),
        ("a/b:c", "docker.io", "a/b", "c", None),
        ("postgres", "docker.io", "library/postgres", None, None),
    ],
)
def test_parse(ref: str, registry: str, repo: str, tag: str | None, digest: str | None) -> None:
    r = ImageRef.parse(ref)
    assert (r.registry, r.repository, r.tag, r.digest) == (registry, repo, tag, digest)
    assert r.name == f"{registry}/{repo}"


@pytest.mark.parametrize("ref", ["reg.io/a@sha256:short", "reg.io/:t", "reg.io/"])
def test_parse_rejects(ref: str) -> None:
    with pytest.raises(ImageError):
        ImageRef.parse(ref)


# ───────────────────────────── Planning ─────────────────────────────


def test_pullable_matches_on_segment_boundary() -> None:
    s = BuildSettings(pullable_registries=("registry.example.com", "other.io/team"))
    assert s.pullable(ImageRef.parse("registry.example.com/ns/x:1"))
    assert s.pullable(ImageRef.parse("other.io/team/x:1"))
    assert not s.pullable(ImageRef.parse("other.io/teamster/x:1"))  # not a substring match
    assert not s.pullable(ImageRef.parse("evil.registry.example.com/x:1"))


def test_plan_image_requires_an_image() -> None:
    with pytest.raises(ImageError, match="no image"):
        plan_image("x", _svc("x"), BuildSettings())


def test_plan_image_requires_a_pullable_source() -> None:
    """Preparation consumes published images: an unpullable source is the data side's problem, not a build."""
    s = BuildSettings(pullable_registries=PULLABLE)
    assert plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s).ref.repository == "ns/pg"
    with pytest.raises(ImageError, match="not pullable"):
        plan_image("x", _svc("x", image="docker.io/library/redis:7"), s)


def test_image_replacements_apply_before_the_pullable_check() -> None:
    s = BuildSettings(
        pullable_registries=PULLABLE,
        image_replacements={"docker.io/library/redis:7": "registry.example.com/mirror/redis:7"},
    )
    assert plan_image("x", _svc("x", image="docker.io/library/redis:7"), s).ref.repository == "mirror/redis"


def test_mounts_never_change_a_service_image() -> None:
    """Writable binds, nocopy volumes, tmpfs and readonly roots are runtime mounts, not image changes."""
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
            "tmpfs": _svc(
                "tmpfs",
                image="r.io/c:1",
                tmpfs=(TmpfsMount(mount_path="/scratch"),),
                readonly_rootfs=True,
            ),
            "plain": _svc("plain", image="r.io/d:1"),
        },
        networks={},
        volumes=frozenset({"cache"}),
    )
    plans = plan_images(project, BuildSettings(pullable_registries=("r.io",)))
    assert {p.service: p.ref.repository for p in plans} == {
        "writes_bind": "a",
        "nocopy": "b",
        "tmpfs": "c",
        "plain": "d",
    }


# ───────────────────────────── Pinning ─────────────────────────────


class FakeRegistry(Registry):
    def __init__(self, digests: dict[str, str]) -> None:
        self._digests = digests  # name:tag → digest
        self.resolved: list[tuple[str, str]] = []

    def digest(self, name: str, reference: str) -> str:
        self.resolved.append((name, reference))
        return self._digests[f"{name}:{reference}"]

    def image_config(self, pinned: str) -> dict[str, object]:
        return {"Cmd": ["/bin/sh"]}


def test_tag_is_resolved_to_a_digest() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.example.com/ns/pg:16": DIGEST})
    plans = [plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)]
    assert pinned_images(pin_images(plans, reg)) == {"db": f"registry.example.com/ns/pg@{DIGEST}"}


def test_pinned_image_is_not_re_resolved() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({})
    plans = [plan_image("db", _svc("db", image=f"registry.example.com/ns/pg@{DIGEST}"), s)]
    assert pinned_images(pin_images(plans, reg)) == {"db": f"registry.example.com/ns/pg@{DIGEST}"}
    assert reg.resolved == []


def test_resolver_rejects_bad_digest_from_registry() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.example.com/ns/pg:16": "not-a-digest"})
    plans = [plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)]
    with pytest.raises(ImageError):
        pin_images(plans, reg)


def test_unpinned_plan_cannot_become_an_image_reference() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    with pytest.raises(ImageError, match="not pinned"):
        pinned_images([plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)])


def test_different_tags_in_same_repository_resolve_independently() -> None:
    settings = BuildSettings(pullable_registries=("r.io",))
    registry = FakeRegistry({"r.io/app:1": DIGEST, "r.io/app:2": DIGEST2})
    plans = [
        plan_image(name, _svc(name, image=f"r.io/app:{tag}"), settings)
        for name, tag in (("a", "1"), ("b", "2"), ("c", "1"))
    ]
    assert pinned_images(pin_images(plans, registry)) == {
        "a": f"r.io/app@{DIGEST}",
        "b": f"r.io/app@{DIGEST2}",
        "c": f"r.io/app@{DIGEST}",
    }
    assert registry.resolved == [("r.io/app", "1"), ("r.io/app", "2")]


@pytest.mark.parametrize("pinned_first", [True, False])
def test_explicit_digest_does_not_replace_tag_resolution(pinned_first: bool) -> None:
    settings = BuildSettings(pullable_registries=("r.io",))
    registry = FakeRegistry({"r.io/app:latest": DIGEST2})
    refs = {"pinned": f"r.io/app@{DIGEST}", "tagged": "r.io/app"}
    plans = [plan_image(name, _svc(name, image=ref), settings) for name, ref in refs.items()]
    resolved = pinned_images(pin_images(plans if pinned_first else plans[::-1], registry))
    assert resolved == {"pinned": f"r.io/app@{DIGEST}", "tagged": f"r.io/app@{DIGEST2}"}
    assert registry.resolved == [("r.io/app", "latest")]
