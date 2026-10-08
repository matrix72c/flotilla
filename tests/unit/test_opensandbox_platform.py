"""OpenSandboxPlatform 组装：create 请求体、link 的 cidr 整体替换、各方法委托到正确路由。

对应 backends/opensandbox.md §3、§4、§8。用一个按路径路由的 `httpx.MockTransport` handler 覆盖控制面
（`/v1/sandboxes...`）与 execd 代理前缀（`/v1/sandboxes/{id}/proxy/44772/...`）。execd 的 NDJSON 流与
文件语义见 `test_opensandbox_execd.py`。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from flotilla.platform.base import (
    Capabilities,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    ProcessSpec,
    Resources,
    SharedVolume,
    Topology,
)
from flotilla.platform.fake import ManualClock
from flotilla.platform.opensandbox import ExecdSettings, OpenSandboxPlatform, StorageSettings, build
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import MANAGED_CREATE_FIELDS

Handler = Callable[[httpx.Request], httpx.Response]

SETTINGS = NetworkSettings(sandbox_cidrs=("10.0.0.0/16",), platform_cidrs=("169.254.169.254/32",))
NONE = ExternalPolicy(mode="none")
HOST_STORAGE = StorageSettings(host_path="/mnt/shared")
PORTS = [{"containerPort": 44772, "name": "execd", "protocol": "tcp"}]


def _spec(unit: str, external: ExternalPolicy = NONE) -> InstanceSpec:
    return InstanceSpec(
        image="reg/app@sha256:abc",
        entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
        labels={"flotilla/unit": unit},
        timeout_seconds=10800,
        resources=Resources(cpu="1", memory="2Gi"),
        volumes=(SharedVolume(key="launches/L/t/data", mount_path="/srv", read_only=False),),
        external=external,
    )


def _platform(
    handler: Handler,
    clock: ManualClock,
    *,
    caps: Capabilities,
    storage: StorageSettings = HOST_STORAGE,
    execd: ExecdSettings | None = None,
    create_fields: dict[str, object] | None = None,
) -> OpenSandboxPlatform:
    return build(
        endpoint="http://os.test",
        credential="ak:secret",
        clock=clock,
        settings=SETTINGS,
        storage=storage,
        caps=caps,
        execd=execd,
        extensions={"project": "p1"},
        create_fields=create_fields,
        transport=httpx.MockTransport(handler),
    )


def test_satisfies_platform_protocol(clock: ManualClock, caps: Capabilities) -> None:
    platform = _platform(lambda r: httpx.Response(200, json={}), clock, caps=caps)
    assert isinstance(platform, Platform)
    assert platform.caps is caps


async def _create_body(
    clock: ManualClock, caps: Capabilities, spec: InstanceSpec, **platform_kwargs: Any
) -> dict[str, Any]:
    """用 `platform_kwargs` 组装后端、发一次 create，返回请求体。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    await clock.run(_platform(handler, clock, caps=caps, **platform_kwargs).create(spec))
    (body,) = bodies
    return body


# ───────────────────────────── create ─────────────────────────────


@pytest.mark.asyncio
async def test_create_builds_request_body(clock: ManualClock, caps: Capabilities) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "sb-1"})

    handle = await clock.run(_platform(handler, clock, caps=caps).create(_spec("db")))
    assert handle == InstanceHandle(iid="sb-1")
    (request,) = requests
    assert request.url.path == "/v1/sandboxes"
    assert request.headers.get("OPEN-SANDBOX-API-KEY") == "ak:secret"
    body: dict[str, Any] = json.loads(request.content)
    assert body["image"] == {"uri": "reg/app@sha256:abc"}
    assert body["entrypoint"] == ["/.flotilla/bin/busybox", "sleep", "2147483647"]
    assert body["metadata"] == {"flotilla/unit": "db"}
    assert body["timeout"] == 10800
    # 两者等值：上游 K8s 即 Guaranteed QoS；只认 resourceRequests 的部署也拿到原值（§3.4）。
    assert body["resourceLimits"] == body["resourceRequests"] == {"cpu": "1", "memory": "2Gi"}
    assert body["volumes"] == [
        {
            "name": "v0",
            "host": {"path": "/mnt/shared"},
            "mountPath": "/srv",
            "readOnly": False,
            "subPath": "launches/L/t/data",
        }
    ]
    # 每个实例都带 networkPolicy（否则无 egress sidecar，§3.3）；none + 创建时无对端 = deny-all。
    assert body["networkPolicy"] == {"defaultAction": "deny", "egress": []}
    assert body["extensions"] == {"project": "p1"}
    assert "env" not in body  # 不传 env（§3.3）
    assert "ports" not in body  # 只有部署配置的 create_fields 才会加


@pytest.mark.asyncio
async def test_create_merges_create_fields(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, _spec("db"), create_fields={"ports": PORTS})
    assert body["ports"] == PORTS
    assert body["networkPolicy"] == {"defaultAction": "deny", "egress": []}  # 管理字段不受影响


@pytest.mark.parametrize("field", sorted(MANAGED_CREATE_FIELDS))
def test_create_fields_cannot_override_managed_fields(clock: ManualClock, caps: Capabilities, field: str) -> None:
    # 否则部署配置能绕过初始隔离、TTL、凭证不下沉等保证（§3.3）。
    with pytest.raises(ValueError, match=field):
        _platform(lambda r: httpx.Response(200), clock, caps=caps, create_fields={field: {}})


@pytest.mark.asyncio
async def test_create_clamps_timeout_floor(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, dataclasses.replace(_spec("db"), timeout_seconds=10))  # 低于下限
    assert body["timeout"] == 60  # 下限 60（§3.4）


@pytest.mark.asyncio
async def test_create_any_external_emits_deny_policy(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, _spec("db", ExternalPolicy(mode="any")))
    policy = body["networkPolicy"]
    assert policy["defaultAction"] == "allow"
    targets = {r["target"] for r in policy["egress"]}
    assert "10.0.0.0/16" in targets and "169.254.169.254/32" in targets  # 整个 sandbox 网段 + 平台网段


# ───────────────────────────── 委托路由（execd 经代理前缀）─────────────────────────────


@pytest.mark.asyncio
async def test_exec_routes_through_proxy_prefix(clock: ManualClock, caps: Capabilities) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, content=b'{"type":"init","text":"c"}\n\n{"type":"execution_complete"}\n\n')

    proc_spec = ProcessSpec(argv=("echo", "hi"), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    result = await clock.run(_platform(handler, clock, caps=caps).exec(InstanceHandle(iid="sb-1"), proc_spec))
    assert result.exit_code == 0
    # 一次 exec 只发一个请求（env 随请求的 envs，不再先写 env 文件），打到该实例的 execd 代理前缀。
    assert paths == ["/v1/sandboxes/sb-1/proxy/44772/command"]


@pytest.mark.asyncio
async def test_renew_delete_route_to_control_plane(clock: ManualClock, caps: Capabilities) -> None:
    from datetime import UTC, datetime

    paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        return httpx.Response(204)

    platform = _platform(handler, clock, caps=caps)
    await clock.run(platform.renew("sb-1", datetime(2026, 2, 2, tzinfo=UTC)))
    await clock.run(platform.delete("sb-1"))
    assert ("POST", "/v1/sandboxes/sb-1/renew-expiration") in paths
    assert ("DELETE", "/v1/sandboxes/sb-1") in paths


# ───────────────────────────── link（cidr 整体替换）─────────────────────────────


@pytest.mark.asyncio
async def test_link_replaces_policy_with_peers(clock: ManualClock, caps: Capabilities) -> None:
    # 两个成员在同一网络；link 应各 PUT 一次，对端 IP 取自 internal_address，并保留各自的外部策略。
    addresses = {"sb-a": "10.0.1.10", "sb-b": "10.0.1.11"}
    ids = iter(["sb-a", "sb-b"])
    policies: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(200, json={"id": next(ids)})
        if path.endswith("/networkpolicy"):
            policies[path.split("/")[3]] = json.loads(request.content)
            return httpx.Response(200)
        if path.endswith("/command"):
            iid = path.split("/")[3]
            argv = json.loads(request.content)["argv"]
            out = (
                f"default via 10.0.1.1 dev eth0 src {addresses[iid]}\n"
                if "route" in argv
                else f"2: eth0    inet {addresses[iid]}/24 scope global eth0\n"
            )
            event = json.dumps({"type": "stdout", "text": out})
            return httpx.Response(200, content=f'{event}\n\n{{"type":"execution_complete"}}\n\n'.encode())
        return httpx.Response(200, json={})

    platform = _platform(handler, clock, caps=caps)
    # 经公开 create 建立 iid → 外部策略映射（a 窄形式 none、b 任意形式 any）。
    ha = await clock.run(platform.create(_spec("a", ExternalPolicy(mode="none"))))
    hb = await clock.run(platform.create(_spec("b", ExternalPolicy(mode="any"))))
    members = {"a": ha, "b": hb}
    topo = Topology(networks={"backend": frozenset({"a", "b"})})
    await clock.run(platform.link(members, topo))

    assert set(policies) == {"sb-a", "sb-b"}
    # a 是窄形式 deny，只放行对端 /32（整体替换）。
    assert policies["sb-a"]["defaultAction"] == "deny"
    assert policies["sb-a"]["egress"] == [{"action": "allow", "target": "10.0.1.11/32"}]
    # b 是任意形式：link 保留 external=any（defaultAction allow），并把对端 a 从 deny 集合里挖出。
    assert policies["sb-b"]["defaultAction"] == "allow"
    denied = {r["target"] for r in policies["sb-b"]["egress"]}
    assert all(r["action"] == "deny" for r in policies["sb-b"]["egress"])
    assert not any(ipaddress.ip_address("10.0.1.10") in ipaddress.ip_network(t) for t in denied)  # 对端被挖出


@pytest.mark.asyncio
async def test_link_unknown_external_is_transient(clock: ManualClock, caps: Capabilities) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/command"):
            out = json.dumps({"type": "stdout", "text": "default via 10.0.1.1 dev eth0\n"})
            addr = json.dumps({"type": "stdout", "text": "2: eth0 inet 10.0.1.9/24 scope global eth0\n"})
            argv = json.loads(request.content)["argv"]
            body = out if "route" in argv else addr
            return httpx.Response(200, content=f'{body}\n\n{{"type":"execution_complete"}}\n\n'.encode())
        if request.method == "GET":  # 别的进程建的实例：标签对不上本进程记下的任何创建
            return httpx.Response(200, json={"id": "sb-a", "status": {"state": "Running"}, "metadata": {"x": "y"}})
        return httpx.Response(200)

    platform = _platform(handler, clock, caps=caps)  # 未经 create，没有任何外部策略记录
    members = {"a": InstanceHandle(iid="sb-a"), "b": InstanceHandle(iid="sb-b")}
    topo = Topology(networks={"n": frozenset({"a", "b"})})
    with pytest.raises(FlotillaError) as exc:
        await clock.run(platform.link(members, topo))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.stage == "wire"


@pytest.mark.asyncio
async def test_link_recovers_external_of_reconciled_instance(clock: ManualClock, caps: Capabilities) -> None:
    # 创建响应丢失（transient）、core 经对账拿到 iid：link 凭 get 到的标签对上创建前记下的外部策略。
    policies: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(503)
        if path == "/v1/sandboxes/sb-lost" and request.method == "GET":
            return httpx.Response(
                200, json={"id": "sb-lost", "status": {"state": "Running"}, "metadata": {"flotilla/unit": "a"}}
            )
        if path.endswith("/networkpolicy"):
            policies[path.split("/")[3]] = json.loads(request.content)
            return httpx.Response(200)
        if path.endswith("/command"):
            argv = json.loads(request.content)["argv"]
            text = "default via 10.0.1.1 dev eth0" if "route" in argv else "2: eth0 inet 10.0.1.9/24 scope global eth0"
            event = json.dumps({"type": "stdout", "text": text})
            return httpx.Response(200, content=f'{event}\n\n{{"type":"execution_complete"}}\n\n'.encode())
        return httpx.Response(404)

    platform = _platform(handler, clock, caps=caps)
    with pytest.raises(FlotillaError):
        await clock.run(platform.create(_spec("a", ExternalPolicy(mode="any"))))
    await clock.run(platform.link({"a": InstanceHandle(iid="sb-lost")}, Topology(networks={})))
    assert policies["sb-lost"]["defaultAction"] == "allow"  # 用的是创建时的 any，而不是缺省


@pytest.mark.asyncio
async def test_internal_address_cached_and_forgotten_on_delete(clock: ManualClock, caps: Capabilities) -> None:
    commands = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal commands
        if request.url.path.endswith("/command"):
            commands += 1
            argv = json.loads(request.content)["argv"]
            text = "default via 10.0.1.1 dev eth0" if "route" in argv else "2: eth0 inet 10.0.1.9/24 scope global eth0"
            event = json.dumps({"type": "stdout", "text": text})
            return httpx.Response(200, content=f'{event}\n\n{{"type":"execution_complete"}}\n\n'.encode())
        return httpx.Response(204)

    platform = _platform(handler, clock, caps=caps)
    handle = InstanceHandle(iid="sb-1")
    assert await clock.run(platform.internal_address(handle)) == "10.0.1.9"
    assert await clock.run(platform.internal_address(handle)) == "10.0.1.9"
    assert commands == 2  # 第二次读缓存（route + addr 各一次）
    await clock.run(platform.delete("sb-1"))
    await clock.run(platform.internal_address(handle))
    assert commands == 4


@pytest.mark.asyncio
async def test_aclose_closes_shared_client(clock: ManualClock, caps: Capabilities) -> None:
    platform = _platform(lambda r: httpx.Response(200, json={}), clock, caps=caps)
    await platform.aclose()
    with pytest.raises(RuntimeError):
        await platform.delete("sb-1")  # client 已关闭


# ───────────────────────────── execd 设置贯穿到执行与文件 ─────────────────────────────


@pytest.mark.asyncio
async def test_legacy_execd_settings_reach_command_and_upload(clock: ManualClock, caps: Capabilities) -> None:
    seen: list[tuple[str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.url.path.endswith("/command") else None
        seen.append((request.url.path, body))
        if body is not None:
            return httpx.Response(200, content=b'{"type":"execution_complete"}\n\n')
        return httpx.Response(200)

    platform = _platform(handler, clock, caps=caps, execd=ExecdSettings(protocol="legacy", wrapper="/opt/bb"))
    handle = InstanceHandle(iid="sb-1")
    proc_spec = ProcessSpec(argv=("mkdir", "-p", "/x"), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    await clock.run(platform.exec(handle, proc_spec))
    await clock.run(platform.write_file(handle, "/etc/hosts", b"x", mode=0o644, uid=0, gid=0))
    prefix = "/v1/sandboxes/sb-1/proxy/44772"
    assert [path for path, _ in seen] == [f"{prefix}/command", f"{prefix}/files/upload"]
    command = seen[0][1]
    assert command is not None and "argv" not in command and str(command["command"]).startswith("/opt/bb sh -c")
