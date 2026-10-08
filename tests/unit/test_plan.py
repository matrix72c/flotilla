"""trial 的纯函数层：plan 校验、depends_on 分层、拓扑与出站、hosts、卷（§3.7、§5.3、第 6、7 节）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from flotilla.core import hosts, network, topology, volumes
from flotilla.core.plan import (
    Dependency,
    ExecContext,
    Healthcheck,
    Mount,
    Network,
    TrialPlan,
    TrialVolume,
    Unit,
)
from flotilla.platform.base import ExternalPolicy, Resources

CTX = ExecContext(env={"PATH": "/usr/bin:/bin"}, uid=0, gid=0, cwd="/")
RES = Resources(cpu="1", memory="1Gi")
CALLER = ExternalPolicy(mode="allowlist", hosts=("gateway.internal",))


def _unit(**kw: object) -> Unit:
    base = Unit(image="x@sha256:1", resources=RES, context=CTX, command=("run",))
    return replace(base, **kw)  # type: ignore[arg-type]


def _web_app_db() -> TrialPlan:
    """main + web + app + db 都在 default 网络；app 依赖 db healthy。"""
    names = {
        "main": frozenset({"main"}),
        "web": frozenset({"web"}),
        "app": frozenset({"app"}),
        "db": frozenset({"db"}),
        "database": frozenset({"db"}),
    }
    return TrialPlan(
        task="web-app-db",
        task_files=None,
        units={n: _unit() for n in ("main", "web", "app")}
        | {"db": _unit(healthcheck=Healthcheck(test=("pg_isready",)))},
        networks={"default": Network(internal=False, members=frozenset(names) - {"database"}, names=names)},
        depends_on={"app": {"db": Dependency("service_healthy")}, "web": {"app": Dependency("service_started")}},
        external_units=frozenset({"main", "web", "app", "db"}),
    )


def _cve() -> TrialPlan:
    """CVE 形状：main 与 target 只在 internal 网络；proxy 同时在 internal 与 public，负责访问推理网关。"""
    return TrialPlan(
        task="cve",
        task_files=None,
        units={n: _unit() for n in ("main", "target", "proxy")},
        networks={
            "internal": Network(
                internal=True,
                members=frozenset({"main", "target", "proxy"}),
                names={
                    "main": frozenset({"main"}),
                    "target": frozenset({"target"}),
                    "model-api-proxy": frozenset({"proxy"}),
                },
            ),
            "public": Network(internal=False, members=frozenset({"proxy"}), names={"proxy": frozenset({"proxy"})}),
        },
        external_units=frozenset({"proxy"}),
    )


# ───────────────────────────── plan.validate ─────────────────────────────


def test_valid_plans_pass() -> None:
    _web_app_db().validate()
    _cve().validate()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: replace(p, units={}),
        lambda p: replace(p, networks={"n": Network(internal=False, members=frozenset({"ghost"}))}),
        lambda p: replace(p, depends_on={"app": {"ghost": Dependency("service_started")}}),
        lambda p: replace(p, depends_on={"app": {"app": Dependency("service_started")}}),
        lambda p: replace(p, external_units=frozenset({"ghost"})),
        lambda p: replace(p, extra_hosts={"ghost": {"h": "1.2.3.4"}}),
        lambda p: replace(p, units={**p.units, "x": _unit(mounts=(Mount("trial", "vol", "/data", False),))}),
        lambda p: replace(p, units={**p.units, "x": _unit(mounts=(Mount("task", "binds/0", "/etc/a", True),))}),
        lambda p: replace(p, units={**p.units, "x": _unit(command=None, restart="always")}),
        lambda p: replace(p, volumes=(TrialVolume("v"), TrialVolume("v"))),
        lambda p: replace(p, volumes=(TrialVolume("v", seed="seeds/v.tar"),)),
        lambda p: replace(p, units={**p.units, "db": _unit()}),
        lambda p: replace(
            p,
            units={**p.units, "init": _unit(command=None)},
            depends_on={"app": {"init": Dependency("service_completed_successfully")}},
        ),
    ],
    ids=[
        "no-units",
        "net-ghost",
        "dep-ghost",
        "dep-self",
        "ext-ghost",
        "extra-hosts-ghost",
        "undeclared-trial-vol",
        "task-mount-without-files",
        "restart-without-process",
        "dup-vol",
        "seed-without-files",
        "healthy-dep-without-healthcheck",
        "completed-dep-without-process",
    ],
)
def test_invalid_plans_rejected(mutate: object) -> None:
    plan = mutate(_web_app_db())  # type: ignore[operator]
    with pytest.raises(ValueError):
        plan.validate()


def test_absent_optional_dependency_is_valid() -> None:
    # §5.3：required: false 的依赖可以不存在（例如 profile 未选中）。
    plan = replace(_web_app_db(), depends_on={"app": {"metrics": Dependency("service_healthy", required=False)}})
    plan.validate()
    assert topology.layers(plan.units, plan.depends_on)[0] == ["app", "db", "main", "web"]


def test_cycle_rejected_by_validate() -> None:
    plan = replace(
        _web_app_db(),
        depends_on={"app": {"web": Dependency("service_started")}, "web": {"app": Dependency("service_started")}},
    )
    with pytest.raises(ValueError, match="成环"):
        plan.validate()


@pytest.mark.parametrize(
    ("task_files", "mount"),
    [
        ("tasks/bk", Mount("task", "", "/x", True)),  # 空 key：会挂上整个任务目录之外的东西
        ("", Mount("task", "binds/0", "/x", True)),  # 空 task_files：等于共享根目录
        ("tasks/../launches/L2", Mount("task", "binds/0", "/x", True)),  # 跳到别的 launch
        ("tasks/bk", Mount("task", "binds/0", "/x", False)),  # 任务文件只读
        ("tasks/bk", Mount("task", "a/../../b", "/x", True)),
    ],
    ids=["empty-key", "empty-task-files", "task-files-escape", "task-mount-rw", "key-escape"],
)
def test_task_file_mounts_are_confined(task_files: str, mount: Mount) -> None:
    plan = TrialPlan(task="t", task_files=task_files, units={"app": _unit(mounts=(mount,))})
    with pytest.raises(ValueError):
        plan.validate()


def test_trial_mount_may_target_subpath_of_declared_volume() -> None:
    plan = replace(
        _web_app_db(),
        volumes=(TrialVolume("binds/0", copy_from="binds/0"),),
        task_files="tasks/bk",
        units={**_web_app_db().units, "app": _unit(mounts=(Mount("trial", "binds/0/conf", "/etc/conf", True),))},
    )
    plan.validate()


def test_healthcheck_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        Healthcheck(test=())
    with pytest.raises(ValueError):
        Healthcheck(test=("true",), retries=0)


# ───────────────────────────── 分层 ─────────────────────────────


def test_layers_follow_dependencies() -> None:
    plan = _web_app_db()
    assert topology.layers(plan.units, plan.depends_on) == [["db", "main"], ["app"], ["web"]]


def test_layers_detect_cycle() -> None:
    deps = {"a": {"b": Dependency("service_started")}, "b": {"a": Dependency("service_started")}}
    with pytest.raises(ValueError):
        topology.layers(["a", "b"], deps)


def test_layers_ignore_dependency_on_absent_unit() -> None:
    # required: false 且被依赖者不存在（例如 profile 未选中）：不阻塞分层。
    deps = {"a": {"gone": Dependency("service_started", required=False)}}
    assert topology.layers(["a"], deps) == [["a"]]


# ───────────────────────────── 拓扑与出站 ─────────────────────────────


def test_topology_single_network() -> None:
    topo = network.topology(_web_app_db())
    assert topo is not None
    assert topo.networks == {"default": frozenset({"main", "web", "app", "db"})}


def test_single_unit_trial_has_no_link() -> None:
    plan = TrialPlan(
        task="solo",
        task_files=None,
        units={"main": _unit()},
        networks={"default": Network(internal=False, members=frozenset({"main"}))},
        external_units=frozenset({"main"}),
    )
    assert network.topology(plan) is None


def test_network_mode_none_unit_is_outside_topology() -> None:
    plan = replace(_web_app_db(), units={**_web_app_db().units, "loner": _unit()})
    plan.validate()
    assert "loner" not in network.link_members(plan)
    assert network.external_policy(plan, "loner", CALLER).mode == "none"


def test_internal_only_unit_gets_none_and_proxy_gets_caller_policy() -> None:
    plan = _cve()
    assert network.external_policy(plan, "main", CALLER).mode == "none"
    assert network.external_policy(plan, "target", CALLER).mode == "none"
    assert network.external_policy(plan, "proxy", CALLER) == CALLER


def test_external_unit_only_in_internal_networks_is_rejected() -> None:
    plan = replace(_cve(), external_units=frozenset({"main"}))
    with pytest.raises(ValueError):
        network.external_policy(plan, "main", CALLER)


def test_empty_network_is_dropped_from_topology() -> None:
    plan = replace(_cve(), networks={**_cve().networks, "unused": Network(internal=False, members=frozenset())})
    topo = network.topology(plan)
    assert topo is not None and "unused" not in topo.networks


# ───────────────────────────── hosts ─────────────────────────────

ADDR = {
    "main": "10.0.0.1",
    "target": "10.0.0.2",
    "proxy": "10.0.0.3",
    "web": "10.0.0.4",
    "app": "10.0.0.5",
    "db": "10.0.0.6",
}


def test_hosts_only_names_on_shared_networks() -> None:
    plan = _cve()
    # target 只与 main、proxy 共享 internal：看得到它们在 internal 上的名字，看不到 proxy 只在 public 上的名字。
    assert hosts.entries(plan, "target", ADDR) == [
        ("10.0.0.1", ("main",)),
        ("10.0.0.2", ("target",)),
        ("10.0.0.3", ("model-api-proxy",)),
    ]
    # proxy 两个网络都在：两个名字都有。
    assert ("10.0.0.3", ("model-api-proxy", "proxy")) in hosts.entries(plan, "proxy", ADDR)


def test_hosts_aliases_and_extra_hosts() -> None:
    plan = replace(_web_app_db(), extra_hosts={"app": {"metadata.internal": "169.254.169.254"}})
    lines = hosts.entries(plan, "app", ADDR)
    assert ("10.0.0.6", ("database", "db")) in lines
    assert ("169.254.169.254", ("metadata.internal",)) in lines
    assert all("metadata.internal" not in names for _, names in hosts.entries(plan, "web", ADDR))


def test_hosts_isolated_unit_sees_nothing() -> None:
    plan = replace(_web_app_db(), units={**_web_app_db().units, "loner": _unit()})
    assert hosts.entries(plan, "loner", ADDR | {"loner": "10.0.0.9"}) == []


def test_hosts_replicas_get_one_line_each() -> None:
    plan = TrialPlan(
        task="replicas",
        task_files=None,
        units={n: _unit() for n in ("main", "worker-1", "worker-2")},
        networks={
            "default": Network(
                internal=False,
                members=frozenset({"main", "worker-1", "worker-2"}),
                names={"main": frozenset({"main"}), "worker": frozenset({"worker-1", "worker-2"})},
            )
        },
    )
    addr = {"main": "10.0.0.1", "worker-1": "10.0.0.2", "worker-2": "10.0.0.3"}
    assert hosts.entries(plan, "main", addr) == [
        ("10.0.0.1", ("main",)),
        ("10.0.0.2", ("worker",)),
        ("10.0.0.3", ("worker",)),
    ]


def test_hosts_block_and_merge() -> None:
    blk = hosts.block([("10.0.0.6", ("database", "db"))])
    assert blk == "# BEGIN flotilla\n10.0.0.6\tdatabase db\n# END flotilla\n"
    original = "127.0.0.1\tlocalhost\n10.9.9.9\tsandbox-abc\n"
    merged = hosts.merge(original, blk)
    assert merged == original + blk
    # 再次合并只替换标记块，系统条目不变。
    again = hosts.merge(merged, hosts.block([("10.0.0.7", ("db",))]))
    assert again == original + "# BEGIN flotilla\n10.0.0.7\tdb\n# END flotilla\n"


def test_hosts_merge_adds_newline_if_missing() -> None:
    assert hosts.merge("127.0.0.1 localhost", "B\n") == "127.0.0.1 localhost\nB\n"
    assert hosts.merge("", "B\n") == "B\n"


def test_hosts_merge_keeps_lines_after_block() -> None:
    original = "a\n# BEGIN flotilla\nold\n# END flotilla\nz\n"
    assert hosts.merge(original, "# BEGIN flotilla\nnew\n# END flotilla\n") == (
        "a\n# BEGIN flotilla\nnew\n# END flotilla\nz\n"
    )


# ───────────────────────────── 卷 ─────────────────────────────


def test_unit_volumes_share_first_then_mounts_in_order() -> None:
    plan = TrialPlan(
        task="vols",
        task_files="tasks/bk1",
        units={
            "app": _unit(
                mounts=(
                    Mount("trial", "static", "/srv/static", False),
                    Mount("task", "binds/0", "/etc/app.conf", True),
                )
            )
        },
        volumes=(TrialVolume("static", owner=(33, 33)),),
    )
    plan.validate()
    vols = volumes.unit_volumes(plan, "app", share_release="2026-09-28.1", launch_id="L1", trial_id="t1")
    assert [(v.key, v.mount_path, v.read_only) for v in vols] == [
        ("share/releases/2026-09-28.1", "/.flotilla", True),
        ("launches/L1/t1/static", "/srv/static", False),
        ("tasks/bk1/binds/0", "/etc/app.conf", True),
    ]


def test_unit_volumes_reject_bad_key() -> None:
    plan = TrialPlan(
        task="bad",
        task_files="tasks/bk1",
        units={"app": _unit(mounts=(Mount("task", "../escape", "/x", True),))},
    )
    with pytest.raises(ValueError):
        volumes.unit_volumes(plan, "app", share_release="r", launch_id="L1", trial_id="t1")


# ───────────────────────────── 真实形状回归：cvebench2tb cve-2024-3234-zero-day ─────────────────────────────


def _cve_2024_3234() -> TrialPlan:
    """按 harbor/cvebench2tb/cvebench/cve-2024-3234-zero-day 的 Compose 手工翻译（2026-10-04）。

    secrets_init（network_mode: none）写共享卷、退出；target 等它 completed 后启动；main 等 target 与
    model-api-proxy healthy。只有 proxy 在非 internal 网络（egress_network）中。
    """
    hc = Healthcheck(test=("/bin/sh", "-c", "/evaluator/health.sh"), interval_s=5, timeout_s=5, retries=180)
    secret = Mount("trial", "secret_file_data", "/secret/file", False)
    return TrialPlan(
        task="cvebench2tb/cve-2024-3234-zero-day",
        task_files="tasks/bk",
        units={
            "main": _unit(command=("sh", "-c", "sleep infinity")),
            "secrets_init": _unit(command=("/secret/entrypoint.sh",), mounts=(secret,)),
            "target": _unit(
                command=("/entrypoint.sh", "/app/entrypoint.sh"),
                healthcheck=hc,
                mounts=(Mount("trial", "secret_file_data", "/run/cvebench-secret", False),),
            ),
            "model-api-proxy": _unit(
                command=("python3", "/opt/model-api-proxy/proxy.py"),
                healthcheck=Healthcheck(test=("curl", "-fsS", "http://127.0.0.1:8080/__health"), interval_s=2),
            ),
        },
        networks={
            "private_network": Network(
                internal=True, members=frozenset({"target"}), names={"target": frozenset({"target"})}
            ),
            "target_network": Network(
                internal=True,
                members=frozenset({"main", "target"}),
                names={"main": frozenset({"main"}), "agent": frozenset({"main"}), "target": frozenset({"target"})},
            ),
            "agent_api_network": Network(
                internal=True,
                members=frozenset({"main", "model-api-proxy"}),
                names={"main": frozenset({"main"}), "model-api-proxy": frozenset({"model-api-proxy"})},
            ),
            "egress_network": Network(
                internal=False,
                members=frozenset({"model-api-proxy"}),
                names={"model-api-proxy": frozenset({"model-api-proxy"})},
            ),
        },
        depends_on={
            "main": {"target": Dependency("service_healthy"), "model-api-proxy": Dependency("service_healthy")},
            "target": {"secrets_init": Dependency("service_completed_successfully")},
        },
        volumes=(TrialVolume("secret_file_data"),),
        external_units=frozenset({"model-api-proxy"}),
    )


def test_cve_shape_layers_network_and_hosts() -> None:
    plan = _cve_2024_3234()
    plan.validate()
    assert topology.layers(plan.units, plan.depends_on) == [["model-api-proxy", "secrets_init"], ["target"], ["main"]]

    # secrets_init 不在任何网络：不进 link、出站 none、hosts 为空。
    assert network.link_members(plan) == {"main", "target", "model-api-proxy"}
    assert network.external_policy(plan, "secrets_init", CALLER).mode == "none"
    # 只有 proxy 获得调用方的出站策略；main、target 只在 internal 网络。
    assert {u: network.external_policy(plan, u, CALLER).mode for u in plan.units} == {
        "main": "none",
        "secrets_init": "none",
        "target": "none",
        "model-api-proxy": "allowlist",
    }

    addr = {"main": "10.0.0.1", "target": "10.0.0.2", "model-api-proxy": "10.0.0.3", "secrets_init": "10.0.0.4"}
    assert hosts.entries(plan, "main", addr) == [
        ("10.0.0.1", ("agent", "main")),
        ("10.0.0.2", ("target",)),
        ("10.0.0.3", ("model-api-proxy",)),
    ]
    # target 与 proxy 不共享网络：互相解析不到（与 Compose 一致）。
    assert all("model-api-proxy" not in names for _, names in hosts.entries(plan, "target", addr))
    assert all("target" not in names for _, names in hosts.entries(plan, "model-api-proxy", addr))
    assert hosts.entries(plan, "secrets_init", addr) == []

    # secrets_init 与 target 挂同一个 trial 卷，看到同一份数据。
    keys = {
        u: [v.key for v in volumes.unit_volumes(plan, u, share_release="r", launch_id="L1", trial_id="t1")[1:]]
        for u in ("secrets_init", "target")
    }
    assert keys == {"secrets_init": ["launches/L1/t1/secret_file_data"], "target": ["launches/L1/t1/secret_file_data"]}
