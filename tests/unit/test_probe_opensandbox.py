"""Backend-specific controls use real request shapes without granting normal capabilities."""

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import (
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    ProcessSpec,
    Resources,
)
from flotilla.platform.opensandbox import StorageSettings, build
from flotilla.platform.opensandbox.filesystem import FilesystemSettings
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform
from flotilla.probe import ProbeSettings, _Run
from flotilla.probe_checks import prerequisites
from flotilla.probe_opensandbox import authentication, policy
from flotilla.testing.config import config
from flotilla.testing.platform import ManualClock


def spec(**changes: Any) -> InstanceSpec:
    base = InstanceSpec(
        image="reg/probe@sha256:" + "1" * 64,
        entrypoint=("sleep", "600"),
        labels={"flotilla/probe": "p1"},
        timeout_seconds=600,
        resources=Resources("1", "512Mi"),
        volumes=(),
        external=ExternalPolicy("none"),
    )
    return replace(base, **changes)


def backend(handler: Callable[[httpx.Request], httpx.Response], clock: ManualClock) -> OpenSandboxPlatform:
    return build(
        endpoint="http://os.test",
        credential="gateway-secret",
        clock=clock,
        settings=NetworkSettings(link="sandbox", ingress="sandbox"),
        storage=StorageSettings(host_path="/shared"),
        caps=config().management_capabilities(),
        filesystem=FilesystemSettings("sandbox-system-v1"),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_probe_pending_filesystem_does_not_enable_normal_create(clock: ManualClock) -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "unit"})

    platform = backend(handler, clock)
    try:
        requested = spec(readonly_rootfs=True)
        await platform.probe_create(requested)
        assert bodies[0]["readOnlyRootFilesystem"] is True
        assert not platform.caps.readonly_rootfs
        with pytest.raises(FlotillaError, match="readonly_rootfs"):
            await platform.create(requested)
        with pytest.raises(ValueError, match="probe run label"):
            await platform.probe_create(replace(requested, labels={}))
        with pytest.raises(ValueError, match="combination"):
            await platform.probe_create(replace(requested, privileged=True))
        assert len(bodies) == 1
    finally:
        await platform.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_token_status,leak,expected",
    [(403, False, "passed"), (200, False, "failed"), (500, False, "error"), (403, True, "failed")],
)
async def test_auth_controls_and_create_disclosure(
    clock: ManualClock, bad_token_status: int, leak: bool, expected: str
) -> None:
    token = ""
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token
        path = request.url.path
        if path == "/v1/sandboxes" and request.method == "POST":
            token = json.loads(request.content)["env"]["EXECD_ACCESS_TOKEN"]
            return httpx.Response(
                200, json={"id": "unit", "env": {"EXECD_ACCESS_TOKEN": token}} if leak else {"id": "unit"}
            )
        if "/proxy/" in path:
            if path.endswith("/download"):
                assert request.url.params["path"] == "/tmp/flotilla-auth-probe"
                assert ("upload", token) in paths
            if path.endswith("/upload"):
                assert b'"path": "/tmp/flotilla-auth-probe"' in request.content
            paths.append((path.rsplit("/", 1)[-1], request.headers.get("X-EXECD-ACCESS-TOKEN")))
            return httpx.Response(
                200 if request.headers.get("X-EXECD-ACCESS-TOKEN") == token else bad_token_status, content=b"{}"
            )
        return httpx.Response(200, json={"id": "unit"} if path.endswith("/unit") else {"items": [{"id": "unit"}]})

    platform = backend(handler, clock)
    try:
        unit = await platform.probe_create(spec())
        result = await authentication(platform, unit, run_id="p1")
        assert result.status == expected
        assert token not in result.model_dump_json() and "gateway-secret" not in result.model_dump_json()
        if expected == "passed":
            assert len(paths) == 9
            assert {path for path, _ in paths} == {"command", "download", "upload"}
            assert sum(value is None for _, value in paths) == 3
    finally:
        await platform.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "missing", "wrong-content", "timeout"])
async def test_prerequisites_require_configured_wrapper_base64(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    platform = backend(lambda _: httpx.Response(500), clock)
    platform._execd = replace(platform._execd, wrapper="/configured/busybox")
    settings = ProbeSettings(
        image="reg/probe@sha256:" + "1" * 64,
        share_release="r1",
        endpoint="http://os.test",
        evidence_profile="test",
    )
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    run = _Run(platform, clock, anchor, settings, "p1")
    commands = []

    async def execute(_unit: InstanceHandle, proc: ProcessSpec) -> ExecResult:
        commands.append(proc.argv)
        if proc.argv[0] != "/configured/busybox":
            return ExecResult(0, b"", b"")
        assert proc.argv[-1] == "printf probe | /configured/busybox base64"
        return ExecResult(
            127 if failure == "missing" else 0,
            b"bad" if failure == "wrong-content" else b"cHJvYmU=\n",
            b"",
            timed_out=failure == "timeout",
        )

    monkeypatch.setattr(platform, "exec", execute)
    try:
        measured = await prerequisites(run, InstanceHandle("unit"))
        assert measured.ok is (failure is None)
        assert len(commands) == 2
    finally:
        await platform.aclose()


@pytest.mark.asyncio
async def test_ingress_can_change_without_changing_source_egress(clock: ManualClock) -> None:
    bodies = []
    ids = iter(("source", "target"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": next(ids)})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    platform = backend(handler, clock)
    try:
        source = await platform.probe_create(spec())
        target = await platform.probe_create(spec())
        await policy(platform, target, egress=(source,), ingress=(source,))
        await policy(platform, target, egress=(source,), ingress=())
        assert bodies[0]["egress"] == bodies[1]["egress"]
        assert bodies[0]["ingress"] != bodies[1]["ingress"]
        assert bodies[1]["ingress"]["rules"][0]["from"] == "sandbox:any"
    finally:
        await platform.aclose()
