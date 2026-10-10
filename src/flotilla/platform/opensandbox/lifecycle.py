"""生命周期：创建、查询、列出（过滤与翻页）、删除、续期、策略整体替换（backends/opensandbox.md §3.1、§3.3–3.5）。

状态归一（§3.5）：`Pending` → PENDING，`Running` → RUNNING，`Stopping` / `Terminated` / `Failed` → TERMINAL，
其余（含暂停相关状态与规范日后新增的值）→ UNKNOWN；原生状态串放 `reason`，`SandboxStatus.message` 放 `message`。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from flotilla.platform.base import ErrorCategory, FlotillaError, InstanceState, InstanceStatus, Stage
from flotilla.platform.opensandbox.http import Http

PAGE_SIZE = 200  # 规范的 pageSize 上限（§3.1）
MAX_PAGES = 10_000  # 翻页不收敛时报错，而不是无限循环

_STATUS = {
    "Pending": InstanceStatus.PENDING,
    "Running": InstanceStatus.RUNNING,
    "Stopping": InstanceStatus.TERMINAL,
    "Terminated": InstanceStatus.TERMINAL,
    "Failed": InstanceStatus.TERMINAL,
}


def sandbox_path(iid: str) -> str:
    return f"/sandboxes/{quote(iid, safe='')}"


def parse_state(item: Mapping[str, Any]) -> InstanceState:
    """`Sandbox` 对象 → `InstanceState`。"""
    status = item.get("status") or {}
    native = str(status.get("state") or "")
    reason = status.get("reason")
    expires = item.get("expiresAt")
    return InstanceState(
        iid=str(item["id"]),
        status=_STATUS.get(native, InstanceStatus.UNKNOWN),
        expires_at=_parse_time(expires) if expires else None,
        labels={str(k): str(v) for k, v in (item.get("metadata") or {}).items()},
        reason=f"{native}: {reason}" if reason else (native or None),
        message=status.get("message"),
    )


def metadata_filter(labels: Mapping[str, str]) -> str:
    """多个条件写在同一个 `metadata` 参数里：`k1=v1&k2=v2` 整体作为参数值（§3.1），由 httpx 再编码一次。"""
    return urlencode(sorted(labels.items()))


class Lifecycle:
    def __init__(self, http: Http) -> None:
        self._http = http

    async def create(self, body: Mapping[str, Any], *, timeout: float | None) -> str:
        """`POST /sandboxes`，返回实例 ID。不重试（创建不幂等，§2.4）。"""
        result = await self._http.request(
            "POST", "/sandboxes", stage="create", idempotent=False, json=body, timeout=timeout
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise FlotillaError(
                # 只报字段名：创建响应会原样带回 `env`（含 execd 访问 token），不进错误信息与日志。
                f"创建响应缺少 id（字段：{sorted(result) if isinstance(result, dict) else type(result).__name__}）",
                stage="create",
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )
        return str(result["id"])

    async def get(self, iid: str) -> InstanceState:
        result = await self._http.request("GET", sandbox_path(iid), stage="run", idempotent=True)
        return parse_state(result)

    async def delete(self, iid: str) -> None:
        """删除；不存在视为成功（§2.3）。"""
        try:
            await self._http.request("DELETE", sandbox_path(iid), stage="run", idempotent=True)
        except FlotillaError as exc:
            if exc.category is not ErrorCategory.NOT_FOUND:
                raise

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        """按标签过滤、翻页取全。"""
        out: list[InstanceState] = []
        params: dict[str, str | int] = {"pageSize": PAGE_SIZE}
        if labels:
            params["metadata"] = metadata_filter(labels)
        for page in range(1, MAX_PAGES + 1):
            params["page"] = page
            result = await self._http.request("GET", "/sandboxes", stage="run", idempotent=True, params=params)
            out.extend(parse_state(item) for item in result.get("items") or [])
            if not (result.get("pagination") or {}).get("hasNextPage"):
                return _dedupe(out)
        raise FlotillaError("列出实例翻页超过上限", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)

    async def renew(self, iid: str, expires_at: datetime) -> None:
        body = {"expiresAt": expires_at.isoformat().replace("+00:00", "Z")}
        await self._http.request(
            "POST", f"{sandbox_path(iid)}/renew-expiration", stage="run", idempotent=True, json=body
        )

    async def put_policy(self, iid: str, policy: Mapping[str, Any], *, stage: Stage) -> None:
        """`PUT /sandboxes/{id}/networkpolicy` 整体替换（§4.5），幂等。"""
        await self._http.request("PUT", f"{sandbox_path(iid)}/networkpolicy", stage=stage, idempotent=True, json=policy)


def _dedupe(states: list[InstanceState]) -> list[InstanceState]:
    """翻页期间实例增删可能让同一实例出现两次；按 iid 去重，保留后出现的。"""
    by_id = {s.iid: s for s in states}
    return list(by_id.values())


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
