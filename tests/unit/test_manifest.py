"""卷/bind 分类、清单生成与读写、清单 → TrialPlan（§7.1、§4.4、§4.5、§3.5、§10）。"""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.compose.layout import classify
from flotilla.compose.manifest_gen import ImageMeta, build_manifest, synthesize_command
from flotilla.compose.model import Healthcheck, Service
from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.integrations.plan import AgentOverlay, PlanError, to_plan
from flotilla.manifest import SCHEMA_VERSION, Manifest, Resources

RES = Resources(cpu="1", memory="2Gi")


def _project(services: dict[str, Any], **top: Any) -> Any:
    p = normalize({"services": {"main": {"image": "m", "command": list(KEEPALIVE)}, **services}, **top}, {})
    assert not p.rejected, p.findings
    return p


def _manifest(project: Any, meta: dict[str, ImageMeta] | None = None, **kw: Any) -> Manifest:
    meta = meta or {}
    return build_manifest(
        task="ds/task",
        build_key="bk1",
        project=project,
        images={n: f"reg/r@sha256:{n}" for n in project.services},
        meta={n: meta.get(n, ImageMeta()) for n in project.services},
        resources={n: RES for n in project.services},
        **kw,
    )


# ───────────────────────────── 卷与 bind 分类 ─────────────────────────────


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
    # 全部只读 → 任务文件；1 个读写 → 本地；≥2 个引用且有读写（含同一单元挂两处）→ trial 共享卷。
    assert modes == {"ro": "task_ro", "solo": "local", "shared": "trial", "twice": "trial"}


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
    assert groups["conf"].mode == "task_ro"  # 无写者，含子路径引用
    assert groups["data"].mode == "trial"  # 有写者、多引用（./data 与 data 规范化后同源）
    assert groups["script.sh"].mode == "image"  # 有写者、单引用 → 写入镜像，不挂载
    b_mounts = {m.target: (m.scope, m.key, m.read_only) for m in layout.mounts["b"]}
    conf = groups["conf"].index
    data = groups["data"].index
    assert b_mounts["/etc/sub"] == ("task", f"binds/{conf}/sub", True)
    assert b_mounts["/d"] == ("trial", f"binds/{data}", True)
    assert "/entry.sh" not in {m.target for m in layout.mounts["a"]}
    assert f"binds/{data}" in layout.trial_dirs


def test_mount_order_follows_compose() -> None:
    p = _project(
        {"a": {"image": "a", "volumes": [_vol("outer", "/srv", True), _vol("./inner", "/srv/inner", True, "bind")]}},
        volumes={"outer": {}},
    )
    assert [m.target for m in classify(p.services).mounts["a"]] == ["/srv", "/srv/inner"]


# ───────────────────────────── 命令合成（§3.5）─────────────────────────────


@pytest.mark.parametrize(
    ("entrypoint", "command", "meta", "expected"),
    [
        (None, None, ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/ep", "arg")),
        (None, ("x",), ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/ep", "x")),  # command 覆盖 CMD
        (("/new",), None, ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/new",)),  # entrypoint 覆盖后镜像 CMD 不生效
        (("/new",), ("y",), ImageMeta(entrypoint=("/ep",), cmd=("arg",)), ("/new", "y")),
        (None, None, ImageMeta(), None),  # 都为空：没有业务进程
    ],
)
def test_synthesize_command(
    entrypoint: tuple[str, ...] | None, command: tuple[str, ...] | None, meta: ImageMeta, expected: Any
) -> None:
    svc = Service(name="s", image="i", entrypoint=entrypoint, command=command)
    assert synthesize_command(svc, meta) == expected


def test_context_merges_image_env_and_user() -> None:
    p = _project({"app": {"image": "a", "environment": {"A": "compose", "B": "2"}, "working_dir": "/app"}})
    meta = ImageMeta(env={"A": "image", "PATH": "/usr/bin"}, uid=33, gid=33, workdir="/var/www", cmd=("serve",))
    unit = _manifest(p, {"app": meta}).units["app"]
    assert unit.context.env == {"A": "compose", "B": "2", "PATH": "/usr/bin"}  # environment 覆盖镜像 ENV
    assert (unit.context.uid, unit.context.gid, unit.context.cwd) == (33, 33, "/app")
    unit = _manifest(p, {"app": meta}, users={"app": (50000, 50000)}).units["app"]
    assert (unit.context.uid, unit.context.gid) == (50000, 50000)  # Compose user 覆盖镜像 USER


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


# ───────────────────────────── 清单 ─────────────────────────────


def _cve_like() -> Any:
    return _project(
        {
            "target": {"image": "t", "networks": ["inner"], "healthcheck": {"test": ["CMD", "ok"]}},
            "proxy": {
                "image": "p",
                "environment": {"UPSTREAM": "${CB2TB_API_UPSTREAM:-http://127.0.0.1:9}"},
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
    assert m.external_units == ["proxy"]  # 只有 proxy 在非 internal 网络
    assert [(p.service, p.var, p.param, p.default) for p in m.runtime_params] == [
        ("proxy", "UPSTREAM", "CB2TB_API_UPSTREAM", "http://127.0.0.1:9")
    ]


def test_manifest_json_roundtrip_and_strictness() -> None:
    m = _manifest(_cve_like(), {n: ImageMeta(cmd=("run",)) for n in ("target", "proxy")})
    text = m.dump_json()
    assert f'"schema": {SCHEMA_VERSION}' in text
    assert Manifest.load_json(text) == m
    with pytest.raises(ValueError):
        Manifest.load_json(text.replace('"build_key"', '"surprise": 1, "build_key"'))  # extra="forbid"
    with pytest.raises(ValueError):
        Manifest.load_json(text.replace(f'"schema": {SCHEMA_VERSION}', '"schema": 1'))


def test_rejected_project_cannot_be_written() -> None:
    p = normalize({"services": {"main": {"image": "m", "read_only": True}}}, {})
    with pytest.raises(ValueError):
        _manifest(p)


# ───────────────────────────── 清单 → TrialPlan ─────────────────────────────


def test_to_plan_substitutes_runtime_params() -> None:
    m = _manifest(_cve_like(), {n: ImageMeta(cmd=("run",)) for n in ("target", "proxy")})
    plan = to_plan(m, runtime_params={"CB2TB_API_UPSTREAM": "https://gw/v1"})
    assert plan.units["proxy"].context.env["UPSTREAM"] == "https://gw/v1"
    # 没配置时用默认值。
    assert to_plan(m, runtime_params={}).units["proxy"].context.env["UPSTREAM"] == "http://127.0.0.1:9"


def test_to_plan_missing_required_param() -> None:
    p = _project({"proxy": {"image": "p", "environment": {"UPSTREAM": "${NEEDED}"}}})
    m = _manifest(p, {"proxy": ImageMeta(cmd=("run",))})
    with pytest.raises(PlanError, match="NEEDED"):
        to_plan(m, runtime_params={})
    assert to_plan(m, runtime_params={"NEEDED": "x"}).units["proxy"].context.env["UPSTREAM"] == "x"


def test_to_plan_agent_overlay() -> None:
    m = _manifest(_project({"db": {"image": "d"}}), {"main": ImageMeta(env={"PATH": "/usr/bin"}), "db": ImageMeta()})
    overlay = AgentOverlay(
        env={"PATH": "/.flotilla/runtime/node/bin:${PATH}", "RT": "/.flotilla/runtime", "X": "${NOPE}"}
    )
    plan = to_plan(m, runtime_params={}, overlay=overlay)
    env = plan.units["main"].context.env
    assert env["PATH"] == "/.flotilla/runtime/node/bin:/usr/bin"  # ${PATH} 引用服务原有的值
    assert env["RT"] == "/.flotilla/runtime" and env["X"] == ""
    assert "RT" not in plan.units["db"].context.env  # 默认只叠加到 agent 服务
    with pytest.raises(PlanError):
        to_plan(m, runtime_params={}, overlay=AgentOverlay(env={"A": "1"}, services=frozenset({"ghost"})))


def test_to_plan_requires_published_task_files_for_mounts() -> None:
    p = _project({"a": {"image": "a", "volumes": [_vol("./conf", "/etc/conf", True, "bind")]}})
    m = _manifest(p, {"a": ImageMeta(cmd=("run",))})
    with pytest.raises(PlanError):
        to_plan(m, runtime_params={})  # 任务文件尚未发布
    plan = to_plan(m.model_copy(update={"task_files": "tasks/bk1"}), runtime_params={})
    [mount] = plan.units["a"].mounts
    assert (mount.scope, mount.key, mount.read_only) == ("task", "binds/0", True)
