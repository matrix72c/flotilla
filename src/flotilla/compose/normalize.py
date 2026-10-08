"""Compose → `Project`：叠加保活覆盖、插值、展开短语法、逐字段归类（Architecture §4.2 第 1 步、§3.5、§4.6）。

字段按 §4.6 的表处理；表中未列出、或数据中没有出现而首版未实现的写法一律 `reject` 并报出路径。
本模块只看 Compose 本身；与部署能力相关的判断（特权、UDP、互联等）在 scan 中按能力报告做。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from flotilla.compose.interpolate import InterpolationError, interpolate
from flotilla.compose.model import (
    MAIN,
    Build,
    Condition,
    Dependency,
    Finding,
    Healthcheck,
    NetworkAttachment,
    NetworkDef,
    Project,
    RestartPolicy,
    Service,
    VolumeMount,
)
from flotilla.compose.units import parse_duration

DEFAULT_NETWORK = "default"
KEEPALIVE = ("sh", "-c", "sleep infinity")

# `read_only: true` 一般需要 readonly_rootfs（C14），首版拒绝；但下列服务只是加固（不写任何文件），
# 只读根文件系统不是任务语义，按等效处理。按"服务名 + 入口"精确匹配，不按名字放宽。
# - 只进行 socket 转发、不写文件的推理网关代理。
READ_ONLY_EQUIVALENT: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {("model-api-proxy", ("python3", "/opt/model-api-proxy/proxy.py"))}
)

# §4.6 中"只记录"的服务字段：不影响 flotilla 的行为。
_EQUIVALENT = {
    "init": "执行守护进程收割僵尸进程",
    "labels": "只记录",
    "logging": "只记录",
    "stop_signal": "服务只随 sandbox 删除而停止（§3.5），只记录",
    "stop_grace_period": "服务只随 sandbox 删除而停止（§3.5），只记录",
    "expose": "同网络的服务之间本就全端口可达，不发布端口，只记录",
    "container_name": "名字写入其他单元的 hosts（§3.7）",
    "pull_policy": "镜像按 digest 固定（§4.2），只记录",
}
# §4.6 中拒绝的服务字段（含平台能力缺口）。
_REJECT = {
    "read_only": "需要 readonly_rootfs（C14），首版拒绝",
    "group_add": "需要 supplementary_groups（C14），首版拒绝",
    "cap_drop": "需要逐命令的能力集控制（C14），首版拒绝",
    "security_opt": "需要逐命令的能力集控制（C14），首版拒绝",
    "ulimits": "需要逐命令 rlimit（C14），首版拒绝",
    "tty": "需要 pty（C14），首版拒绝",
    "stdin_open": "需要 pty（C14），首版拒绝",
    "pid": "pid 共享需要多容器实例（C16），首版拒绝",
    "ipc": "ipc 共享需要多容器实例（C16），首版拒绝",
    "userns_mode": "隔离的 sandbox 中不成立",
    "runtime": "不支持指定容器运行时",
    "volumes_from": "首版未实现",
    "extends": "首版未实现（数据中未出现）",
    "env_file": "首版未实现（数据中未出现）",
    "profiles": "首版未实现（数据中未出现）",
    "secrets": "首版未实现（数据中未出现）",
    "configs": "首版未实现（数据中未出现）",
    "devices": "首版未实现设备（数据中未出现）",
    "sysctls": "首版未实现（数据中未出现）",
    "cap_add": "首版未实现（数据中未出现）",
    "links": "首版未实现（数据中未出现）",
    "extra_hosts": "首版未实现（数据中未出现）",
    "domainname": "首版未实现（数据中未出现）",
    "scale": "首版未实现副本（数据中未出现）",
    "deploy": "首版未实现（数据中未出现）",
    "mem_limit": "资源取自 task.toml（§4.5），不读 Compose 的限制",
    "cpus": "资源取自 task.toml（§4.5），不读 Compose 的限制",
    "shm_size": "需要 shm_size（C14），首版拒绝",
}
_HANDLED = {
    "image", "build", "command", "entrypoint", "environment", "working_dir", "user", "healthcheck", "depends_on",
    "networks", "network_mode", "volumes", "tmpfs", "restart", "privileged", "hostname", "ports",
}  # fmt: skip


class _Collector:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def equivalent(self, path: str, reason: str) -> None:
        self.findings.append(Finding(path, "equivalent", reason))

    def warn(self, path: str, reason: str) -> None:
        self.findings.append(Finding(path, "warn", reason))

    def reject(self, path: str, reason: str) -> None:
        self.findings.append(Finding(path, "reject", reason))


def harbor_overlay(compose: Mapping[str, Any], *, main_image: str | None, main_build: bool) -> dict[str, Any]:
    """在任务 Compose 之前叠加 Harbor 的保活覆盖（§3.5、§4.2 第 1 步）：`main` 的 `command` 为
    `sleep infinity`，镜像来自 task.toml 的 `docker_image` 或 environment 目录的 Dockerfile。任务文件后合并，
    可以覆盖它。

    合并规则与 `docker compose -f base -f task` 一致：服务按名字合并，标量与列表以后者为准，映射递归合并。
    """
    base_main: dict[str, Any] = {"command": list(KEEPALIVE)}
    if main_image is not None:
        base_main["image"] = main_image
    elif main_build:
        base_main["build"] = {"context": "."}
    base = {"services": {MAIN: base_main}}
    return cast(dict[str, Any], _merge(base, dict(compose)))


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base[k], v) if k in base else v
        return out
    return over


def normalize(compose: Mapping[str, Any], variables: Mapping[str, str]) -> Project:
    """规范化一个已叠加保活覆盖的 Compose 文档（见模块说明）。"""
    c = _Collector()
    for key in compose:
        if key not in ("services", "networks", "volumes", "name", "version"):
            c.reject(key, "首版未实现的顶层字段")
    raw_services = compose.get("services") or {}
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError("Compose 没有 services")

    network_defs = _networks(compose.get("networks") or {}, c)
    volumes = _top_volumes(compose.get("volumes") or {}, c)

    services: dict[str, Service] = {}
    runtime_params: dict[str, dict[str, tuple[str, str | None]]] = {}
    for name, raw in raw_services.items():
        raw = raw or {}
        path = f"services.{name}"
        env_raw = raw.get("environment") or {}
        rest = {k: v for k, v in raw.items() if k != "environment"}
        try:
            rest_i = interpolate(rest, variables)
            env_i = interpolate(env_raw, variables)
        except InterpolationError as exc:
            c.reject(path, str(exc))
            continue
        if rest_i.unresolved:
            # 只有 build.args 中的代理变量（HTTP_PROXY 等）允许未提供：它们有默认值空串，构建时生效（§4.2）。
            hard = {k: d for k, d in rest_i.unresolved.items() if d is None}
            if hard:
                c.reject(path, f"引用了任务未提供、且没有默认值的变量：{sorted(hard)}")
        params = _runtime_params(env_raw, env_i.unresolved)
        if params:
            runtime_params[name] = params
        svc = _service(name, cast(dict[str, Any], rest_i.value), env_i.value, network_defs, volumes, c)
        if svc is not None:
            services[name] = svc

    if MAIN not in services and not any(f.path.startswith(f"services.{MAIN}") for f in c.findings):
        c.reject("services", f"没有 agent 服务 {MAIN}")
    _check_references(services, network_defs, c)
    used = {n for s in services.values() for n in s.networks}
    if DEFAULT_NETWORK in used and DEFAULT_NETWORK not in network_defs:
        network_defs[DEFAULT_NETWORK] = NetworkDef()
    return Project(
        services=services,
        networks={k: v for k, v in network_defs.items() if k in used},
        volumes=frozenset(volumes),
        runtime_params=runtime_params,
        findings=tuple(c.findings),
    )


# ───────────────────────────── 服务 ─────────────────────────────


def _service(
    name: str,
    raw: dict[str, Any],
    env: object,
    networks: Mapping[str, NetworkDef],
    volumes: set[str],
    c: _Collector,
) -> Service | None:
    path = f"services.{name}"
    for key in raw:
        if key in _HANDLED:
            continue
        if key == "read_only" and raw[key] and _read_only_allowed(name, raw):
            c.equivalent(f"{path}.read_only", "该服务不写文件，只读根文件系统只是加固（READ_ONLY_EQUIVALENT）")
            continue
        if key == "read_only" and not raw[key]:
            continue
        if key in _EQUIVALENT:
            c.equivalent(f"{path}.{key}", _EQUIVALENT[key])
        elif key in _REJECT:
            c.reject(f"{path}.{key}", _REJECT[key])
        else:
            c.reject(f"{path}.{key}", "首版未实现的字段")

    if "image" not in raw and "build" not in raw:
        c.reject(path, "既没有 image 也没有 build")
    hc, hc_disabled = _healthcheck(raw.get("healthcheck"), f"{path}.healthcheck", c)
    return Service(
        name=name,
        image=_opt_str(raw.get("image"), f"{path}.image", c),
        build=_build(raw.get("build"), f"{path}.build", c),
        command=_argv(raw.get("command"), f"{path}.command", c),
        entrypoint=_argv(raw.get("entrypoint"), f"{path}.entrypoint", c),
        environment=_environment(env, f"{path}.environment", c),
        working_dir=_opt_str(raw.get("working_dir"), f"{path}.working_dir", c),
        user=_opt_str(raw.get("user"), f"{path}.user", c),
        healthcheck=hc,
        healthcheck_disabled=hc_disabled,
        depends_on=_depends_on(raw.get("depends_on"), f"{path}.depends_on", c),
        networks=_service_networks(raw, path, c),
        volumes=_volumes(raw.get("volumes"), f"{path}.volumes", volumes, c),
        tmpfs=_tmpfs(raw.get("tmpfs"), f"{path}.tmpfs", c),
        restart=_restart(raw.get("restart"), f"{path}.restart", c),
        privileged=bool(raw.get("privileged", False)),
        hostname=_hostname(raw.get("hostname"), f"{path}.hostname", c),
        udp_ports=_ports(raw.get("ports"), f"{path}.ports", c),
    )


def _read_only_allowed(name: str, raw: Mapping[str, Any]) -> bool:
    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, list):
        return False
    return (name, tuple(str(x) for x in entrypoint)) in READ_ONLY_EQUIVALENT


def _opt_str(value: object, path: str, c: _Collector) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int):
        return str(value)
    c.reject(path, f"应为字符串：{value!r}")
    return None


def _argv(value: object, path: str, c: _Collector) -> tuple[str, ...] | None:
    """`command` / `entrypoint`：列表即 exec 形式；字符串是 shell 形式，合成 `["/bin/sh","-c",…]`（§3.5）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return ("/bin/sh", "-c", value)
    if isinstance(value, list) and all(isinstance(x, str | int | float) for x in value):
        return tuple(str(x) for x in value)
    c.reject(path, f"无法识别的写法：{value!r}")
    return None


def _build(value: object, path: str, c: _Collector) -> Build | None:
    if value is None:
        return None
    if isinstance(value, str):
        return Build(context=value)
    if not isinstance(value, dict):
        c.reject(path, f"无法识别的写法：{value!r}")
        return None
    for key in value:
        if key not in ("context", "dockerfile", "args"):
            c.reject(f"{path}.{key}", "首版未实现的 build 字段")
    args = value.get("args") or {}
    if isinstance(args, list):
        args = dict(_split_kv(x) for x in args)
    return Build(
        context=str(value.get("context", ".")),
        dockerfile=_opt_str(value.get("dockerfile"), f"{path}.dockerfile", c),
        args={str(k): "" if v is None else str(v) for k, v in args.items()},
    )


def _environment(value: object, path: str, c: _Collector) -> dict[str, str]:
    """映射或 `K=V` 列表；值为 null（只写键）的条目在 Compose 中取宿主环境——这里没有宿主环境，按未设置拒绝。"""
    if value is None:
        return {}
    items: list[tuple[str, object]]
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = []
        for x in value:
            key, sep, text = str(x).partition("=")
            items.append((key, text if sep else None))
    else:
        c.reject(path, f"无法识别的写法：{value!r}")
        return {}
    out: dict[str, str] = {}
    for k, v in items:
        if v is None:
            c.reject(f"{path}.{k}", "只写了键、取宿主环境的变量；构建机环境不进入任务")
            continue
        out[str(k)] = _scalar(v)
    return out


def _scalar(v: object) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _split_kv(item: object) -> tuple[str, str]:
    k, _, v = str(item).partition("=")
    return k, v


def _healthcheck(value: object, path: str, c: _Collector) -> tuple[Healthcheck | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, dict):
        c.reject(path, f"无法识别的写法：{value!r}")
        return None, False
    if value.get("disable"):
        return None, True
    for key in value:
        if key not in ("test", "interval", "timeout", "retries", "start_period", "start_interval", "disable"):
            c.reject(f"{path}.{key}", "首版未实现的健康检查字段")
    test = value.get("test")
    argv: tuple[str, ...]
    if isinstance(test, str):
        argv = ("/bin/sh", "-c", test)
    elif isinstance(test, list) and test and test[0] == "NONE":
        return None, True
    elif isinstance(test, list) and len(test) >= 2 and test[0] == "CMD":
        argv = tuple(str(x) for x in test[1:])
    elif isinstance(test, list) and len(test) == 2 and test[0] == "CMD-SHELL":
        argv = ("/bin/sh", "-c", str(test[1]))
    elif test is None:
        c.reject(path, "只覆盖间隔、沿用镜像 HEALTHCHECK 的 test：首版未实现（需在构建时合并镜像元数据）")
        return None, False
    else:
        c.reject(f"{path}.test", f"无法识别的写法：{test!r}")
        return None, False
    try:
        hc = Healthcheck(
            test=argv,
            interval_s=parse_duration(value.get("interval", "30s"), path=f"{path}.interval"),
            timeout_s=parse_duration(value.get("timeout", "30s"), path=f"{path}.timeout"),
            retries=int(value.get("retries", 3)),
            start_period_s=parse_duration(value.get("start_period", "0s"), path=f"{path}.start_period"),
            start_interval_s=parse_duration(value.get("start_interval", "5s"), path=f"{path}.start_interval"),
        )
    except ValueError as exc:
        c.reject(path, str(exc))
        return None, False
    if hc.interval_s <= 0 or hc.timeout_s <= 0 or hc.retries < 1:
        c.reject(path, "健康检查的间隔、超时须 > 0，retries 须 >= 1")
        return None, False
    return hc, False


_CONDITIONS: tuple[Condition, ...] = ("service_started", "service_healthy", "service_completed_successfully")


def _depends_on(value: object, path: str, c: _Collector) -> dict[str, Dependency]:
    if value is None:
        return {}
    if isinstance(value, list):
        return {str(n): Dependency() for n in value}
    if not isinstance(value, dict):
        c.reject(path, f"无法识别的写法：{value!r}")
        return {}
    out: dict[str, Dependency] = {}
    for dep, spec in value.items():
        spec = spec or {}
        cond = spec.get("condition", "service_started")
        if cond not in _CONDITIONS:
            c.reject(f"{path}.{dep}.condition", f"未知的条件：{cond!r}")
            continue
        for key in spec:
            if key == "restart":
                c.equivalent(f"{path}.{dep}.restart", "只对显式重启生效，flotilla 没有重启入口（§5.3）")
            elif key not in ("condition", "required"):
                c.reject(f"{path}.{dep}.{key}", "首版未实现的 depends_on 字段")
        out[str(dep)] = Dependency(condition=cast(Condition, cond), required=bool(spec.get("required", True)))
    return out


def _service_networks(raw: Mapping[str, Any], path: str, c: _Collector) -> dict[str, NetworkAttachment]:
    mode = raw.get("network_mode")
    nets = raw.get("networks")
    if mode is not None:
        if nets:
            c.reject(path, "network_mode 与 networks 不能同时出现")
        if mode == "none":
            return {}
        reason = (
            "需要多容器实例（C16），首版拒绝"
            if str(mode).startswith(("service:", "container:"))
            else "隔离的 sandbox 中不成立"
        )
        c.reject(f"{path}.network_mode", reason)
        return {}
    if nets is None:
        return {DEFAULT_NETWORK: NetworkAttachment()}
    if isinstance(nets, list):
        return {str(n): NetworkAttachment() for n in nets}
    if not isinstance(nets, dict):
        c.reject(f"{path}.networks", f"无法识别的写法：{nets!r}")
        return {}
    out: dict[str, NetworkAttachment] = {}
    for net, spec in nets.items():
        spec = spec or {}
        for key in spec:
            if key in ("gw_priority", "priority"):
                c.equivalent(f"{path}.networks.{net}.{key}", "只影响多网卡时的默认路由；sandbox 只有一块网卡")
            elif key in ("ipv4_address", "ipv6_address"):
                c.reject(f"{path}.networks.{net}.{key}", "地址由平台分配（C7），按固定 IP 访问的服务不成立")
            elif key != "aliases":
                c.reject(f"{path}.networks.{net}.{key}", "首版未实现的网络字段")
        out[str(net)] = NetworkAttachment(aliases=tuple(str(a) for a in spec.get("aliases") or ()))
    return out


def _volumes(value: object, path: str, named: set[str], c: _Collector) -> tuple[VolumeMount, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        c.reject(path, f"无法识别的写法：{value!r}")
        return ()
    out: list[VolumeMount] = []
    for i, v in enumerate(value):
        p = f"{path}[{i}]"
        if not isinstance(v, dict):
            c.reject(p, "短语法卷首版未实现（数据中未出现），请用长语法")
            continue
        kind = v.get("type")
        target = v.get("target")
        if not isinstance(target, str) or not target.startswith("/"):
            c.reject(p, f"target 须为绝对路径：{target!r}")
            continue
        if kind == "tmpfs":
            c.equivalent(p, "构建时清空该目录，sandbox 启动时为空（§4.6）")
            continue
        if kind not in ("volume", "bind"):
            c.reject(p, f"首版未实现的卷类型：{kind!r}")
            continue
        source = v.get("source")
        if not isinstance(source, str) or not source:
            c.reject(p, "匿名卷首版未实现")
            continue
        for key in v:
            if key not in ("type", "source", "target", "read_only", "volume", "bind"):
                c.reject(f"{p}.{key}", "首版未实现的卷字段")
        nocopy = bool((v.get("volume") or {}).get("nocopy", False))
        bind_opts = v.get("bind") or {}
        for key in bind_opts:
            if key not in ("create_host_path",):
                c.reject(f"{p}.bind.{key}", "首版未实现的 bind 字段")
        if kind == "volume" and source not in named:
            c.reject(p, f"引用了未声明的 named volume：{source}")
            continue
        if kind == "bind" and (source.startswith("/") or ".." in source.split("/")):
            c.reject(p, f"bind 源在任务目录外：{source}")
            continue
        out.append(
            VolumeMount(
                kind=kind,
                source=source,
                target=target,
                read_only=bool(v.get("read_only")),
                nocopy=nocopy,
                create_host_path=bool(bind_opts.get("create_host_path", True)),
            )
        )
    return tuple(out)


def _tmpfs(value: object, path: str, c: _Collector) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        c.reject(path, f"无法识别的写法：{value!r}")
        return ()
    c.equivalent(path, "构建时清空该目录，sandbox 启动时为空；容量与内存计费不同，只记录（§4.6）")
    return tuple(str(x).split(":")[0] for x in items)


_RESTART: dict[str, RestartPolicy] = {"no": "no", "always": "always", "unless-stopped": "unless-stopped"}


def _restart(value: object, path: str, c: _Collector) -> RestartPolicy:
    if value is None or value is False:
        return "no"
    text = str(value)
    if text.startswith("on-failure"):
        return "on-failure"
    if text in _RESTART:
        if text != "no":
            c.equivalent(path, "不在运行中重启；服务实际退出、Docker 会重启它时样本作废（§3.6）")
        return _RESTART[text]
    c.reject(path, f"未知的 restart 策略：{text!r}")
    return "no"


def _hostname(value: object, path: str, c: _Collector) -> str | None:
    if value is None:
        return None
    c.warn(path, "名字解析实现（写入其他单元的 hosts）；自身主机名是缺口（C14），gethostname() 仍是平台的名字")
    return str(value)


def _ports(value: object, path: str, c: _Collector) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        c.reject(path, f"无法识别的写法：{value!r}")
        return ()
    c.equivalent(path, "同网络的服务之间本就全端口可达，不发布任何端口，只记录（§4.6）")
    udp: list[int] = []
    for p in value:
        if isinstance(p, dict) and p.get("protocol") == "udp":
            udp.append(int(p["target"]))
        elif isinstance(p, str) and p.endswith("/udp"):
            udp.append(int(p.split("/")[0].split(":")[-1]))
    return tuple(udp)


# ───────────────────────────── 顶层 ─────────────────────────────

# driver_opts 白名单（§4.6）：等效的键与值；None 表示任何值都等效。
_DRIVER_OPTS: dict[str, Callable[[str], bool]] = {
    "com.docker.network.bridge.name": lambda v: True,
    "com.docker.network.driver.mtu": lambda v: True,
    "com.docker.network.bridge.gateway_mode_ipv4": lambda v: v in ("nat", "routed", "isolated"),
    "com.docker.network.bridge.gateway_mode_ipv6": lambda v: v in ("nat", "routed", "isolated"),
}


def _networks(value: object, c: _Collector) -> dict[str, NetworkDef]:
    if not isinstance(value, dict):
        c.reject("networks", f"无法识别的写法：{value!r}")
        return {}
    out: dict[str, NetworkDef] = {}
    for name, spec in value.items():
        spec = spec or {}
        path = f"networks.{name}"
        for key, v in spec.items():
            if key == "internal":
                continue
            if key == "driver":
                if v != "bridge":
                    c.reject(f"{path}.driver", f"只接受 bridge：{v!r}")
            elif key == "ipam":
                if v not in ({}, None) and v != {"driver": "default"}:
                    c.reject(f"{path}.ipam", "指定子网、地址范围或网关：地址由平台分配（C7）")
            elif key == "driver_opts":
                for opt, val in (v or {}).items():
                    ok = _DRIVER_OPTS.get(str(opt))
                    if ok is None or not ok(str(val)):
                        c.reject(f"{path}.driver_opts.{opt}", f"白名单外的 driver_opts：{opt}={val}")
                    else:
                        c.equivalent(f"{path}.driver_opts.{opt}", "只影响宿主侧网桥或出站方式，出站另由 §6.4 决定")
            elif key == "external":
                c.reject(f"{path}.external", "训练中不存在跨 trial 的外部网络")
            else:
                c.reject(f"{path}.{key}", "首版未实现的网络字段")
        out[str(name)] = NetworkDef(internal=bool(spec.get("internal", False)))
    return out


def _top_volumes(value: object, c: _Collector) -> set[str]:
    if not isinstance(value, dict):
        c.reject("volumes", f"无法识别的写法：{value!r}")
        return set()
    for name, spec in value.items():
        for key in spec or {}:
            if key == "external":
                c.reject(f"volumes.{name}.external", "训练中不存在跨 trial 的外部卷（§7.1）")
            else:
                c.reject(f"volumes.{name}.{key}", "首版未实现的卷字段")
    return {str(n) for n in value}


def _check_references(services: Mapping[str, Service], networks: Mapping[str, NetworkDef], c: _Collector) -> None:
    for s in services.values():
        for net in s.networks:
            if net != DEFAULT_NETWORK and net not in networks:
                c.reject(f"services.{s.name}.networks.{net}", "引用了未声明的网络")
        for dep, d in s.depends_on.items():
            if dep not in services and d.required:
                c.reject(f"services.{s.name}.depends_on.{dep}", "依赖不存在的服务")


def _runtime_params(env_raw: object, unresolved: Mapping[str, str | None]) -> dict[str, tuple[str, str | None]]:
    """`environment` 中引用了未提供变量的条目：变量名 → (原表达式, 默认值)。"""
    if not unresolved or not isinstance(env_raw, dict):
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    for key, expr in env_raw.items():
        if not isinstance(expr, str):
            continue
        for name, default in unresolved.items():
            if f"${{{name}" in expr:
                out[str(key)] = (expr, default)
    return out
