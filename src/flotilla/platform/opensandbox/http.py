"""HTTP：错误映射、对幂等请求的重试与退避、请求日志脱敏（backends/opensandbox.md §3.5）。

错误映射（§3.5）：

| 平台返回 | 类别 |
|---|---|
| 429（带 `Retry-After`） | `rate_limited`，按 `Retry-After` 退避 |
| 403 且 `code` 为 `QUOTA_EXCEEDED` | `capacity` |
| 503、其他 5xx、连接错误、超时 | `transient` |
| 400 / 422 / 413 | `invalid` |
| 其他 401 / 403 | `invalid`（凭证或配置错误，不重试） |
| 404 | `not_found` |

错误体：上游 server 把 `HTTPException` 拍平为 `{code, message}`；FastAPI 默认的
`{"detail": {code, message}}`（§3.5）同样接受，由 `error_fields` 统一取出 `code` 与 `message`。

**只对幂等请求重试**（`idempotent=True`）：查询、列出、删除、续期（同一过期时间）、整体替换策略。创建、命令执行、
文件覆盖不重试（Architecture §2.4、PRD N5）——它们的失败原样报给调用方，由编排核心对账或报告。

凭证只出现在请求头中，日志只记方法、路径与状态码，不记请求头与请求体（原则 8）。重试的等待经注入的 `Clock`。

**超时**：单次请求默认用 client 的超时（`build()` 设定）；`timeout=None` 表示"用 client 默认"，不是"不限时"。
SSE 命令流另有经 `Clock` 的总时限（`stream_post` 的 `deadline_s`）：execd 定期发 `ping`，读超时管不住它。

一个 `httpx.AsyncClient` 由全部实例共用：`with_prefix` 派生出指向某实例 execd 的 `Http`（同一连接池）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from flotilla.platform.base import Clock, ErrorCategory, FlotillaError, Stage

log = logging.getLogger(__name__)


def _json_body(response: httpx.Response) -> Any:
    """成功响应的 JSON（无 body 时为 None）。"""
    return response.json() if response.content else None


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5  # 含第一次
    initial_s: float = 0.5
    cap_s: float = 10.0
    max_retry_after_s: float = 60.0


def error_fields(body: object) -> tuple[str | None, str | None]:
    """错误体 → (code, message)。接受扁平的 `{code, message}` 与包一层的 `{"detail": {code, message}}`；
    `detail` 为字符串时当作 message。其余形态两者都为 None。"""
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
    """HTTP 状态码与平台错误码 → (类别, 向使用方报告的 retryable)。"""
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
    """一个基址（client 的 base_url + `prefix`）上的请求。`client` 由调用方构造（测试注入 `httpx.MockTransport`）。"""

    def __init__(
        self, client: httpx.AsyncClient, clock: Clock, retry: RetryPolicy = DEFAULT_RETRY, *, prefix: str = ""
    ) -> None:
        self._client = client
        self._clock = clock
        self._retry = retry
        self._prefix = prefix

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def with_prefix(self, prefix: str) -> Http:
        """同一 client、时钟与重试策略，路径再加 `prefix`（例如某实例的 execd 代理前缀，§3.1）。"""
        return Http(self._client, self._clock, self._retry, prefix=self._prefix + prefix)

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
        """发出请求，返回解析后的 JSON（无响应体时为 None）。失败按 §3.5 映射为 `FlotillaError`。

        `content` + `headers` 用于非 JSON 的请求体（execd 的定长 multipart 上传，§3.6）：由调用方拼好
        body 并设 `Content-Type`，不走 httpx 的流式 multipart 编码器。`timeout=None` 用 client 默认。
        """
        url = self._prefix + path

        async def send() -> httpx.Response:
            return await self._client.request(
                method, url, json=json, params=params, content=content, headers=headers, timeout=_timeout(timeout)
            )

        return await self._run(send, _json_body, stage=stage, idempotent=idempotent, method=method, path=url)

    async def download(
        self,
        path: str,
        *,
        stage: Stage,
        params: Mapping[str, str | int] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """GET 原始字节（execd 的 `/files/download`，§3.6）。幂等，按 §3.5 重试。"""
        url = self._prefix + path

        async def send() -> httpx.Response:
            return await self._client.request("GET", url, params=params, timeout=_timeout(timeout))

        return await self._run(send, lambda r: r.content, stage=stage, idempotent=True, method="GET", path=url)

    async def stream_post(
        self,
        path: str,
        *,
        json: Any,
        stage: Stage,
        deadline_s: float,
    ) -> str:
        """POST 并读完响应流，返回原文（execd `/command` 的 NDJSON，§3.6）。不幂等、不重试。

        流在命令结束或超时后收尾；body 在 `stream` 上下文内整体读入（execd 把事件缓存在内存，读全量与
        逐事件无差别，和 Go SDK 一致）。整个请求最多 `deadline_s` 秒（经 `Clock`），到时按 transient 报错：
        execd 每 3 秒发一次 `ping`，client 的读超时永远等不到，只有总时限能结束一个不收尾的流。
        """
        url = self._prefix + path

        async def read() -> httpx.Response:
            async with self._client.stream("POST", url, json=json, headers={"Accept": "text/event-stream"}) as response:
                await response.aread()  # 读进 response.content，退出上下文后仍可用
                return response

        async def send() -> httpx.Response:
            return await self._bounded(read(), deadline_s)

        return await self._run(send, lambda r: r.text, stage=stage, idempotent=False, method="POST", path=url)

    async def _bounded(self, aw: Awaitable[httpx.Response], deadline_s: float) -> httpx.Response:
        """在 `Clock` 上的 `deadline_s` 秒内完成 `aw`，否则取消它并抛 `httpx.TimeoutException`（按 transient 映射）。"""
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
            task.exception()  # 取走异常，免得 asyncio 报 "never retrieved"
        raise httpx.TimeoutException(f"超过 {deadline_s}s 未完成")

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
        """重试/退避骨架（§3.5）：`send` 发请求并读完 body，成功时 `extract` 取结果。"""
        delay = self._retry.initial_s
        for attempt in range(1, self._retry.attempts + 1):
            try:
                response = await send()
            except httpx.TransportError as exc:  # 含超时、连接错误：结果未知，按 transient
                error = FlotillaError(
                    f"{method} {path} 失败：{exc!r}", stage=stage, category=ErrorCategory.TRANSIENT, retryable=True
                )
                retry_after = None
            else:
                if response.is_success:
                    return extract(response)
                error = self.error(response, stage=stage, method=method, path=path)
                retry_after = _retry_after(response)
            log.debug("%s %s 失败（第 %d 次）：%s", method, path, attempt, error.category)
            if not idempotent or not _retryable(error.category) or attempt == self._retry.attempts:
                raise error
            wait = min(retry_after, self._retry.max_retry_after_s) if retry_after is not None else delay
            await self._clock.sleep(wait)
            delay = min(delay * 2, self._retry.cap_s)
        raise AssertionError("unreachable")

    @staticmethod
    def error(response: httpx.Response, *, stage: Stage, method: str, path: str) -> FlotillaError:
        try:
            code, detail = error_fields(response.json())
        except ValueError:
            code, detail = None, None
        category, retryable = map_status(response.status_code, code)
        message = (
            f"{method} {path} → {response.status_code}"
            + (f" {code}" if code else "")
            + f"：{detail or response.text[:300]}"
        )
        return FlotillaError(message, stage=stage, category=category, retryable=retryable)


def _timeout(timeout: float | None) -> Any:
    """`None` → client 默认超时（`httpx.USE_CLIENT_DEFAULT`）。httpx 里显式 `timeout=None` 是关掉全部超时，
    这里绝不传它。返回类型写 `Any`：哨兵的类型 `UseClientDefault` 不在 httpx 的公开导出里。"""
    return httpx.USE_CLIENT_DEFAULT if timeout is None else timeout


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
