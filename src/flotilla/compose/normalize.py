"""Normalize supported Compose semantics and report unsupported fields."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Mapping
from typing import Any, cast

from flotilla.compose import resources as resource_values
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
    RuntimeExpression,
    Service,
    VolumeMount,
)
from flotilla.compose.paths import valid_name
from flotilla.compose.units import parse_duration
from flotilla.platform.base import TmpfsMount
from flotilla.platform.filesystem import memory_bytes, validate_mounts

DEFAULT_NETWORK = "default"
KEEPALIVE = ("sh", "-c", "sleep infinity")

# Service fields that are only recorded: they do not affect flotilla's behavior.
_EQUIVALENT = {
    "init": "the exec daemon reaps zombie processes",
    "labels": "recorded only",
    "logging": "recorded only",
    "stop_signal": "services stop only when the sandbox is deleted; recorded only",
    "stop_grace_period": "services stop only when the sandbox is deleted; recorded only",
    "expose": "services on the same network already reach all ports; nothing is published, recorded only",
    "container_name": "the name is written into other units' hosts",
    "pull_policy": "images are pinned by digest; recorded only",
}
# Rejected service fields (including platform capability gaps).
_REJECT = {
    "group_add": "requires supplementary_groups; not supported yet",
    "cap_drop": "requires per-command capability control; not supported yet",
    "security_opt": "requires per-command capability control; not supported yet",
    "ulimits": "requires per-command rlimit; not supported yet",
    "tty": "requires a pty; not supported yet",
    "stdin_open": "requires a pty; not supported yet",
    "pid": "pid sharing requires multi-container instances; not supported yet",
    "ipc": "ipc sharing requires multi-container instances; not supported yet",
    "userns_mode": "not meaningful in an isolated sandbox",
    "runtime": "choosing a container runtime is not supported",
    "volumes_from": "not implemented yet",
    "extends": "not implemented yet (not seen in task data)",
    "env_file": "not implemented yet (not seen in task data)",
    "profiles": "not implemented yet (not seen in task data)",
    "secrets": "not implemented yet (not seen in task data)",
    "configs": "not implemented yet (not seen in task data)",
    "devices": "devices not implemented yet (not seen in task data)",
    "sysctls": "not implemented yet (not seen in task data)",
    "cap_add": "not implemented yet (not seen in task data)",
    "links": "not implemented yet (not seen in task data)",
    "extra_hosts": "not implemented yet (not seen in task data)",
    "domainname": "not implemented yet (not seen in task data)",
    "scale": "replicas not implemented yet (not seen in task data)",
}
_HANDLED = {
    "image", "build", "command", "entrypoint", "environment", "working_dir", "user", "healthcheck", "depends_on",
    "networks", "network_mode", "volumes", "tmpfs", "shm_size", "restart", "privileged", "hostname", "ports",
    "cpus", "mem_limit", "deploy",
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
    """Layer Harbor's keepalive override beneath the task Compose: `main` gets `command` `sleep infinity` and the
    image from task.toml `docker_image` or the environment Dockerfile. The task file merges on top and may override
    the keepalive command; an explicit docker_image always wins over the main service's image/build.

    Merging matches `docker compose -f base -f task`: services merge by name, scalars and lists take the later
    value, mappings merge recursively.
    """
    base_main: dict[str, Any] = {"command": list(KEEPALIVE)}
    if main_image is not None:
        base_main["image"] = main_image
    elif main_build:
        base_main["build"] = {"context": "."}
    base = {"services": {MAIN: base_main}}
    merged = cast(dict[str, Any], _merge(base, dict(compose)))
    if (
        main_image is not None
        and isinstance(merged.get("services"), dict)
        and isinstance(merged["services"].get(MAIN), dict)
    ):
        merged["services"][MAIN]["image"] = main_image
        merged["services"][MAIN].pop("build", None)
    return merged


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base[k], v) if k in base else v
        return out
    return over


def normalize(compose: Mapping[str, Any], variables: Mapping[str, str]) -> Project:
    """Normalize a Compose document that already has the keepalive override applied."""
    c = _Collector()
    for key in compose:
        if key not in ("services", "networks", "volumes", "name", "version"):
            c.reject(key, "unsupported top-level field")
    raw_services = compose.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError("services: expected a nonempty mapping")

    network_defs = _networks({} if compose.get("networks") is None else compose["networks"], c)
    volumes = _top_volumes({} if compose.get("volumes") is None else compose["volumes"], c)

    services: dict[str, Service] = {}
    runtime_params: dict[str, dict[str, RuntimeExpression]] = {}
    for name, raw in raw_services.items():
        raw = {} if raw is None else raw
        path = f"services.{name}"
        if not valid_name(name) or not isinstance(raw, dict):
            c.reject(path, "service name must be a safe identifier and its definition a mapping")
            continue
        env_raw = _environment(raw.get("environment"), f"{path}.environment", c)
        rest = {k: v for k, v in raw.items() if k != "environment"}
        try:
            rest_i = interpolate(rest, variables)
            env_i = interpolate(env_raw, variables)
        except InterpolationError as exc:
            c.reject(path, str(exc))
            continue
        if rest_i.unresolved:
            # Only proxy variables in build.args (HTTP_PROXY etc.) may be unset: they default to "" at build time.
            hard = {k: d for k, d in rest_i.unresolved.items() if d is None}
            if hard:
                c.reject(
                    path, f"references variables the task does not provide and that have no default: {sorted(hard)}"
                )
        params = _runtime_params(env_raw, variables)
        if params:
            runtime_params[name] = params
        services[name] = _service(
            name, cast(dict[str, Any], rest_i.value), cast(dict[str, str], env_i.value), volumes, c
        )

    if MAIN not in services and not any(f.path.startswith(f"services.{MAIN}") for f in c.findings):
        c.reject("services", f"missing agent service {MAIN}")
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


def _service(
    name: str,
    raw: dict[str, Any],
    env: dict[str, str],
    volumes: set[str],
    c: _Collector,
) -> Service:
    path = f"services.{name}"
    for key in raw:
        if key in _HANDLED:
            continue
        if key == "read_only":
            continue
        if key in _EQUIVALENT:
            c.equivalent(f"{path}.{key}", _EQUIVALENT[key])
        elif key in _REJECT:
            c.reject(f"{path}.{key}", _REJECT[key])
        else:
            c.reject(f"{path}.{key}", "unsupported field")

    if "image" not in raw and "build" not in raw:
        c.reject(path, "neither image nor build")
    hc, hc_disabled = _healthcheck(raw.get("healthcheck"), f"{path}.healthcheck", c)
    mounts = _volumes(raw.get("volumes"), f"{path}.volumes", volumes, c)
    tmpfs = (*_tmpfs(raw.get("tmpfs"), f"{path}.tmpfs", c), *_long_tmpfs(raw.get("volumes"), f"{path}.volumes", c))
    try:
        validate_mounts(tmpfs, (m.target for m in mounts))
    except ValueError as exc:
        c.reject(f"{path}.tmpfs", str(exc))
    return Service(
        name=name,
        resources=_resources(raw, path, c),
        image=_opt_str(raw.get("image"), f"{path}.image", c),
        build=_build(raw.get("build"), f"{path}.build", c),
        command=_argv(raw.get("command"), f"{path}.command", c),
        entrypoint=_argv(raw.get("entrypoint"), f"{path}.entrypoint", c),
        environment=env,
        working_dir=_opt_str(raw.get("working_dir"), f"{path}.working_dir", c),
        user=_opt_str(raw.get("user"), f"{path}.user", c),
        healthcheck=hc,
        healthcheck_disabled=hc_disabled,
        depends_on=_depends_on(raw.get("depends_on"), f"{path}.depends_on", c),
        networks=_service_networks(raw, path, c),
        volumes=mounts,
        tmpfs=tmpfs,
        shm_size_bytes=_shm_size(raw.get("shm_size"), f"{path}.shm_size", c),
        readonly_rootfs=_boolean(raw.get("read_only", False), f"{path}.read_only", c),
        restart=_restart(raw.get("restart"), f"{path}.restart", c),
        privileged=_boolean(raw.get("privileged", False), f"{path}.privileged", c),
        container_name=_hostname(raw.get("container_name"), f"{path}.container_name", c),
        hostname=_hostname(raw.get("hostname"), f"{path}.hostname", c),
        udp_ports=_ports(raw.get("ports"), f"{path}.ports", c),
    )


def _boolean(value: object, path: str, c: _Collector) -> bool:
    if isinstance(value, bool):
        return value
    c.reject(path, "must be a boolean true or false")
    return False


def _mapping(value: object, path: str, c: _Collector) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    c.reject(path, "must be a mapping")
    return {}


def _opt_str(value: object, path: str, c: _Collector) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int) and not isinstance(value, bool):
        return str(value)
    c.reject(path, f"expected a string: {value!r}")
    return None


def _argv(value: object, path: str, c: _Collector) -> tuple[str, ...] | None:
    """Compose shell words; write sh -c explicitly when a shell is needed."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError as exc:
            c.reject(path, f"unbalanced quotes in command: {exc}")
            return None
    if isinstance(value, list) and all(isinstance(x, str | int | float) for x in value):
        return tuple(str(x) for x in value)
    c.reject(path, f"unrecognized syntax: {value!r}")
    return None


def _build(value: object, path: str, c: _Collector) -> Build | None:
    if value is None:
        return None
    if isinstance(value, str):
        return Build(context=value)
    if not isinstance(value, dict):
        c.reject(path, f"unrecognized syntax: {value!r}")
        return None
    for key in value:
        if key not in ("context", "dockerfile", "args"):
            c.reject(f"{path}.{key}", "unsupported build field")
    args = {} if value.get("args") is None else value["args"]
    if isinstance(args, list):
        args = dict(_split_kv(x) for x in args)
    args = _mapping(args, f"{path}.args", c)
    return Build(
        context=_opt_str(value.get("context", "."), f"{path}.context", c) or ".",
        dockerfile=_opt_str(value.get("dockerfile"), f"{path}.dockerfile", c),
        args={str(k): "" if v is None else str(v) for k, v in args.items()},
    )


def _environment(value: object, path: str, c: _Collector) -> dict[str, str]:
    """Mapping or `K=V` list; key-only (null) entries take the host env in Compose; there is none here, so reject."""
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
        c.reject(path, f"unrecognized syntax: {value!r}")
        return {}
    out: dict[str, str] = {}
    for k, v in items:
        if v is None:
            c.reject(f"{path}.{k}", "key-only variable inherits the host env; the build host env never enters tasks")
            continue
        if not isinstance(v, str | int | float | bool):
            c.reject(f"{path}.{k}", "environment values must be scalars")
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
        c.reject(path, f"unrecognized syntax: {value!r}")
        return None, False
    if _boolean(value.get("disable", False), f"{path}.disable", c):
        return None, True
    for key in value:
        if key not in ("test", "interval", "timeout", "retries", "start_period", "start_interval", "disable"):
            c.reject(f"{path}.{key}", "unsupported healthcheck field")
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
        c.reject(path, "overriding timings while inheriting the image HEALTHCHECK test is not implemented yet")
        return None, False
    else:
        c.reject(f"{path}.test", f"unrecognized syntax: {test!r}")
        return None, False
    try:
        retries = value.get("retries", 3)
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise ValueError("healthcheck.retries must be an integer")
        hc = Healthcheck(
            test=argv,
            interval_s=parse_duration(value.get("interval", "30s"), path=f"{path}.interval"),
            timeout_s=parse_duration(value.get("timeout", "30s"), path=f"{path}.timeout"),
            retries=retries,
            start_period_s=parse_duration(value.get("start_period", "0s"), path=f"{path}.start_period"),
            start_interval_s=parse_duration(value.get("start_interval", "5s"), path=f"{path}.start_interval"),
        )
    except ValueError as exc:
        c.reject(path, str(exc))
        return None, False
    if hc.interval_s <= 0 or hc.timeout_s <= 0 or hc.retries < 1:
        c.reject(path, "healthcheck interval and timeout must be > 0 and retries >= 1")
        return None, False
    return hc, False


_CONDITIONS: tuple[Condition, ...] = ("service_started", "service_healthy", "service_completed_successfully")


def _depends_on(value: object, path: str, c: _Collector) -> dict[str, Dependency]:
    if value is None:
        return {}
    if isinstance(value, list):
        return {str(n): Dependency() for n in value}
    if not isinstance(value, dict):
        c.reject(path, f"unrecognized syntax: {value!r}")
        return {}
    out: dict[str, Dependency] = {}
    for dep, spec in value.items():
        spec = _mapping(spec, f"{path}.{dep}", c)
        cond = spec.get("condition", "service_started")
        if cond not in _CONDITIONS:
            c.reject(f"{path}.{dep}.condition", f"unknown condition: {cond!r}")
            continue
        for key in spec:
            if key == "restart":
                c.equivalent(
                    f"{path}.{dep}.restart", "applies only to explicit restarts; flotilla has no restart entry point"
                )
            elif key not in ("condition", "required"):
                c.reject(f"{path}.{dep}.{key}", "unsupported depends_on field")
        out[str(dep)] = Dependency(
            condition=cast(Condition, cond), required=_boolean(spec.get("required", True), f"{path}.{dep}.required", c)
        )
    return out


def _service_networks(raw: Mapping[str, Any], path: str, c: _Collector) -> dict[str, NetworkAttachment]:
    mode = raw.get("network_mode")
    nets = raw.get("networks")
    if mode is not None:
        if nets:
            c.reject(path, "network_mode and networks cannot both be set")
        if mode == "none":
            return {}
        reason = (
            "requires multi-container instances; not supported yet"
            if str(mode).startswith(("service:", "container:"))
            else "not meaningful in an isolated sandbox"
        )
        c.reject(f"{path}.network_mode", reason)
        return {}
    if nets is None:
        return {DEFAULT_NETWORK: NetworkAttachment()}
    if isinstance(nets, list):
        return {str(n): NetworkAttachment() for n in nets}
    if not isinstance(nets, dict):
        c.reject(f"{path}.networks", f"unrecognized syntax: {nets!r}")
        return {}
    out: dict[str, NetworkAttachment] = {}
    for net, spec in nets.items():
        spec = _mapping(spec, f"{path}.networks.{net}", c)
        for key in spec:
            if key in ("gw_priority", "priority"):
                c.equivalent(
                    f"{path}.networks.{net}.{key}",
                    "affects only the default route with multiple NICs; sandboxes have one",
                )
            elif key in ("ipv4_address", "ipv6_address"):
                c.reject(f"{path}.networks.{net}.{key}", "addresses are platform-assigned; fixed-IP access cannot hold")
            elif key != "aliases":
                c.reject(f"{path}.networks.{net}.{key}", "unsupported network field")
        aliases = [] if spec.get("aliases") is None else spec["aliases"]
        if not isinstance(aliases, list) or any(not valid_name(alias) for alias in aliases):
            c.reject(f"{path}.networks.{net}.aliases", "aliases must be a list of valid hostnames")
            aliases = []
        out[str(net)] = NetworkAttachment(aliases=tuple(aliases))
    return out


def _volumes(value: object, path: str, named: set[str], c: _Collector) -> tuple[VolumeMount, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        c.reject(path, f"unrecognized syntax: {value!r}")
        return ()
    out: list[VolumeMount] = []
    for i, v in enumerate(value):
        p = f"{path}[{i}]"
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) in (2, 3) and (len(parts) == 2 or parts[2] in ("ro", "rw")):
                short_source, short_target = parts[:2]
                v = {
                    "type": "bind" if short_source.startswith((".", "/", "~")) else "volume",
                    "source": short_source,
                    "target": short_target,
                    "read_only": len(parts) == 3 and parts[2] == "ro",
                }
        if not isinstance(v, dict):
            c.reject(
                p,
                "volumes must use long syntax or source:target[:ro|rw]; anonymous volumes and options unsupported",
            )
            continue
        kind = v.get("type")
        target = v.get("target")
        if not isinstance(target, str) or not target.startswith("/"):
            c.reject(p, f"target must be an absolute path: {target!r}")
            continue
        target = posixpath.normpath(target)
        if target == "/":
            c.reject(p, "mounting at the root directory is not supported")
            continue
        if kind == "tmpfs":
            continue
        if kind not in ("volume", "bind"):
            c.reject(p, f"unsupported volume type: {kind!r}")
            continue
        source = v.get("source")
        if not isinstance(source, str) or not source:
            c.reject(p, "anonymous volumes are not implemented yet")
            continue
        for key in v:
            if key not in ("type", "source", "target", "read_only", "volume", "bind"):
                c.reject(f"{p}.{key}", "unsupported volume field")
        volume_opts = _mapping(v.get("volume"), f"{p}.volume", c)
        for key in volume_opts:
            if key != "nocopy":
                c.reject(f"{p}.volume.{key}", "unsupported volume option")
        nocopy = _boolean(volume_opts.get("nocopy", False), f"{p}.volume.nocopy", c)
        bind_opts = _mapping(v.get("bind"), f"{p}.bind", c)
        for key in bind_opts:
            if key not in ("create_host_path",):
                c.reject(f"{p}.bind.{key}", "unsupported bind field")
        if kind == "volume" and source not in named:
            c.reject(p, f"references undeclared named volume: {source}")
            continue
        if kind == "bind" and (source.startswith("/") or ".." in source.split("/")):
            c.reject(p, f"bind source is outside the task directory: {source}")
            continue
        _boolean(bind_opts.get("create_host_path", True), f"{p}.bind.create_host_path", c)
        out.append(
            VolumeMount(
                kind=kind,
                source=source,
                target=target,
                read_only=_boolean(v.get("read_only", False), f"{p}.read_only", c),
                nocopy=nocopy,
            )
        )
    return tuple(out)


def _tmpfs(value: object, path: str, c: _Collector) -> tuple[TmpfsMount, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        c.reject(path, f"unrecognized syntax: {value!r}")
        return ()
    out: list[TmpfsMount] = []
    for i, item in enumerate(items):
        p = f"{path}[{i}]"
        if not isinstance(item, str):
            c.reject(p, "tmpfs must be an absolute directory with optional size, mode, uid, gid options")
            continue
        target, separator, text = item.partition(":")
        options: dict[str, object] = {}
        if separator:
            for option in text.split(","):
                key, equals, val = option.partition("=")
                if not equals or key not in ("size", "mode", "uid", "gid") or key in options:
                    c.reject(p, f"unsupported or duplicate tmpfs option: {key}")
                else:
                    options[key] = val
        mount = _tmpfs_mount(target, options, p, c)
        if mount is not None:
            out.append(mount)
    return tuple(out)


def _long_tmpfs(value: object, path: str, c: _Collector) -> tuple[TmpfsMount, ...]:
    if not isinstance(value, list):
        return ()
    out: list[TmpfsMount] = []
    for i, v in enumerate(value):
        if not isinstance(v, dict) or v.get("type") != "tmpfs":
            continue
        p = f"{path}[{i}]"
        for key in v:
            if key not in ("type", "target", "tmpfs", "read_only"):
                c.reject(f"{p}.{key}", "unsupported tmpfs mount field")
        if _boolean(v.get("read_only", False), f"{p}.read_only", c):
            c.reject(f"{p}.read_only", "read-only tmpfs is not supported by the lifecycle API")
        opts = _mapping(v.get("tmpfs"), f"{p}.tmpfs", c)
        for key in opts:
            if key not in ("size", "mode"):
                c.reject(f"{p}.tmpfs.{key}", "unsupported long-syntax tmpfs option; use short syntax for uid/gid")
        mount = _tmpfs_mount(v.get("target"), opts, p, c)
        if mount is not None:
            out.append(mount)
    return tuple(out)


def _tmpfs_mount(target: object, options: Mapping[str, object], path: str, c: _Collector) -> TmpfsMount | None:
    try:
        if not isinstance(target, str):
            raise ValueError("tmpfs target must be a canonical absolute directory")
        size = memory_bytes(resource_values.memory(options["size"])) if "size" in options else None
        mode = options.get("mode", "1777")
        if isinstance(mode, str) and re.fullmatch(r"[0-7]{1,4}", mode):
            mode = int(mode, 8)
        if type(mode) is not int:
            raise ValueError("tmpfs mode must be an octal string or YAML octal integer")
        ids = []
        for name in ("uid", "gid"):
            value = options.get(name, 0)
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                value = int(value)
            if type(value) is not int:
                raise ValueError(f"tmpfs {name} must be a nonnegative integer")
            ids.append(value)
        return TmpfsMount(target, size, mode, ids[0], ids[1])
    except ValueError as exc:
        c.reject(path, str(exc))
        return None


def _shm_size(value: object, path: str, c: _Collector) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError("shm_size must be a byte count or K/M/G/T quantity")
        return memory_bytes(resource_values.memory(value))
    except ValueError as exc:
        c.reject(path, str(exc))
        return None


_RESTART: dict[str, RestartPolicy] = {"no": "no", "always": "always", "unless-stopped": "unless-stopped"}


def _restart(value: object, path: str, c: _Collector) -> RestartPolicy:
    if value is None or value is False:
        return "no"
    text = str(value)
    if text in _RESTART:
        if text != "no":
            c.equivalent(
                path, "no restarts while running; a sample is void if the service exits and Docker would restart it"
            )
        return _RESTART[text]
    c.reject(path, f"unknown restart policy: {text!r}")
    return "no"


def _hostname(value: object, path: str, c: _Collector) -> str | None:
    if value is None:
        return None
    if not valid_name(value):
        c.reject(path, "must be a valid hostname")
        return None
    c.warn(
        path,
        "name resolution works via other units' hosts; the own hostname is unchanged (gethostname() is the platform's)",
    )
    return str(value)


def _ports(value: object, path: str, c: _Collector) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        c.reject(path, f"unrecognized syntax: {value!r}")
        return ()
    c.equivalent(path, "services on the same network already reach all ports; no ports are published, recorded only")
    udp: list[int] = []
    for index, port in enumerate(value):
        field = f"{path}[{index}]"
        try:
            if isinstance(port, dict):
                protocol = port.get("protocol", "tcp")
                target = port.get("target")
                field += ".target"
            elif isinstance(port, (str, int)) and not isinstance(port, bool):
                raw, _, protocol = str(port).partition("/")
                protocol = protocol or "tcp"
                target = raw.rsplit(":", 1)[-1]
            else:
                raise ValueError("expected a port number, string or mapping")
            if protocol not in ("tcp", "udp"):
                raise ValueError("only tcp and udp are supported")
            if isinstance(target, bool) or not str(target).isdecimal():
                raise ValueError("target must be one numeric port; ranges are unsupported")
            number = int(str(target))
            if not 1 <= number <= 65535:
                raise ValueError("port must be between 1 and 65535")
            if protocol == "udp":
                udp.append(number)
        except ValueError as exc:
            c.reject(field, str(exc))
    return tuple(udp)


# driver_opts allowlist: equivalent keys and values; None means any value is equivalent.
_DRIVER_OPTS: dict[str, Callable[[str], bool]] = {
    "com.docker.network.bridge.name": lambda v: True,
    "com.docker.network.driver.mtu": lambda v: True,
    "com.docker.network.bridge.gateway_mode_ipv4": lambda v: v in ("nat", "routed", "isolated"),
    "com.docker.network.bridge.gateway_mode_ipv6": lambda v: v in ("nat", "routed", "isolated"),
}


def _networks(value: object, c: _Collector) -> dict[str, NetworkDef]:
    if not isinstance(value, dict):
        c.reject("networks", f"unrecognized syntax: {value!r}")
        return {}
    out: dict[str, NetworkDef] = {}
    for name, spec in value.items():
        path = f"networks.{name}"
        spec = _mapping(spec, path, c)
        for key, v in spec.items():
            if key == "internal":
                continue
            if key == "driver":
                if v != "bridge":
                    c.reject(f"{path}.driver", f"only bridge is accepted: {v!r}")
            elif key == "ipam":
                if v not in ({}, None) and v != {"driver": "default"}:
                    c.reject(f"{path}.ipam", "subnets, ranges, or gateways specified: addresses are platform-assigned")
            elif key == "driver_opts":
                for opt, val in _mapping(v, f"{path}.driver_opts", c).items():
                    ok = _DRIVER_OPTS.get(str(opt))
                    if ok is None or not ok(str(val)):
                        c.reject(f"{path}.driver_opts.{opt}", f"driver_opts not in allowlist: {opt}={val}")
                    else:
                        c.equivalent(
                            f"{path}.driver_opts.{opt}",
                            "affects only the host bridge or egress mode; egress is governed by the egress policy",
                        )
            elif key == "external":
                c.reject(f"{path}.external", "external networks shared across trials do not exist in training")
            else:
                c.reject(f"{path}.{key}", "unsupported network field")
        out[str(name)] = NetworkDef(internal=_boolean(spec.get("internal", False), f"{path}.internal", c))
    return out


def _top_volumes(value: object, c: _Collector) -> set[str]:
    if not isinstance(value, dict):
        c.reject("volumes", f"unrecognized syntax: {value!r}")
        return set()
    for name, spec in value.items():
        if not valid_name(name):
            c.reject(
                f"volumes.{name}",
                "volume names must start with a letter or digit and contain only letters, digits, _, ., and -",
            )
        for key in _mapping(spec, f"volumes.{name}", c):
            if key == "external":
                c.reject(f"volumes.{name}.external", "external volumes shared across trials do not exist in training")
            else:
                c.reject(f"volumes.{name}.{key}", "unsupported volume field")
    return {str(n) for n in value}


def _check_references(services: Mapping[str, Service], networks: Mapping[str, NetworkDef], c: _Collector) -> None:
    for s in services.values():
        for net in s.networks:
            if net != DEFAULT_NETWORK and net not in networks:
                c.reject(f"services.{s.name}.networks.{net}", "references an undeclared network")
        for dep, d in s.depends_on.items():
            if dep not in services and d.required:
                c.reject(f"services.{s.name}.depends_on.{dep}", "depends on a nonexistent service")


def _runtime_params(env_raw: Mapping[str, str], variables: Mapping[str, str]) -> dict[str, RuntimeExpression]:
    """Keep fixed values separate from syntax so literal dollars and braces are never reinterpreted."""
    out: dict[str, RuntimeExpression] = {}
    for key, expr in env_raw.items():
        result = interpolate(expr, variables)
        if result.unresolved:
            out[key] = RuntimeExpression(
                expr, {name: variables[name] for name in sorted(result.references) if name in variables}
            )
    return out


def _resources(raw: Mapping[str, Any], path: str, c: _Collector) -> dict[str, str]:
    deploy = _mapping(raw.get("deploy"), f"{path}.deploy", c)
    for key in deploy:
        if key != "resources":
            c.reject(f"{path}.deploy.{key}", "unsupported deploy field")
    resources = _mapping(deploy.get("resources"), f"{path}.deploy.resources", c)
    for key in resources:
        if key != "limits":
            c.reject(f"{path}.deploy.resources.{key}", "only limits are supported; platform requests equal limits")
    limits = _mapping(resources.get("limits"), f"{path}.deploy.resources.limits", c)
    for key in limits:
        if key not in ("cpus", "memory"):
            c.reject(f"{path}.deploy.resources.limits.{key}", "unsupported resource limit")
    out = {}
    for field, alias, dest, parse in (
        ("cpus", "cpus", "cpu", resource_values.cpu),
        ("mem_limit", "memory", "memory", resource_values.memory),
    ):
        try:
            direct = parse(raw[field]) if field in raw else None
            nested = parse(limits[alias]) if alias in limits else None
            if direct is not None and nested is not None and direct != nested:
                raise ValueError("service and deploy.resources.limits declare conflicting resources")
            if (value := direct or nested) is not None:
                out[dest] = value
        except ValueError as exc:
            c.reject(f"{path}.{field}", str(exc))
    return out
