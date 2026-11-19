"""Backend-specific controls use real request shapes without granting normal capabilities."""

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from flotilla.platform.base import (
    ExternalPolicy,
    InstanceSpec,
    Resources,
)
from flotilla.platform.opensandbox import StorageSettings, build
from flotilla.platform.opensandbox.filesystem import FilesystemSettings
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform
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
async def test_probe_pending_filesystem_does_not_become_a_capability(clock: ManualClock) -> None:
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
        # Pending evidence enables nothing: admission against these caps still refuses the trial.
        with pytest.raises(ValueError, match="readonly_rootfs"):
            platform.caps.require_filesystem(True, ())
        with pytest.raises(ValueError, match="probe run label"):
            await platform.probe_create(replace(requested, labels={}))
        with pytest.raises(ValueError, match="combination"):
            await platform.probe_create(replace(requested, privileged=True))
        assert len(bodies) == 1
    finally:
        await platform.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_token_status,leak,held",
    [(403, False, True), (200, False, False), (500, False, False), (403, True, False)],
)
async def test_the_exec_channel_must_reject_a_missing_or_wrong_token(
    clock: ManualClock, bad_token_status: int, leak: bool, held: bool
) -> None:
    """A channel that serves anyone who connects, or a lifecycle read that leaks the token, is a gap."""
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
        assert result.held is held
        assert token not in result.observed and "gateway-secret" not in result.observed
        if held:
            assert len(paths) == 9
            assert {path for path, _ in paths} == {"command", "download", "upload"}
            assert sum(value is None for _, value in paths) == 3
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
