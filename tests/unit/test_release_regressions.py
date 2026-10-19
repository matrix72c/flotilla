"""Behavioral regressions for the public-release audit."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from flotilla import Flotilla, FlotillaError
from flotilla._io import owned_io
from flotilla.build.builder import base_images
from flotilla.build.docker import DockerRegistry, extract_archive
from flotilla.build.images import ImageError
from flotilla.build.keys import entries
from flotilla.compose.normalize import normalize
from flotilla.config import FlotillaConfig
from flotilla.platform.base import ExecAttempt, InstanceHandle, ProcessSpec
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings, multipart_upload
from flotilla.platform.opensandbox.http import Http, RetryPolicy
from flotilla.preparation.service import Preparer
from flotilla.runtime.events import EventDispatcher
from flotilla.runtime.types import RuntimeEvent
from tests.support.environments import FixtureRequest, open_fixture
from tests.support.platform import FakePlatform, ManualClock, SharedTree
from tests.support.runtime import deployment as deployment
from tests.support.runtime import task


def test_binary_multipart_reselects_colliding_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    choices = iter(["collision", "pathcollision", "safe-boundary"])
    monkeypatch.setattr("flotilla.platform.opensandbox.execd.secrets.token_hex", lambda _: next(choices))
    content = bytes(range(256)) + b"\r\n--collision\r\n" + bytes(range(256))
    content_type, body = multipart_upload("/pathcollision", content, 0o6751)
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    parts = list(message.iter_parts())
    metadata = parts[0].get_payload(decode=True)
    assert isinstance(metadata, bytes)
    assert json.loads(metadata)["mode"] == 6751
    assert parts[1].get_payload(decode=True) == content


def test_from_options_continuations_aliases_and_arg() -> None:
    assert base_images(
        "FROM --platform=linux/amd64 \\\n ubuntu:24.04 AS build\nFROM BUILD AS later\nFROM ${BASE}\nFROM scratch"
    ) == ["ubuntu:24.04", "${BASE}"]


def test_missing_amd64_never_selects_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerRegistry, "_run", lambda *_: json.dumps({"linux/arm64": {"config": {}}}))
    with pytest.raises(ImageError, match="linux/amd64"):
        DockerRegistry().image_config("fixture")


def test_archive_metadata_is_independent_of_local_owner(tmp_path: Path) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        info = tarfile.TarInfo("payload")
        info.uid, info.gid, info.mode, info.size = 45678, 45679, 0o6751, 4
        archive.addfile(info, io.BytesIO(b"data"))
    data.seek(0)
    root = tmp_path / "files"
    root.mkdir()
    owners: dict[str, tuple[int, int, int]] = {}
    assert extract_archive(data, root, owners)
    (tmp_path / "file-identities.json").write_text(json.dumps(owners))
    record = entries(root)[0]
    assert (record["uid"], record["gid"], record["mode"]) == (45678, 45679, "6751")
    assert (root / "payload").read_bytes() == b"data"


@pytest.mark.parametrize("port", [{"protocol": "udp"}, "bad/udp", {"target": False}, [80]])
def test_invalid_port_has_field_path(port: object) -> None:
    project = normalize({"services": {"main": {"image": "alpine", "ports": [port]}}}, {})
    assert any(f.kind == "reject" and f.path.startswith("services.main.ports[0]") for f in project.findings)


@pytest.mark.asyncio
async def test_timeout_requires_confirmed_remote_stop(clock: ManualClock) -> None:
    requests = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b'{"type":"init","text":"cmd"}\n'
            await clock.sleep(100)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, stream=Stream())
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"running": False, "exit_code": -1})

    async with httpx.AsyncClient(base_url="http://fixture", transport=httpx.MockTransport(handle)) as client:
        backend = Execd(Http(client, clock, RetryPolicy()), ExecdSettings())
        result = await clock.run(backend.exec(ProcessSpec(("sleep", "100"), 0, 0, "/", {}, 1)))
    assert result.timed_out and requests == ["POST", "DELETE", "GET"]


@pytest.mark.asyncio
async def test_cancelled_thread_finishes_before_owner_returns() -> None:
    entered, finish = threading.Event(), threading.Event()

    def work() -> None:
        entered.set()
        assert finish.wait(2)

    pending = asyncio.create_task(owned_io(work))
    assert await asyncio.to_thread(entered.wait, 1)
    pending.cancel()
    await asyncio.sleep(0.01)
    pending.cancel()
    assert not pending.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending


def test_event_queue_drops_diagnostics_without_blocking() -> None:
    entered, finish = threading.Event(), threading.Event()

    def callback(_: RuntimeEvent) -> None:
        entered.set()
        finish.wait(2)

    dispatcher = EventDispatcher(callback)
    event = RuntimeEvent("test", "env", "request", "digest")
    try:
        dispatcher.emit(event)
        assert entered.wait(1)
        for _ in range(1030):
            dispatcher.emit(event)
        assert dispatcher.dropped == 6
    finally:
        dispatcher.close()
        finish.set()
        dispatcher.thread.join(2)
    assert not dispatcher.thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("known_id", [True, False])
async def test_background_start_cancel_accounts_for_remote_effect(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    known_id: bool,
) -> None:
    cfg, platform, _ = deployment
    started = threading.Event()
    original = platform.start_process
    child: list[tuple[InstanceHandle, str]] = []

    async def lost_response(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> str:
        pid = await original(handle, proc, attempt=attempt)
        child.append((handle, pid))
        if attempt is not None and not known_id:
            attempt.command_id = None
        started.set()
        await asyncio.sleep(100)
        return pid

    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, FixtureRequest(task(), "background-cancel"))
        monkeypatch.setattr(platform, "start_process", lost_response)
        pending = asyncio.create_task(env.agent.start_process(["sleep", "100"]))
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        if known_id:
            await env.check()
            assert not (await platform.process_status(*child[0])).running
        else:
            with pytest.raises(FlotillaError):
                await env.check()
            assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_preparation_limits_work_and_cancelled_queue_has_no_operation(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _, _ = deployment
    cfg = cfg.model_copy(update={"preparation": cfg.preparation.model_copy(update={"concurrency": 2})})
    original = Preparer._build_process
    active = peak = 0
    entered, finish = threading.Event(), threading.Event()

    async def blocked(self: Preparer, request: Path) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            while not finish.is_set():
                await asyncio.sleep(0.005)
            await original(self, request)
        finally:
            active -= 1

    monkeypatch.setattr(Preparer, "_build_process", blocked)
    async with Flotilla(deployment=cfg) as fleet:
        pending = [asyncio.create_task(open_fixture(fleet, FixtureRequest(task(), str(i)))) for i in range(3)]
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending[-1].cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending[-1]
            assert fleet._preparer is not None and len(fleet._preparer.operations) == 2
        finally:
            finish.set()
        envs = await asyncio.gather(*pending[:2])
        assert peak == 2
        await asyncio.gather(*(env.release() for env in envs))


@pytest.mark.asyncio
async def test_anchor_stat_preserves_special_permission_bits(
    platform: FakePlatform,
    clock: ManualClock,
) -> None:
    from flotilla.core.anchor import Anchor, AnchorSettings

    tree = SharedTree(platform)
    anchor = Anchor(platform, clock, "numeric-stat", AnchorSettings(image="fixture"))
    await clock.run(anchor.start())
    try:
        tree.files.add("payload")
        tree.modes["payload"] = "7755"
        tree.owners["payload"] = "1234:5678"
        info = await clock.run(anchor.stat("payload", stage="prepare"))
        assert (info.mode, info.uid, info.gid) == (0o7755, 1234, 5678)
    finally:
        await anchor.close()


@pytest.mark.asyncio
async def test_unknown_fake_command_fails(platform: FakePlatform) -> None:
    from tests.support.platform import _default_exec_handler

    with pytest.raises(AssertionError, match="unconfigured"):
        _default_exec_handler("fixture", ProcessSpec(("unimplemented",), 0, 0, "/", {}, 1))


@pytest.mark.asyncio
async def test_http_response_body_does_not_escape_into_diagnostics(clock: ManualClock) -> None:
    import traceback

    secret = "synthetic-sensitive-value"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": {"message": secret, "code": "INVALID"}})

    async with httpx.AsyncClient(base_url="http://fixture", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FlotillaError) as error:
            await Http(client, clock, RetryPolicy()).request("POST", "/command", stage="run", idempotent=False)
    assert secret not in "".join(traceback.format_exception(error.value))
    assert "HTTP 400" in str(error.value) and "INVALID" in str(error.value)


@pytest.mark.asyncio
async def test_undelivered_success_has_cancellation_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from flotilla.runtime.worker import Worker

    worker = Worker()
    shield = asyncio.shield
    cleaned = []

    async def cancellation_at_delivery(future):  # type: ignore[no-untyped-def]
        value = await shield(future)
        if value == "command-id":
            raise asyncio.CancelledError
        return value

    async def cleanup(value: str) -> None:
        cleaned.append(value)

    monkeypatch.setattr(asyncio, "shield", cancellation_at_delivery)
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker.call(lambda: asyncio.sleep(0, result="command-id"), settle_cancel=True, cancel_result=cleanup)
        assert cleaned == ["command-id"]
    finally:
        worker.loop.call_soon_threadsafe(worker.loop.stop)
        await asyncio.to_thread(worker.thread.join, 2)


@pytest.mark.parametrize("field,value", [("networks", []), ("volumes", False)])
def test_wrong_empty_table_types_are_not_missing(field: str, value: object) -> None:
    project = normalize({"services": {"main": {"image": "alpine"}}, field: value}, {})
    assert any(f.kind == "reject" and f.path == field for f in project.findings)


@pytest.mark.parametrize("value", ["on-failure:3", "on-failure-arbitrary", "on-failure:"])
def test_restart_counts_and_prefix_matches_are_rejected(value: str) -> None:
    project = normalize({"services": {"main": {"image": "alpine", "restart": value}}}, {})
    assert any(f.kind == "reject" and f.path == "services.main.restart" for f in project.findings)
