"""OpenSandboxPlatform assembly: create request body, wholesale cidr replacement in link, and methods delegating
to the right routes.

See docs/backends/opensandbox.md. A path-routed `httpx.MockTransport` handler covers the
control plane (`/v1/sandboxes...`) and the execd proxy prefix (`/v1/sandboxes/{id}/proxy/44772/...`). execd
NDJSON streams and file semantics are covered in `test_opensandbox_execd.py`.
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
from flotilla.platform.opensandbox import ExecdSettings, OpenSandboxPlatform, StorageSettings, build
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import EXECD_TOKEN_HEADER, MANAGED_CREATE_FIELDS
from flotilla.testing.platform import ManualClock

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
    extensions: dict[str, object] | None = None,
    group_extension: str | None = None,
    settings: NetworkSettings = SETTINGS,
) -> OpenSandboxPlatform:
    return build(
        endpoint="http://os.test",
        credential="ak:secret",
        clock=clock,
        settings=settings,
        storage=storage,
        caps=caps,
        execd=execd,
        extensions={"project": "p1"} if extensions is None else extensions,
        create_fields=create_fields,
        group_extension=group_extension,
        transport=httpx.MockTransport(handler),
    )


def test_satisfies_platform_protocol(clock: ManualClock, caps: Capabilities) -> None:
    platform = _platform(lambda r: httpx.Response(200, json={}), clock, caps=caps)
    assert isinstance(platform, Platform)
    assert platform.caps is caps


async def _create_body(
    clock: ManualClock, caps: Capabilities, spec: InstanceSpec, **platform_kwargs: Any
) -> dict[str, Any]:
    """Assemble the backend from `platform_kwargs`, issue one create and return the request body."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    await clock.run(_platform(handler, clock, caps=caps, **platform_kwargs).create(spec))
    (body,) = bodies
    return body


def _with_create(handler: Handler, *ids: str) -> Handler:
    """`POST /sandboxes` returns `ids` in order; other requests go to `handler`."""
    pending = iter(ids)

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(200, json={"id": next(pending)})
        return handler(request)

    return wrapped


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
    assert body["metadata"]["flotilla/unit"] == "db"
    assert len(body["metadata"]["flotilla/create-id"]) == 16
    assert body["timeout"] == 10800
    # Both equal: upstream K8s gets Guaranteed QoS; deployments honoring only resourceRequests still get the
    # original value.
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
    # Every instance carries a networkPolicy (otherwise no egress sidecar); none + no peers at create
    # = deny-all.
    assert body["networkPolicy"] == {"defaultAction": "deny", "egress": []}
    assert body["extensions"] == {"project": "p1"}
    # env holds only the execd access token (workload environment is passed per exec).
    assert list(body["env"]) == ["EXECD_ACCESS_TOKEN"] and len(body["env"]["EXECD_ACCESS_TOKEN"]) >= 32
    assert "ports" not in body  # only added via the deployment's create_fields


@pytest.mark.asyncio
async def test_create_merges_create_fields(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, _spec("db"), create_fields={"ports": PORTS})
    assert body["ports"] == PORTS
    assert body["networkPolicy"] == {"defaultAction": "deny", "egress": []}  # managed fields unaffected


@pytest.mark.parametrize("field", sorted(MANAGED_CREATE_FIELDS))
def test_create_fields_cannot_override_managed_fields(clock: ManualClock, caps: Capabilities, field: str) -> None:
    # Otherwise deployment config could bypass guarantees such as initial isolation, TTL and keeping
    # credentials out of instances.
    with pytest.raises(ValueError, match=field):
        _platform(lambda r: httpx.Response(200), clock, caps=caps, create_fields={field: {}})


@pytest.mark.asyncio
async def test_create_clamps_timeout_floor(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, dataclasses.replace(_spec("db"), timeout_seconds=10))  # below the floor
    assert body["timeout"] == 60  # floor is 60


@pytest.mark.asyncio
async def test_create_any_external_emits_deny_policy(clock: ManualClock, caps: Capabilities) -> None:
    body = await _create_body(clock, caps, _spec("db", ExternalPolicy(mode="any")))
    policy = body["networkPolicy"]
    assert policy["defaultAction"] == "allow"
    targets = {r["target"] for r in policy["egress"]}
    assert "10.0.0.0/16" in targets and "169.254.169.254/32" in targets  # whole sandbox range + platform range


# ───────────────────────────── Delegated routes (execd via proxy prefix) ─────────────────────────────


@pytest.mark.asyncio
async def test_exec_routes_through_proxy_prefix(clock: ManualClock, caps: Capabilities) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, content=b'{"type":"init","text":"c"}\n\n{"type":"execution_complete"}\n\n')

    platform = _platform(_with_create(handler, "sb-1"), clock, caps=caps)
    handle = await clock.run(platform.create(_spec("db")))
    proc_spec = ProcessSpec(argv=("echo", "hi"), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    result = await clock.run(platform.exec(handle, proc_spec))
    assert result.exit_code == 0
    # One exec sends a single request (env travels in the request envs, no env file written first), to that
    # instance's execd proxy prefix.
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


# ───────────────────────────── link (wholesale replacement per config) ─────────────────────────────


@pytest.mark.asyncio
async def test_sandbox_link_uses_exact_topology_and_same_upstream_endpoint(
    clock: ManualClock, caps: Capabilities
) -> None:
    policies: dict[str, dict[str, Any]] = {}
    ids = iter(["sb-a", "sb-b", "sb-c"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            body = json.loads(request.content)
            assert all(not r["target"].startswith("sandbox:") for r in body["networkPolicy"]["egress"])
            assert body["extensions"] == {"project": "p1"}
            return httpx.Response(200, json={"id": next(ids)})
        assert request.method == "PUT" and request.url.path.endswith("/networkpolicy")
        policies[request.url.path.split("/")[3]] = json.loads(request.content)
        return httpx.Response(200)

    platform = _platform(handler, clock, caps=caps, settings=NetworkSettings(link="sandbox"))
    external = ExternalPolicy(mode="allowlist", hosts=("model.example.com",))
    members = {name: await clock.run(platform.create(_spec(name, external))) for name in ("a", "b", "c")}
    topology = Topology(networks={"front": frozenset({"a", "b"}), "back": frozenset({"b", "c"})})
    await clock.run(platform.link(members, topology))
    assert {iid: p["egress"] for iid, p in policies.items()} == {
        "sb-a": [{"action": "allow", "target": "sandbox:sb-b"}, {"action": "allow", "target": "model.example.com"}],
        "sb-b": [
            {"action": "allow", "target": "sandbox:sb-a"},
            {"action": "allow", "target": "sandbox:sb-c"},
            {"action": "allow", "target": "model.example.com"},
        ],
        "sb-c": [{"action": "allow", "target": "sandbox:sb-b"}, {"action": "allow", "target": "model.example.com"}],
    }
    await clock.run(platform.link(members, Topology(networks={})))
    assert all(
        p == {"defaultAction": "deny", "egress": [{"action": "allow", "target": "model.example.com"}]}
        for p in policies.values()
    )


@pytest.mark.asyncio
async def test_link_replaces_policy_with_peers(clock: ManualClock, caps: Capabilities) -> None:
    # Two members on the same network; link should PUT once each, take peer IPs from internal_address, and keep
    # each one's external policy.
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
    # Build the iid -> external policy mapping via the public create (a narrow form none, b open form any).
    ha = await clock.run(platform.create(_spec("a", ExternalPolicy(mode="none"))))
    hb = await clock.run(platform.create(_spec("b", ExternalPolicy(mode="any"))))
    members = {"a": ha, "b": hb}
    topo = Topology(networks={"backend": frozenset({"a", "b"})})
    await clock.run(platform.link(members, topo))

    assert set(policies) == {"sb-a", "sb-b"}
    # a is narrow-form deny, allowing only the peer /32 (wholesale replacement).
    assert policies["sb-a"]["defaultAction"] == "deny"
    assert policies["sb-a"]["egress"] == [{"action": "allow", "target": "10.0.1.11/32"}]
    # b is open form: link keeps external=any (defaultAction allow) and carves peer a out of the deny set.
    assert policies["sb-b"]["defaultAction"] == "allow"
    denied = {r["target"] for r in policies["sb-b"]["egress"]}
    assert all(r["action"] == "deny" for r in policies["sb-b"]["egress"])
    assert not any(ipaddress.ip_address("10.0.1.10") in ipaddress.ip_network(t) for t in denied)  # peer carved out


@pytest.mark.asyncio
async def test_foreign_instance_is_not_retryable(clock: ManualClock, caps: Capabilities) -> None:
    # Instance created by another process: its labels match no create recorded here, so there is no external
    # policy and no execd access token.
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"id": "sb-a", "status": {"state": "Running"}, "metadata": {"x": "y"}})
        return httpx.Response(200)

    platform = _platform(handler, clock, caps=caps)
    proc_spec = ProcessSpec(argv=("true",), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(platform.exec(InstanceHandle(iid="sb-a"), proc_spec))
    assert exc.value.category is ErrorCategory.INVALID and exc.value.stage == "run"
    assert exc.value.retryable is False
    with pytest.raises(FlotillaError) as exc:
        await clock.run(platform.internal_address(InstanceHandle(iid="sb-a")))
    assert exc.value.stage == "address"
    assert exc.value.category is ErrorCategory.INVALID and exc.value.retryable is False
    assert all(not s.endswith("/command") for s in sent)  # not a single exec request went out without a token


@pytest.mark.asyncio
async def test_link_recovers_external_of_reconciled_instance(clock: ManualClock, caps: Capabilities) -> None:
    # Create response lost (transient) and core obtains the iid via reconciliation: the labels from get match
    # the external policy and token recorded before create.
    policies: dict[str, dict[str, Any]] = {}
    tokens: list[str] = []
    created_token: list[str] = []
    created_metadata: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sandboxes" and request.method == "POST":
            created_token.append(json.loads(request.content)["env"]["EXECD_ACCESS_TOKEN"])
            created_metadata.update(json.loads(request.content)["metadata"])
            return httpx.Response(503)
        if path == "/v1/sandboxes/sb-lost" and request.method == "GET":
            return httpx.Response(
                200, json={"id": "sb-lost", "status": {"state": "Running"}, "metadata": created_metadata}
            )
        if path.endswith("/networkpolicy"):
            policies[path.split("/")[3]] = json.loads(request.content)
            return httpx.Response(200)
        if path.endswith("/command"):
            tokens.append(request.headers[EXECD_TOKEN_HEADER])
            argv = json.loads(request.content)["argv"]
            text = "default via 10.0.1.1 dev eth0" if "route" in argv else "2: eth0 inet 10.0.1.9/24 scope global eth0"
            event = json.dumps({"type": "stdout", "text": text})
            return httpx.Response(200, content=f'{event}\n\n{{"type":"execution_complete"}}\n\n'.encode())
        return httpx.Response(404)

    platform = _platform(handler, clock, caps=caps)
    with pytest.raises(FlotillaError):
        await clock.run(platform.create(_spec("a", ExternalPolicy(mode="any"))))
    await clock.run(platform.link({"a": InstanceHandle(iid="sb-lost")}, Topology(networks={})))
    assert policies["sb-lost"]["defaultAction"] == "allow"  # uses the any from create, not the default
    assert tokens and len(set(tokens)) == 1 and tokens[0] == created_token[0]  # exec uses the token made before create


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
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(204)

    platform = _platform(_with_create(handler, "sb-1"), clock, caps=caps)
    handle = await clock.run(platform.create(_spec("db")))
    assert await clock.run(platform.internal_address(handle)) == "10.0.1.9"
    assert await clock.run(platform.internal_address(handle)) == "10.0.1.9"
    assert commands == 2  # second read hits the cache (one route + one addr)
    await clock.run(platform.delete("sb-1"))
    with pytest.raises(FlotillaError):  # delete clears all records: address cache and token are gone
        await clock.run(platform.internal_address(handle))
    assert commands == 2


@pytest.mark.asyncio
async def test_aclose_closes_shared_client(clock: ManualClock, caps: Capabilities) -> None:
    platform = _platform(lambda r: httpx.Response(200, json={}), clock, caps=caps)
    await platform.aclose()
    with pytest.raises(RuntimeError):
        await platform.delete("sb-1")  # client already closed


# ───────────────────────────── execd settings flow through to exec and files ─────────────────────────────


@pytest.mark.asyncio
async def test_legacy_execd_settings_reach_command_and_upload(clock: ManualClock, caps: Capabilities) -> None:
    seen: list[tuple[str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.url.path.endswith("/command") else None
        seen.append((request.url.path, body))
        if body is not None:
            return httpx.Response(200, content=b'{"type":"execution_complete"}\n\n')
        return httpx.Response(200)

    platform = _platform(
        _with_create(handler, "sb-1"), clock, caps=caps, execd=ExecdSettings(protocol="legacy", wrapper="/opt/bb")
    )
    handle = await clock.run(platform.create(_spec("db")))
    proc_spec = ProcessSpec(argv=("mkdir", "-p", "/x"), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    await clock.run(platform.exec(handle, proc_spec))
    await clock.run(platform.write_file(handle, "/etc/hosts", b"x", mode=0o644, uid=0, gid=0))
    prefix = "/v1/sandboxes/sb-1/proxy/44772"
    assert [path for path, _ in seen] == [f"{prefix}/command", f"{prefix}/files/upload"]
    command = seen[0][1]
    assert command is not None and "argv" not in command and str(command["command"]).startswith("/opt/bb sh -c")


# ───────────────────────────── execd access token ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exec_auth", [True, False])
async def test_each_instance_gets_own_token_on_every_execd_request(
    clock: ManualClock, caps: Capabilities, exec_auth: bool
) -> None:
    from datetime import UTC, datetime

    created: dict[str, str] = {}
    seen: list[tuple[str, str, str | None]] = []
    ids = iter(["sb-a", "sb-b"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token = request.headers.get(EXECD_TOKEN_HEADER)
        if path == "/v1/sandboxes" and request.method == "POST":
            iid = next(ids)
            created[iid] = json.loads(request.content)["env"]["EXECD_ACCESS_TOKEN"]
            assert token is None
            return httpx.Response(200, json={"id": iid})
        seen.append((path.split("/")[3], path, token))
        if path.endswith("/command"):
            return httpx.Response(200, content=b'{"type":"execution_complete"}\n\n')
        if "/command/status/" in path:
            return httpx.Response(200, json={"running": False, "exit_code": 0})
        if path.endswith("/files/download"):
            return httpx.Response(200, content=b"x")
        return httpx.Response(200, json={})

    platform = _platform(handler, clock, caps=dataclasses.replace(caps, exec_auth=exec_auth))
    ha = await clock.run(platform.create(_spec("a")))
    hb = await clock.run(platform.create(_spec("b")))
    assert created["sb-a"] != created["sb-b"]
    proc_spec = ProcessSpec(argv=("true",), uid=0, gid=0, cwd="/", env={}, timeout_s=5.0)
    for handle in (ha, hb):
        await clock.run(platform.exec(handle, proc_spec))
        await clock.run(platform.write_file(handle, "/tmp/f", b"x", mode=0o644, uid=0, gid=0))
        await clock.run(platform.read_file(handle, "/tmp/f"))
    await clock.run(platform.renew("sb-a", datetime(2030, 1, 1, tzinfo=UTC)))
    execd = [(iid, token) for iid, rest, token in seen if "proxy/44772" in rest]
    assert {iid for iid, _ in execd} == {"sb-a", "sb-b"}
    assert all(token == created[iid] for iid, token in execd)
    control = [token for _, rest, token in seen if "proxy/44772" not in rest]
    assert control and all(token is None for token in control)  # control-plane requests carry no execd token


@pytest.mark.asyncio
async def test_missing_id_error_does_not_echo_token(clock: ManualClock, caps: Capabilities) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        echoed = json.loads(request.content)["env"]
        return httpx.Response(200, json={"env": echoed, "state": "pending"})  # platform echoes env back but omits id

    platform = _platform(handler, clock, caps=caps)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(platform.create(_spec("db")))
    assert "instance ID" in str(exc.value) and "EXECD_ACCESS_TOKEN" not in str(exc.value)


# ───────────────────────────── Placement groups ─────────────────────────────


@pytest.mark.asyncio
async def test_group_written_to_configured_extension(clock: ManualClock, caps: Capabilities) -> None:
    grouped = dataclasses.replace(_spec("db"), group="g-1")
    body = await _create_body(clock, caps, grouped, group_extension="sandboxGroup")
    assert body["extensions"] == {"project": "p1", "sandboxGroup": "g-1"}
    # Not written for ungrouped instances, or when the deployment has no co-scheduling configured.
    assert (
        "sandboxGroup"
        not in (await _create_body(clock, caps, _spec("db"), group_extension="sandboxGroup"))["extensions"]
    )
    assert (await _create_body(clock, caps, grouped))["extensions"] == {"project": "p1"}


def test_extensions_cannot_preset_group_key(clock: ManualClock, caps: Capabilities) -> None:
    with pytest.raises(ValueError, match="sandboxGroup"):
        _platform(
            lambda r: httpx.Response(200),
            clock,
            caps=caps,
            extensions={"project": "p1", "sandboxGroup": "fixed"},
            group_extension="sandboxGroup",
        )


@pytest.mark.asyncio
async def test_long_command_cannot_starve_lifecycle_or_status(caps: Capabilities) -> None:
    import asyncio
    import contextlib

    from flotilla.platform.base import ExecAttempt
    from flotilla.platform.clock import SystemClock

    entered = asyncio.Event()
    release = asyncio.Event()
    writers: set[asyncio.StreamWriter] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            first, *headers = header.decode().split("\r\n")
            _method, path, _ = first.split()
            length = next((int(h.split(":", 1)[1]) for h in headers if h.lower().startswith("content-length:")), 0)
            if length:
                await reader.readexactly(length)
            if path.endswith("/command"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
                writer.write(b'{"type":"init","text":"long-command"}\n\n')
                await writer.drain()
                entered.set()
                await release.wait()
                return
            if "/command/status/" in path:
                data: dict[str, object] = {"running": True, "exit_code": None}
            else:
                data = {"id": "pool-test", "status": {"state": "Running"}}
            body = json.dumps(data).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    platform = build(
        endpoint=f"http://127.0.0.1:{port}",
        credential="test",
        clock=SystemClock(),
        settings=SETTINGS,
        storage=HOST_STORAGE,
        caps=caps,
        max_connections=1,
        timeout=httpx.Timeout(1, pool=0.1),
    )
    command = None
    try:
        handle = await platform.create(_spec("main"))
        command = asyncio.create_task(
            platform.exec(handle, ProcessSpec(("hang",), 0, 0, "/", {}, 30), attempt=ExecAttempt())
        )
        async with asyncio.timeout(2):
            await entered.wait()
            assert (await platform.get(handle.iid)).iid == handle.iid
            assert (await platform.process_status(handle, "long-command")).running
    finally:
        if command is not None:
            command.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await command
        release.set()
        await platform.aclose()
        server.close()
        await server.wait_closed()
        async with asyncio.timeout(2):
            while writers:
                await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_reconciliation_keeps_tokens_of_repeated_creates_distinct(clock: ManualClock, caps: Capabilities) -> None:
    created: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            created.append(json.loads(request.content))
            return httpx.Response(503)
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": str(i), "metadata": body["metadata"], "status": {"state": "Running"}}
                        for i, body in reversed(list(enumerate(created)))
                    ]
                },
            )
        if request.url.path.endswith("/command"):
            iid = int(request.url.path.split("/")[3])
            assert request.headers[EXECD_TOKEN_HEADER] == created[iid]["env"]["EXECD_ACCESS_TOKEN"]
            return httpx.Response(200, content=b'{"type":"execution_complete"}\n')
        return httpx.Response(200)

    platform = _platform(handler, clock, caps=caps)
    for _ in range(2):
        with pytest.raises(FlotillaError):
            await platform.create(_spec("same-labels"))
    listed = await platform.list({"flotilla/unit": "same-labels"})
    for state in listed:
        assert (await platform.exec(InstanceHandle(state.iid), ProcessSpec(("true",), 0, 0, "/", {}, 1))).exit_code == 0
        await platform.delete(state.iid)
    assert not platform._pending
    # Failed creations that never appear can be forgotten only after the owning sweep completes.
    with pytest.raises(FlotillaError):
        await platform.create(_spec("not-visible"))
    platform.forget_creates({"flotilla/unit": "not-visible"})
    assert not platform._pending
    await platform.aclose()
