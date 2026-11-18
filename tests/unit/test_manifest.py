"""Volume/bind classification, manifest generation and I/O, manifest -> TrialPlan."""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.compose.layout import classify
from flotilla.compose.manifest_gen import build_manifest, synthesize_command
from flotilla.compose.model import Healthcheck, ImageMeta, Service
from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.manifest import SCHEMA_VERSION, Manifest, Resources
from flotilla.runtime.planning import AgentOverlay, PlanError, to_plan

RES = Resources(cpu="1", memory="2Gi")


def _project(services: dict[str, Any], **top: Any) -> Any:
    p = normalize({"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top})
    assert not p.rejected, p.findings
    return p


def _manifest(project: Any, meta: dict[str, ImageMeta] | None = None, **kw: Any) -> Manifest:
    meta = meta or {}
    return build_manifest(
        task="ds/task",
        project=project,
        images={n: f"reg/r@sha256:{n}" for n in project.services},
        meta={n: meta.get(n, ImageMeta()) for n in project.services},
        resources={n: RES for n in project.services},
        **kw,
    )


# ───────────────────────────── Volume and bind classification ─────────────────────────────


def _vol(source: str, target: str, ro: bool = False, kind: str = "volume") -> dict[str, Any]:
    return {"type": kind, "source": source, "target": target, "read_only": ro}


def test_named_volume_classification() -> None:
    p = _project(
        {
            "a": {"image": "a", "volumes": [_vol("ro", "/ro", True), _vol("solo", "/solo"), _vol("shared", "/s")]},
            "b": {"image": "b", "volumes": [_vol("ro", "/ro2", True), _vol("shared", "/s2", True)]},
            "c": {"image": "c", "volumes": [_vol("twice", "/x"), _vol("twice", "/y", True)]},
        },
        volumes={"ro": {}, "solo": {}, "shared": {}, "twice": {}},
    )
    modes = {v.name: v.mode for v in classify(p.services).volumes}
    # Every named volume is attempt-owned, independent of reference count and flags.
    assert modes == dict.fromkeys(("ro", "solo", "shared", "twice"), "trial")


def test_bind_source_groups() -> None:
    p = _project(
        {
            "a": {
                "image": "a",
                "volumes": [
                    _vol("./conf", "/etc/conf", True, "bind"),
                    _vol("./data", "/data", False, "bind"),
                    _vol("./script.sh", "/entry.sh", False, "bind"),
                ],
            },
            "b": {
                "image": "b",
                "volumes": [_vol("./conf/sub", "/etc/sub", True, "bind"), _vol("data", "/d", True, "bind")],
            },
        }
    )
    layout = classify(p.services)
    groups = {g.root: g for g in layout.binds}
    assert set(groups) == {"conf", "data", "script.sh"}
    assert all(group.mode == "trial" for group in groups.values())
    b_mounts = {m.target: (m.scope, m.key, m.read_only) for m in layout.mounts["b"]}
    conf = groups["conf"].index
    data = groups["data"].index
    assert b_mounts["/etc/sub"] == ("trial", f"binds/{conf}/sub", True)
    assert b_mounts["/d"] == ("trial", f"binds/{data}", True)
    assert "/entry.sh" in {m.target for m in layout.mounts["a"]}
    assert any(v.copy_from == f"binds/{data}" for v in _manifest(p).trial_volumes)


def test_bind_environment_root_and_child_share_trial_data() -> None:
    p = _project(
        {
            "main": {"image": "m", "volumes": [_vol(".", "/workspace", False, "bind")]},
            "reader": {"image": "r", "volumes": [_vol("./data", "/data", True, "bind")]},
        }
    )
    layout = classify(p.services)
    assert len(layout.binds) == 1
    assert layout.binds[0].mode == "trial"
    assert layout.mounts["main"][0].key == "binds/0"
    assert layout.mounts["reader"][0].key == "binds/0/data"
    assert layout.mounts["reader"][0].read_only
    assert _manifest(p).trial_volumes[0].copy_from == "binds/0"


@pytest.mark.parametrize("reverse", [False, True])
def test_mount_order_places_parent_before_child(reverse: bool) -> None:
    mounts = [_vol("outer", "/srv", True), _vol("./inner", "/srv/inner", True, "bind")]
    if reverse:
        mounts.reverse()
    p = _project(
        {"a": {"image": "a", "volumes": mounts}},
        volumes={"outer": {}},
    )
    assert [m.target for m in classify(p.services).mounts["a"]] == ["/srv", "/srv/inner"]


# ───────────────────────────── Command composition ─────────────────────────────


@pytest.mark.parametrize(
    ("entrypoint", "command", "meta", "expected"),
    [
        (None, None, ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/ep", "arg")),
        (None, ("x",), ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/ep", "x")),  # command overrides CMD
        (("/new",), None, ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/new",)),  # entrypoint override drops CMD
        (("/new",), ("y",), ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/new", "y")),
        (None, None, ImageMeta(), None),  # both empty: no workload process
    ],
)
def test_synthesize_command(
    entrypoint: tuple[str, ...] | None, command: tuple[str, ...] | None, meta: ImageMeta, expected: Any
) -> None:
    svc = Service(name="s", image="i", entrypoint=entrypoint, command=command)
    assert synthesize_command(svc, meta) == expected


def test_context_carries_only_declared_env_with_the_image_user() -> None:
    """The image ENV is applied by the container runtime, so only the task's own variables are sent."""
    p = _project({"app": {"image": "a", "environment": {"A": "compose", "B": "2"}, "working_dir": "/app"}})
    meta = ImageMeta(uid=33, gid=33, workdir="/var/www", cmd=("serve",))
    unit = _manifest(p, {"app": meta}).units["app"]
    assert unit.context.env == {"A": "compose", "B": "2"}
    assert (unit.context.uid, unit.context.gid, unit.context.cwd) == (33, 33, "/app")


def test_image_healthcheck_used_unless_overridden_or_disabled() -> None:
    image_hc = Healthcheck(test=("img-check",))
    meta = {n: ImageMeta(cmd=("run",), healthcheck=image_hc) for n in ("a", "b", "c")}
    p = _project(
        {
            "a": {"image": "a"},
            "b": {"image": "b", "healthcheck": {"test": ["CMD", "mine"]}},
            "c": {"image": "c", "healthcheck": {"disable": True}},
        }
    )
    units = _manifest(p, meta).units
    assert units["a"].healthcheck is not None and units["a"].healthcheck.test == ["img-check"]
    assert units["b"].healthcheck is not None and units["b"].healthcheck.test == ["mine"]
    assert units["c"].healthcheck is None


# ───────────────────────────── Manifest ─────────────────────────────


def _cve_like() -> Any:
    return _project(
        {
            "target": {"image": "t", "networks": ["inner"], "healthcheck": {"test": ["CMD", "ok"]}},
            "proxy": {
                "image": "p",
                "environment": {"UPSTREAM": "${EXAMPLE_API_UPSTREAM:-http://127.0.0.1:9}"},
                "networks": {"api": {"aliases": ["model-api-proxy"]}, "egress": {}},
            },
            "main": {
                "image": "m",
                "command": list(KEEPALIVE),
                "networks": {"inner": {"aliases": ["agent"]}, "api": {}},
                "depends_on": {"target": {"condition": "service_healthy"}},
            },
        },
        networks={"inner": {"internal": True}, "api": {"internal": True}, "egress": {}},
    )


def test_manifest_networks_names_and_external_units() -> None:
    m = _manifest(_cve_like(), {n: ImageMeta(cmd=("run",)) for n in ("target", "proxy")})
    assert m.networks["inner"].members == ["main", "target"]
    assert m.networks["inner"].names == {"agent": ["main"], "main": ["main"], "target": ["target"]}
    assert m.networks["api"].names["model-api-proxy"] == ["proxy"]
    assert m.external_units == ["proxy"]  # only proxy is on a non-internal network


def test_schema_nine_loads_with_no_static_hosts_entries() -> None:
    """Schema 10 only adds `units[].extra_hosts`; a schema-9 release keeps loading with none."""
    data = _manifest(_project({"main": {"image": "m"}})).model_dump(by_alias=True)
    data["schema"] = 9
    for unit in data["units"].values():
        unit.pop("extra_hosts")
    manifest = Manifest.load(data)
    assert manifest.schema_ == SCHEMA_VERSION
    assert all(unit.extra_hosts == {} for unit in manifest.units.values())


def test_schema_eight_drops_the_volume_seed_this_version_no_longer_writes() -> None:
    """A trial volume starts empty now; a release that seeded one from its image must be prepared again."""
    data = _manifest(
        _project({"main": {"image": "m", "volumes": [_vol("data", "/data", False, "volume")]}}, volumes={"data": {}})
    ).model_dump(by_alias=True)
    data["schema"] = 8
    data["trial_volumes"] = [{**volume, "seed": None} for volume in data["trial_volumes"]]
    assert data["trial_volumes"]  # the fixture really declares one
    manifest = Manifest.load(data)
    assert manifest.schema_ == SCHEMA_VERSION
    assert Manifest.load_json(manifest.dump_json()) == manifest

    seeded = {**data, "trial_volumes": [{**volume, "seed": "seeds/data.tar"} for volume in data["trial_volumes"]]}
    with pytest.raises(ValueError, match="prepare its release again"):
        Manifest.load(seeded)


def test_schema_seven_drops_both_fields_this_version_removed() -> None:
    """Both keys a schema-7 manifest carries, as a release on shared storage actually holds them.

    Building the fixture from the current model and adding one key back is what let a
    release prepared against schema 7 fail to load: the other key was still there.
    """
    data = _manifest(_project({"main": {"image": "m"}})).model_dump(by_alias=True)
    data["schema"] = 7
    data["runtime_params"] = []
    data["task_files_published"] = None
    manifest = Manifest.load(data)
    assert manifest.schema_ == SCHEMA_VERSION
    assert Manifest.load_json(manifest.dump_json()) == manifest

    deferred = {**data, "runtime_params": [{"service": "main", "var": "URL", "expr": "${HOST}", "variables": {}}]}
    with pytest.raises(ValueError, match="prepare its release again"):
        Manifest.load(deferred)
    published = {**data, "task_files_published": {"storage_root": "host:/mnt/shared"}}
    with pytest.raises(ValueError, match="prepare its release again"):
        Manifest.load(published)


def test_manifest_json_roundtrip_and_strictness() -> None:
    m = _manifest(_cve_like(), {n: ImageMeta(cmd=("run",)) for n in ("target", "proxy")})
    text = m.dump_json()
    assert f'"schema": {SCHEMA_VERSION}' in text
    assert Manifest.load_json(text) == m
    with pytest.raises(ValueError):
        Manifest.load_json(text.replace('"agent_service"', '"surprise": 1, "agent_service"'))  # extra="forbid"
    with pytest.raises(ValueError):
        Manifest.load_json(text.replace(f'"schema": {SCHEMA_VERSION}', '"schema": 1'))


def test_rejected_project_cannot_be_written() -> None:
    p = normalize({"services": {"main": {"image": "m", "read_only": "invalid"}}})
    with pytest.raises(ValueError):
        _manifest(p)


# ───────────────────────────── Manifest -> TrialPlan ─────────────────────────────


def test_to_plan_agent_overlay() -> None:
    m = _manifest(
        _project({"main": {"image": "m", "environment": {"PATH": "/usr/bin"}}, "db": {"image": "d"}}),
        {"main": ImageMeta(), "db": ImageMeta()},
    )
    overlay = AgentOverlay(env={"PATH": "/.flotilla/runtime/node/bin:${PATH}", "RT": "/.flotilla/runtime"})
    plan = to_plan(m, overlay=overlay)
    env = plan.units["main"].context.env
    assert env["PATH"] == "/.flotilla/runtime/node/bin:/usr/bin"  # ${PATH} refers to the service's declared value
    assert env["RT"] == "/.flotilla/runtime"
    assert "RT" not in plan.units["db"].context.env  # overlaid only onto the agent service by default


def test_to_plan_agent_overlay_rejects_an_undeclared_reference() -> None:
    """The image environment is not in the manifest, so `${PATH}` cannot stand for the image's own value."""
    m = _manifest(_project({"db": {"image": "d"}}), {"main": ImageMeta(), "db": ImageMeta()})
    with pytest.raises(PlanError, match=r"references \['PATH'\]"):
        to_plan(m, overlay=AgentOverlay(env={"PATH": "/.flotilla/runtime/node/bin:${PATH}"}))
    with pytest.raises(PlanError):
        to_plan(m, overlay=AgentOverlay(env={"A": "1"}, services=frozenset({"ghost"})))


def test_to_plan_requires_published_task_files_for_mounts() -> None:
    p = _project({"a": {"image": "a", "volumes": [_vol("./conf", "/etc/conf", True, "bind")]}})
    m = _manifest(p, {"a": ImageMeta(cmd=("run",))})
    with pytest.raises(PlanError):
        to_plan(m)  # task files not yet published
    plan = to_plan(m.model_copy(update={"task_files": "tasks/bk1"}))
    [mount] = plan.units["a"].mounts
    assert (mount.scope, mount.key, mount.read_only) == ("trial", "binds/0", True)
