"""Execd HTTP transport: the `phases=` marks its end log carries.

httpcore reports transport milestones only to the request's `trace` extension; the end log of a request with
`status=None` uses them to say whether it was still queued for a connection, connecting, or waiting for the status
line of a request already sent. The marks are simulated here because `httpx.MockTransport` bypasses httpcore.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from flotilla.platform.base import FlotillaError
from flotilla.platform.opensandbox.http import Http
from flotilla.testing.platform import ManualClock

PREFIX = "/sandboxes/sbx/proxy/44772"
MARKS_BEFORE_STATUS = (
    "connection.connect_tcp.started",
    "connection.connect_tcp.complete",
    "http11.send_request_headers.complete",
    "http11.receive_response_headers.started",
)
STATUS = "http11.receive_response_headers.complete"


def _http(handler: httpx.AsyncBaseTransport, clock: ManualClock) -> Http:
    return Http(httpx.AsyncClient(base_url="http://execd.test", transport=handler), clock, prefix=PREFIX)


def _end_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [record for record in caplog.records if "execd request end" in record.getMessage()]
    return record


@pytest.mark.asyncio
async def test_end_log_says_a_sent_request_was_waiting_for_the_status_line(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        trace = request.extensions["trace"]
        for name in MARKS_BEFORE_STATUS:
            await trace(name, {})
            await clock.advance(0.25)
        raise httpx.ReadTimeout("no status line", request=request)

    caplog.set_level(logging.DEBUG, logger="flotilla.platform.opensandbox.http")
    with pytest.raises(FlotillaError):
        await _http(httpx.MockTransport(handler), clock).stream_post("/command", json={}, stage="run", deadline_s=10)
    record = _end_log(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "status=None" in message
    assert "phases=connect@0.000 connected@0.250 sent@0.500 status_wait@0.750" in message
    assert " status@" not in message


@pytest.mark.asyncio
async def test_end_log_says_when_no_connection_was_assigned(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    caplog.set_level(logging.DEBUG, logger="flotilla.platform.opensandbox.http")
    with pytest.raises(FlotillaError):
        await _http(httpx.MockTransport(handler), clock).stream_post("/command", json={}, stage="run", deadline_s=10)
    message = _end_log(caplog).getMessage()
    assert "status=None" in message
    assert "phases=none" in message


@pytest.mark.asyncio
async def test_end_log_marks_a_request_that_got_its_status_line(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        trace = request.extensions["trace"]
        for name in (*MARKS_BEFORE_STATUS, STATUS):
            await trace(name, {})
            await clock.advance(0.25)
        return httpx.Response(200, content=b'{"type":"execution_complete"}\n')

    caplog.set_level(logging.DEBUG, logger="flotilla.platform.opensandbox.http")
    text = await _http(httpx.MockTransport(handler), clock).stream_post("/command", json={}, stage="run", deadline_s=10)
    assert text == '{"type":"execution_complete"}\n'
    record = _end_log(caplog)
    assert record.levelno == logging.DEBUG
    message = record.getMessage()
    assert "status=200" in message
    assert "phases=connect@0.000 connected@0.250 sent@0.500 status_wait@0.750 status@1.000" in message
