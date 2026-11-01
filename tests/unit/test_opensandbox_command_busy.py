"""Busy is an explicit rejection, not an unknown execution outcome or a transport retry."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.core.plan import ExecContext, Healthcheck, Unit
from flotilla.core.process import wait_healthy
from flotilla.platform.base import (
    CommandBusy,
    ErrorCategory,
    ExecAttempt,
    ExecOutcomeUnknown,
    ExecResult,
    FlotillaError,
    InstanceHandle,
    ProcessSpec,
    ProcessStatus,
    Resources,
)
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings
from flotilla.platform.opensandbox.http import Http
from flotilla.testing.environments import FixtureRequest, open_fixture
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task

PREFIX = "/sandboxes/sb-1/proxy/44772"
PROC = ProcessSpec(argv=("secret-command",), uid=0, gid=0, cwd="/", env={"SECRET": "secret-env"}, timeout_s=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_busy_is_not_replayed_or_wrapped(
    clock: ManualClock, caplog: pytest.LogCaptureFixture, background: bool, wrapped: bool
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = {"code": "CONFLICT", "message": "secret-error-body"}
        return httpx.Response(409, json={"detail": body} if wrapped else body, headers={"x-request-id": "req-1"})

    async with httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client:
        execd = Execd(Http(client, clock, prefix=PREFIX), ExecdSettings())
        attempt = ExecAttempt()
        call = execd.start_process if background else execd.exec
        with pytest.raises(CommandBusy) as error:
            await clock.run(call(PROC, attempt=attempt))
    assert calls == 1
    assert attempt.sent and attempt.command_id is None
    assert error.value.category is ErrorCategory.TRANSIENT and error.value.retryable
    assert error.value.stage == ("start" if background else "run")
    assert "request_id=req-1" in str(error.value)
    assert "secret-error-body" not in str(error.value)
    assert "status=409 request_id=req-1 command_id= outcome=CommandBusy" in caplog.text
    assert "duration_s=" in caplog.text and PREFIX in caplog.text
    assert "secret-" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_public_busy_preserves_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
) -> None:
    cfg, platform, _ = deployment
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(409, json={"code": "CONFLICT"})

    async with (
        httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client,
        Flotilla(deployment=cfg) as fleet,
    ):
        env = await open_fixture(fleet, FixtureRequest(task(), "busy"))
        execd = Execd(Http(client, SystemClock(), prefix=PREFIX), ExecdSettings())

        async def execute(
            handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
        ) -> ExecResult:
            return await execd.exec(proc, attempt=attempt)

        async def start(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> str:
            return await execd.start_process(proc, attempt=attempt)

        with monkeypatch.context() as patch:
            patch.setattr(platform, "exec", execute)
            patch.setattr(platform, "start_process", start)
            call = env.agent.start_process if background else env.agent.exec
            with pytest.raises(CommandBusy):
                await call(["true"])
        assert len(requests) == 1
        await env.check()
        assert (await env.agent.exec(["true"])).exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "stream", "body"),
    [
        ("POST", "/command", True, {}),
        ("POST", "/command", True, {"code": "OTHER_CONFLICT"}),
        ("POST", "/command", True, {"detail": "command busy"}),
        ("POST", "/other/command", True, {"code": "CONFLICT"}),
        ("POST", "/sandboxes", False, {"code": "CONFLICT"}),
        ("DELETE", "/command", False, {"code": "CONFLICT"}),
        ("GET", "/command/status/cmd-1", False, {"code": "CONFLICT"}),
    ],
)
async def test_other_conflicts_stay_invalid(
    clock: ManualClock, method: str, path: str, stream: bool, body: dict[str, str]
) -> None:
    async with httpx.AsyncClient(
        base_url="http://execd.test", transport=httpx.MockTransport(lambda r: httpx.Response(409, json=body))
    ) as client:
        http = Http(client, clock, prefix=PREFIX)
        with pytest.raises(FlotillaError) as error:
            await clock.run(
                http.stream_post(path, json={}, stage="run", deadline_s=10)
                if stream
                else http.request(method, path, stage="run", idempotent=True)
            )
    assert error.value.category is ErrorCategory.INVALID and not error.value.retryable


@pytest.mark.asyncio
async def test_busy_healthcheck_skips_one_interval(
    clock: ManualClock, platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.monotonic())
        if len(sent_at) == 1:
            return httpx.Response(409, json={"code": "CONFLICT"})
        return httpx.Response(200, content=b'{"type":"execution_complete"}\n')

    async with httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client:
        execd = Execd(Http(client, clock, prefix=PREFIX), ExecdSettings())

        async def execute(handle: InstanceHandle, proc: ProcessSpec) -> ExecResult:
            return await execd.exec(proc)

        async def status(handle: InstanceHandle, pid: str) -> ProcessStatus:
            return ProcessStatus(found=True, running=True, exit_code=None)

        monkeypatch.setattr(platform, "exec", execute)
        monkeypatch.setattr(platform, "process_status", status)
        unit = Unit(
            image="test",
            resources=Resources(cpu="1", memory="1Gi"),
            context=ExecContext(uid=0, gid=0, cwd="/", env={}),
            command=("serve",),
            healthcheck=Healthcheck(test=("check",), interval_s=2, timeout_s=10, retries=1),
        )
        started = clock.monotonic()
        await clock.run(wait_healthy(platform, clock, InstanceHandle("sb-1"), "web", unit, "pid", started_at=started))
    assert sent_at == [started + 2, started + 4]  # busy did not consume the single allowed healthcheck failure


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("broken_transport", [False, True])
async def test_lost_stream_still_has_unknown_outcome(
    clock: ManualClock, background: bool, broken_transport: bool
) -> None:
    calls = 0

    class Lost(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            # Without init, neither a foreground nor background call has a usable result.
            yield b'{"type":"ping"}\n'
            if broken_transport:
                raise httpx.ReadError("secret-transport-detail")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=Lost())

    async with httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client:
        execd = Execd(Http(client, clock, prefix=PREFIX), ExecdSettings())
        call = execd.start_process if background else execd.exec
        with pytest.raises(ExecOutcomeUnknown) as error:
            await clock.run(call(PROC))
    assert calls == 1 and not error.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_request_timing_logs_are_correlated_and_safe(
    clock: ManualClock, caplog: pytest.LogCaptureFixture, failure: bool
) -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"type":"init","text":"cmd-1"}\n'
            await clock.sleep(3)
            if failure:
                raise httpx.ReadError("secret-transport-detail")
            yield b'{"type":"stdout","text":"secret-output"}\n{"type":"execution_complete"}\n'

    caplog.set_level(logging.DEBUG, logger="flotilla.platform.opensandbox.http")
    async with httpx.AsyncClient(
        base_url="http://execd.test",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Stream(), headers={"x-request-id": "req-1"})
        ),
    ) as client:
        execd = Execd(Http(client, clock, prefix=PREFIX, headers={"X-EXECD-TOKEN": "secret-token"}), ExecdSettings())
        if failure:
            with pytest.raises(ExecOutcomeUnknown):
                await clock.run(execd.exec(PROC))
        else:
            await clock.run(execd.exec(PROC))
    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith("execd ")]
    assert len(messages) == 4  # send, response headers, init, end
    traces = {message.split("trace_id=", 1)[1].split()[0] for message in messages}
    assert len(traces) == 1
    assert "request send" in messages[0] and PREFIX in messages[0]
    assert "status=200 request_id=req-1" in messages[1]
    assert "command_id=cmd-1" in messages[2]
    assert "duration_s=3.000000" in messages[3]
    assert f"outcome={'FlotillaError' if failure else 'response_complete'}" in messages[3]
    assert "secret-" not in caplog.text
