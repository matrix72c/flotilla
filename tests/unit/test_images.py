"""Image disposition: classification (link / mirror / build), ImageRef parsing, digest resolution and mirror copy."""

from __future__ import annotations

import pytest

from flotilla.build.images import (
    BuildSettings,
    ImageError,
    ImageRef,
    Registry,
    pin_images,
    plan_image,
    plan_images,
    resolve_images,
)
from flotilla.compose.model import MAIN, Build, Project, Service, VolumeMount
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


# ───────────────────────────── Classification ─────────────────────────────


def test_pullable_matches_on_segment_boundary() -> None:
    s = BuildSettings(pullable_registries=("registry.example.com", "other.io/team"))
    assert s.pullable(ImageRef.parse("registry.example.com/ns/x:1"))
    assert s.pullable(ImageRef.parse("other.io/team/x:1"))
    assert not s.pullable(ImageRef.parse("other.io/teamster/x:1"))  # not a substring match
    assert not s.pullable(ImageRef.parse("evil.registry.example.com/x:1"))


def test_plan_image_dispositions() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    link = plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)
    assert link.disposition == "link"
    built = plan_image(MAIN, _svc(MAIN, build=Build(context=".")), s)
    assert built.disposition == "build" and built.ref is None


def test_mirror_requires_explicit_configuration() -> None:
    # Mirroring publishes into the user's registry; it is never an implicit
    # fallback for a source the platform cannot pull.
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    with pytest.raises(ImageError, match="mirror_images"):
        plan_image("x", _svc("x", image="docker.io/library/redis:7"), s)
    explicit = BuildSettings(pullable_registries=PULLABLE, mirror_images=True, target="t.io/ns/repo")
    assert plan_image("x", _svc("x", image="docker.io/library/redis:7"), explicit).disposition == "mirror"


def test_plan_image_requires_image_or_build() -> None:
    with pytest.raises(ImageError):
        plan_image("x", _svc("x"), BuildSettings())


def test_mounts_never_plan_image_builds() -> None:
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
    assert {p.service: p.disposition for p in plans} == {
        "writes_bind": "link",
        "nocopy": "link",
        "tmpfs": "link",
        "plain": "link",
    }


# ───────────────────────────── Resolution and mirror ─────────────────────────────


class FakeRegistry(Registry):
    def __init__(self, digests: dict[str, str]) -> None:
        self._digests = digests  # name:tag → digest
        self._published: dict[str, str] = {}  # name:tag or name@digest → digest served after a copy
        self.resolved: list[tuple[str, str]] = []
        self.copied: list[tuple[str, str]] = []

    def digest(self, name: str, reference: str) -> str:
        key = f"{name}@{reference}" if reference.startswith("sha256:") else f"{name}:{reference}"
        pushed = self._published.get(key)
        if pushed is not None:
            return pushed
        self.resolved.append((name, reference))
        return self._digests[f"{name}:{reference}"]

    def image_config(self, pinned: str) -> dict[str, object]:
        return {"Cmd": ["/bin/sh"]}

    def copy(self, source: str, target: str) -> None:
        self.copied.append((source, target))
        self._published[target] = source.rpartition("@")[2]


def _project(**services: Service) -> Project:
    return Project(services=services, networks={}, volumes=frozenset())


def test_link_references_source_digest_without_copy() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, target="t.io/ns/repo")
    reg = FakeRegistry({"registry.example.com/ns/pg:16": DIGEST})
    plans = [plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)]
    res = resolve_images(plans, s, reg)
    assert res.images == {"db": f"registry.example.com/ns/pg@{DIGEST}"}
    assert reg.copied == [] and res.to_build == ()


def test_mirror_copies_once_and_references_target() -> None:
    s = BuildSettings(pullable_registries=PULLABLE, mirror_images=True, target="t.io/ns/repo")
    reg = FakeRegistry({"docker.io/library/redis:7": DIGEST})
    # Two services reference the same public image: resolved once, copied once,
    # and the recorded reference is the remote digest read after the copy.
    plans = [
        plan_image("a", _svc("a", image="docker.io/library/redis:7"), s),
        plan_image("b", _svc("b", image="docker.io/library/redis:7"), s),
    ]
    res = resolve_images(plans, s, reg)
    assert res.images == {"a": f"t.io/ns/repo@{DIGEST}", "b": f"t.io/ns/repo@{DIGEST}"}
    assert reg.copied == [(f"docker.io/library/redis@{DIGEST}", f"t.io/ns/repo:mirror-{'ab' * 6}")]
    assert reg.resolved == [("docker.io/library/redis", "7")]


def test_pinned_image_is_not_re_resolved() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({})
    plans = [plan_image("db", _svc("db", image=f"registry.example.com/ns/pg@{DIGEST}"), s)]
    res = resolve_images(plans, s, reg)
    assert res.images == {"db": f"registry.example.com/ns/pg@{DIGEST}"} and reg.resolved == []


def test_mirror_without_target_rejected() -> None:
    s = BuildSettings(mirror_images=True)  # no target
    reg = FakeRegistry({"docker.io/library/redis:7": DIGEST})
    plans = [plan_image("a", _svc("a", image="docker.io/library/redis:7"), s)]
    with pytest.raises(ImageError, match="target"):
        resolve_images(plans, s, reg)


def test_build_services_go_to_to_build() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.example.com/ns/pg:16": DIGEST})
    project = _project(
        main=_svc(MAIN, build=Build(context=".")),
        db=_svc("db", image="registry.example.com/ns/pg:16"),
    )
    res = resolve_images(plan_images(project, s), s, reg)
    assert res.to_build == (MAIN,)
    assert res.images == {"db": f"registry.example.com/ns/pg@{DIGEST}"}
    assert res.dispositions == {MAIN: "build", "db": "link"}


def test_resolver_rejects_bad_digest_from_registry() -> None:
    s = BuildSettings(pullable_registries=PULLABLE)
    reg = FakeRegistry({"registry.example.com/ns/pg:16": "not-a-digest"})
    plans = [plan_image("db", _svc("db", image="registry.example.com/ns/pg:16"), s)]
    with pytest.raises(ImageError):
        resolve_images(plans, s, reg)


def test_different_tags_in_same_repository_resolve_independently() -> None:
    settings = BuildSettings(pullable_registries=("r.io",))
    registry = FakeRegistry({"r.io/app:1": DIGEST, "r.io/app:2": DIGEST2})
    plans = [
        plan_image(name, _svc(name, image=f"r.io/app:{tag}"), settings)
        for name, tag in (("a", "1"), ("b", "2"), ("c", "1"))
    ]
    result = resolve_images(plans, settings, registry)
    assert result.images == {"a": f"r.io/app@{DIGEST}", "b": f"r.io/app@{DIGEST2}", "c": f"r.io/app@{DIGEST}"}
    assert registry.resolved == [("r.io/app", "1"), ("r.io/app", "2")]


@pytest.mark.parametrize("pinned_first", [True, False])
def test_explicit_digest_does_not_replace_tag_resolution(pinned_first: bool) -> None:
    settings = BuildSettings(pullable_registries=("r.io",))
    registry = FakeRegistry({"r.io/app:latest": DIGEST2})
    refs = {"pinned": f"r.io/app@{DIGEST}", "tagged": "r.io/app"}
    plans = [plan_image(name, _svc(name, image=ref), settings) for name, ref in refs.items()]
    result = resolve_images(plans if pinned_first else plans[::-1], settings, registry)
    assert result.images == {"pinned": f"r.io/app@{DIGEST}", "tagged": f"r.io/app@{DIGEST2}"}
    assert registry.resolved == [("r.io/app", "latest")]


def test_pinning_does_not_copy_and_pins_every_mirrored_source() -> None:
    settings = BuildSettings(pullable_registries=("r.io",), target="t.io/out", mirror_images=True)
    registry = FakeRegistry({"r.io/app:1": DIGEST})
    plans = [
        plan_image("a", _svc("a", image="r.io/app:1"), settings),
        plan_image("b", _svc("b", image="r.io/app:1"), settings),
    ]
    pinned = pin_images(plans, registry)
    assert all(p.ref is not None and p.ref.digest == DIGEST for p in pinned)
    assert registry.copied == []
    resolve_images(pinned, settings, registry)
    assert registry.resolved == [("r.io/app", "1")]
    assert len(registry.copied) == 1


def test_attestations_require_oci_mediatypes() -> None:
    # Schema2 publications are the default; attestations are an explicit OCI opt-in.
    with pytest.raises(ValueError, match="oci_mediatypes"):
        BuildSettings(attestations=True)
    assert BuildSettings(oci_mediatypes=True, attestations=True).attestations
