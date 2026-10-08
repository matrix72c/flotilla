"""OpenSandbox 生命周期层：create / get / list / delete / renew / put_policy（backends/opensandbox.md §3.1、§3.3–3.5）。

对着 `httpx.MockTransport` 跑，覆盖翻页与 `metadata` 参数编码、删除幂等、续期体、状态归一。
纯编译与 HTTP 重试见 `test_opensandbox_compile.py`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from flotilla.platform.base import ErrorCategory, FlotillaError, InstanceStatus
from flotilla.platform.fake import ManualClock
from flotilla.platform.opensandbox.http import Http, RetryPolicy
from flotilla.platform.opensandbox.lifecycle import Lifecycle, metadata_filter, parse_state

Handler = Callable[[httpx.Request], httpx.Response]


def _lifecycle(handler: Handler, clock: ManualClock) -> Lifecycle:
    client = httpx.AsyncClient(base_url="http://os.test/v1", transport=httpx.MockTransport(handler))
    return Lifecycle(Http(client, clock, RetryPolicy(attempts=3, initial_s=1.0, cap_s=4.0)))


# ───────────────────────────── create ─────────────────────────────


@pytest.mark.asyncio
async def test_create_posts_body_and_returns_id(clock: ManualClock) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "sb-1"})

    iid = await clock.run(_lifecycle(handler, clock).create({"image": {"uri": "r@sha256:x"}}, timeout=60.0))
    assert iid == "sb-1"
    assert seen == {"method": "POST", "url": "http://os.test/v1/sandboxes"}


@pytest.mark.asyncio
async def test_create_missing_id_is_transient(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"state": "Pending"}})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_lifecycle(handler, clock).create({}, timeout=None))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.stage == "create"


@pytest.mark.asyncio
async def test_create_is_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError):
        await clock.run(_lifecycle(handler, clock).create({}, timeout=None))
    assert calls == 1  # 创建不幂等，不重试（§2.4）


# ───────────────────────────── get / 状态归一 ─────────────────────────────


@pytest.mark.asyncio
async def test_get_parses_state(clock: ManualClock) -> None:
    item = {
        "id": "sb-2",
        "status": {"state": "Running", "reason": "Started", "message": "ok"},
        "expiresAt": "2026-01-01T00:00:00Z",
        "metadata": {"flotilla/unit": "db"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sb-2"
        return httpx.Response(200, json=item)

    state = await clock.run(_lifecycle(handler, clock).get("sb-2"))
    assert state.iid == "sb-2"
    assert state.status is InstanceStatus.RUNNING
    assert state.reason == "Running: Started" and state.message == "ok"
    assert state.labels == {"flotilla/unit": "db"}
    assert state.expires_at == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("Pending", InstanceStatus.PENDING),
        ("Running", InstanceStatus.RUNNING),
        ("Stopping", InstanceStatus.TERMINAL),
        ("Terminated", InstanceStatus.TERMINAL),
        ("Failed", InstanceStatus.TERMINAL),
        ("Paused", InstanceStatus.UNKNOWN),
        ("SomethingNew", InstanceStatus.UNKNOWN),
    ],
)
def test_status_normalization(native: str, expected: InstanceStatus) -> None:
    state = parse_state({"id": "x", "status": {"state": native}})
    assert state.status is expected
    assert state.reason == native  # 无 reason 时仍保留原生串


# ───────────────────────────── delete ─────────────────────────────


@pytest.mark.asyncio
async def test_delete_treats_404_as_success(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "gone"})

    await clock.run(_lifecycle(handler, clock).delete("sb-x"))  # 不抛


@pytest.mark.asyncio
async def test_delete_reraises_other_errors(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "BAD", "message": "no"})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_lifecycle(handler, clock).delete("sb-x"))
    assert exc.value.category is ErrorCategory.INVALID


# ───────────────────────────── list（翻页 + metadata 编码）─────────────────────────────


def test_metadata_filter_joins_sorted_pairs() -> None:
    # 多个条件在同一个 metadata 参数值里，k=v&k=v（§3.1）；此处是未经 httpx 外层编码的参数值。
    assert metadata_filter({"b": "2", "a": "1"}) == "a=1&b=2"


@pytest.mark.asyncio
async def test_list_paginates_and_filters(clock: ManualClock) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        seen.append({"page": p["page"], "pageSize": p["pageSize"], "metadata": p.get("metadata", "")})
        if p["page"] == "1":
            return httpx.Response(
                200,
                json={"items": [{"id": "a", "status": {"state": "Running"}}], "pagination": {"hasNextPage": True}},
            )
        return httpx.Response(
            200,
            json={"items": [{"id": "b", "status": {"state": "Pending"}}], "pagination": {"hasNextPage": False}},
        )

    states = await clock.run(_lifecycle(handler, clock).list({"flotilla/launch": "L1", "flotilla/unit": "db"}))
    assert [s.iid for s in states] == ["a", "b"]
    assert seen[0]["page"] == "1" and seen[1]["page"] == "2"
    assert seen[0]["pageSize"] == "200"
    # 多个条件在同一个 metadata 参数里；httpx 对参数值再编码一层，服务端一次解码恰好拿回 metadata_filter 的结果。
    assert seen[0]["metadata"] == metadata_filter({"flotilla/launch": "L1", "flotilla/unit": "db"})


@pytest.mark.asyncio
async def test_list_dedupes_across_pages(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "dup", "status": {"state": "Pending"}}],
                    "pagination": {"hasNextPage": True},
                },
            )
        return httpx.Response(
            200,
            json={"items": [{"id": "dup", "status": {"state": "Running"}}], "pagination": {"hasNextPage": False}},
        )

    states = await clock.run(_lifecycle(handler, clock).list({}))
    assert len(states) == 1 and states[0].iid == "dup"
    assert states[0].status is InstanceStatus.RUNNING  # 保留后出现的


# ───────────────────────────── renew / put_policy ─────────────────────────────


@pytest.mark.asyncio
async def test_renew_posts_iso_z_expiry(clock: ManualClock) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    when = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await clock.run(_lifecycle(handler, clock).renew("sb-9", when))
    assert captured["path"] == "/v1/sandboxes/sb-9/renew-expiration"
    assert captured["body"] == {"expiresAt": "2026-06-01T12:00:00Z"}


@pytest.mark.asyncio
async def test_put_policy_replaces_whole_policy(clock: ManualClock) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    policy = {"defaultAction": "deny", "egress": [{"action": "allow", "target": "10.0.0.5/32"}]}
    await clock.run(_lifecycle(handler, clock).put_policy("sb-7", policy, stage="wire"))
    assert captured["method"] == "PUT"
    assert captured["path"] == "/v1/sandboxes/sb-7/networkpolicy"
    assert captured["body"] == policy
