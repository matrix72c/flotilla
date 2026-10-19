"""Bounded transport retries for idempotent requests and safe error diagnostics."""

from __future__ import annotations

import asyncio
import json as json_module
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from flotilla.platform.base import Clock, ErrorCategory, ExecAttempt, ExecOutcomeUnknown, FlotillaError, Stage

log = logging.getLogger(__name__)


def _json_body(response: httpx.Response) -> Any:
    """JSON of a successful response (None when there is no body)."""
    return response.json() if response.content else None


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5  # including the first
    initial_s: float = 0.5
    cap_s: float = 10.0
    max_retry_after_s: float = 60.0


def error_fields(body: object) -> tuple[str | None, str | None]:
    """Error body -> (code, message). Accepts flat `{code, message}` and wrapped `{"detail": {code, message}}`;
    a string `detail` is taken as the message. Any other shape yields None for both."""
    if not isinstance(body, Mapping):
        return None, None
    detail = body.get("detail")
    if isinstance(detail, Mapping):
        body = detail
    elif isinstance(detail, str) and "message" not in body:
        return _str_or_none(body.get("code")), detail
    return _str_or_none(body.get("code")), _str_or_none(body.get("message"))


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def map_status(status: int, code: str | None) -> tuple[ErrorCategory, bool]:
    """HTTP status and platform error code -> (category, retryable reported to callers)."""
    if status == 429:
        return ErrorCategory.RATE_LIMITED, True
    if status == 403 and code == "QUOTA_EXCEEDED":
        return ErrorCategory.CAPACITY, True
    if status == 404:
        return ErrorCategory.NOT_FOUND, False
    if status >= 500:
        return ErrorCategory.TRANSIENT, True
    return ErrorCategory.INVALID, False


def _retryable(category: ErrorCategory) -> bool:
    return category in (ErrorCategory.RATE_LIMITED, ErrorCategory.TRANSIENT)


DEFAULT_RETRY = RetryPolicy()


class Http:
    """Requests against one base (client base_url + `prefix`). Callers build `client` (tests inject a MockTransport)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        retry: RetryPolicy = DEFAULT_RETRY,
        *,
        prefix: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock
        self._retry = retry
        self._prefix = prefix
        self._headers = dict(headers or {})

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def with_prefix(self, prefix: str) -> Http:
        """Same client, clock, and retry policy, with `prefix` appended to the path (e.g. an instance's execd proxy)."""
        return Http(self._client, self._clock, self._retry, prefix=self._prefix + prefix, headers=self._headers)

    def with_headers(self, headers: Mapping[str, str]) -> Http:
        """Same base, adding `headers` to each request (merged with per-call headers, which win). Never logged."""
        return Http(self._client, self._clock, self._retry, prefix=self._prefix, headers={**self._headers, **headers})

    def _merged(self, headers: Mapping[str, str] | None) -> dict[str, str] | None:
        if not self._headers:
            return dict(headers) if headers is not None else None
        return {**self._headers, **(headers or {})}

    async def request(
        self,
        method: str,
        path: str,
        *,
        stage: Stage,
        idempotent: bool,
        json: Any = None,
        params: Mapping[str, str | int] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send the request and return parsed JSON (None without a body). Failures map to `FlotillaError`.

        `content` + `headers` are for non-JSON bodies (execd fixed-length multipart upload): the caller builds the
        body and sets `Content-Type`, bypassing httpx's streaming multipart encoder. `timeout=None` uses the client
        default.
        """
        url = self._prefix + path

        async def send() -> httpx.Response:
            return await self._client.request(
                method,
                url,
                json=json,
                params=params,
                content=content,
                headers=self._merged(headers),
                timeout=_timeout(timeout),
            )

        return await self._run(send, _json_body, stage=stage, idempotent=idempotent, method=method, path=url)

    async def download(
        self,
        path: str,
        *,
        stage: Stage,
        params: Mapping[str, str | int] | None = None,
        timeout: float | None = None,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> bytes:
        """GET raw bytes (execd `/files/download`). Idempotent and retried."""
        url = self._prefix + path

        async def send() -> httpx.Response:
            async with self._client.stream(
                "GET", url, params=params, headers=self._merged(None), timeout=_timeout(timeout)
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > max_bytes:
                        raise FlotillaError(
                            "download exceeds transfer limit",
                            stage=stage,
                            category=ErrorCategory.INVALID,
                            retryable=False,
                        )
                    body.extend(chunk)
                # aiter_bytes already decoded content encoding. Keep status/retry headers only;
                # constructing a response with Content-Encoding would decode the body twice.
                headers = dict(response.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(
                    response.status_code, headers=headers, content=bytes(body), request=response.request
                )

        return await self._run(send, lambda r: r.content, stage=stage, idempotent=True, method="GET", path=url)

    async def stream_post(
        self,
        path: str,
        *,
        json: Any,
        stage: Stage,
        deadline_s: float,
        attempt: ExecAttempt | None = None,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> str:
        """POST and read the whole response stream, returning raw text (execd `/command` NDJSON). Not retried.

        Consumes in chunks with a size limit and records the command ID as soon as init arrives. The whole request
        takes at most `deadline_s` seconds (via `Clock`), then fails as transient: execd sends a `ping` every
        3 seconds, so the client read timeout never fires; only a total deadline ends a stream that never closes.
        """
        url = self._prefix + path

        async def read() -> httpx.Response:
            headers = self._merged({"Accept": "text/event-stream"})
            if attempt is not None:
                attempt.sent = True
            async with self._client.stream("POST", url, json=json, headers=headers) as response:
                body = bytearray()
                pending = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise ExecOutcomeUnknown(
                            "execd output exceeds configured limit", command_id=attempt.command_id if attempt else None
                        )
                    body.extend(chunk)
                    if attempt is not None and attempt.command_id is None and response.is_success:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, tail = pending.partition(b"\n")
                            pending = bytearray(tail)
                            try:
                                event = json_module.loads(line.removeprefix(b"data:").strip())
                            except ValueError:
                                continue
                            if isinstance(event, dict) and event.get("type") == "init" and event.get("text"):
                                attempt.command_id = str(event["text"])
                return httpx.Response(response.status_code, content=bytes(body), request=response.request)

        async def send() -> httpx.Response:
            return await self._bounded(read(), deadline_s)

        return await self._run(send, lambda r: r.text, stage=stage, idempotent=False, method="POST", path=url)

    async def _bounded(self, aw: Awaitable[httpx.Response], deadline_s: float) -> httpx.Response:
        """Finish `aw` within `deadline_s` on `Clock`, else cancel it and raise `httpx.TimeoutException` (transient)."""
        task = asyncio.ensure_future(aw)
        timer = asyncio.ensure_future(self._clock.sleep(deadline_s))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            await asyncio.wait({task})
            raise
        finally:
            timer.cancel()
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieve the exception so asyncio doesn't report "never retrieved"
        raise httpx.TimeoutException(f"not finished after {deadline_s}s")

    async def _run[T](
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        extract: Callable[[httpx.Response], T],
        *,
        stage: Stage,
        idempotent: bool,
        method: str,
        path: str,
    ) -> T:
        """Retry/backoff skeleton: `send` issues the request and reads the body; `extract` gets the success result."""
        delay = self._retry.initial_s
        for attempt in range(1, self._retry.attempts + 1):
            try:
                response = await send()
            except httpx.TransportError as exc:  # incl. timeouts and connection errors: outcome unknown, transient
                error = FlotillaError(
                    f"{method} {path}: {type(exc).__name__}",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                )
                retry_after = None
            else:
                if response.is_success:
                    return extract(response)
                error = self.error(response, stage=stage, method=method, path=path)
                retry_after = _retry_after(response)
            log.debug("%s %s failed (attempt %d): %s", method, path, attempt, error.category)
            if not idempotent or not _retryable(error.category) or attempt == self._retry.attempts:
                raise error
            wait = min(retry_after, self._retry.max_retry_after_s) if retry_after is not None else delay
            await self._clock.sleep(wait)
            delay = min(delay * 2, self._retry.cap_s)
        raise AssertionError("unreachable")

    @staticmethod
    def error(response: httpx.Response, *, stage: Stage, method: str, path: str) -> FlotillaError:
        try:
            code, _detail = error_fields(response.json())
        except ValueError:
            code, _detail = None, None
        category, retryable = map_status(response.status_code, code)
        safe_code = code if code and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else None
        request_id = response.headers.get("x-request-id", "")
        safe_id = request_id if re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id) else ""
        message = f"{method} {path}: HTTP {response.status_code}"
        if safe_code:
            message += f" ({safe_code})"
        if safe_id:
            message += f" request_id={safe_id}"
        return FlotillaError(message, stage=stage, category=category, retryable=retryable)


def _timeout(timeout: float | None) -> Any:
    """`None` -> client default timeout (`httpx.USE_CLIENT_DEFAULT`). An explicit `timeout=None` in httpx disables all
    timeouts, so never pass it. Returns `Any`: the sentinel type `UseClientDefault` is not publicly exported."""
    return httpx.USE_CLIENT_DEFAULT if timeout is None else timeout


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
