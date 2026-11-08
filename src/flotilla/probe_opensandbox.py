"""OpenSandbox-only probe operations; never inferred from normal runtime evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from flotilla.capabilities import Item
from flotilla.platform.base import ExternalPolicy, InstanceHandle, Platform
from flotilla.platform.opensandbox.execd import command_body, multipart_upload
from flotilla.platform.opensandbox.lifecycle import metadata_filter, sandbox_path
from flotilla.platform.opensandbox.network import compile_policy
from flotilla.platform.opensandbox.platform import EXECD_TOKEN_HEADER, OpenSandboxPlatform


async def policy(
    platform: Platform,
    handle: InstanceHandle,
    *,
    egress: Sequence[InstanceHandle] = (),
    ingress: Sequence[InstanceHandle] = (),
    external: ExternalPolicy | None = None,
) -> None:
    """Change egress independently of ingress, preserving the configured policy dialect."""
    if not isinstance(platform, OpenSandboxPlatform):
        raise NotImplementedError("backend has no asymmetric probe policy helper")
    unit = await platform._unit_of(handle.iid, stage="wire")
    settings = platform._settings

    async def targets(handles: Sequence[InstanceHandle]) -> list[str]:
        return [h.iid if settings.link == "sandbox" else await platform.internal_address(h) for h in handles]

    effective = external or unit.external
    result = compile_policy(effective, await targets(egress), settings)
    if settings.ingress == "sandbox":
        result["ingress"] = compile_policy(effective, await targets(ingress), settings)["ingress"]
    elif ingress:
        raise NotImplementedError("configured backend cannot independently express ingress")
    await platform._lifecycle.put_policy(handle.iid, result, stage="wire", headers={EXECD_TOKEN_HEADER: unit.token})


async def unmounted_mountinfo(platform: Platform, handle: InstanceHandle) -> str:
    """Inspect a no-volume create without depending on the absent shared exec wrapper."""
    if not isinstance(platform, OpenSandboxPlatform):
        raise NotImplementedError("backend has no unmounted inspection")
    from flotilla.platform.opensandbox.execd import fold_exec, parse_events

    unit = await platform._unit_of(handle.iid, stage="run")
    http = platform._execd_http(handle.iid).with_headers({EXECD_TOKEN_HEADER: unit.token})
    body = {"cwd": "/", "uid": 0, "gid": 0, "background": False, "timeout": 30000}
    if platform._execd.protocol == "legacy":
        body["command"] = "/bin/sh -c 'cat /proc/self/mountinfo'"
    else:
        body["argv"] = ["/bin/sh", "-c", "cat /proc/self/mountinfo"]
    result = fold_exec(parse_events(await http.stream_post("/command", json=body, stage="run", deadline_s=30)))
    if result.exit_code != 0 or result.timed_out:
        raise ValueError("unmounted inspection failed")
    return result.stdout.decode()


async def authentication(platform: Platform, handle: InstanceHandle, *, run_id: str, scope: str = "ordinary") -> Item:
    if not isinstance(platform, OpenSandboxPlatform):
        return Item(status="skipped", evidence="backend has no authentication probe", scope=scope)
    from flotilla.probe import _sh

    unit = await platform._unit_of(handle.iid, stage="run")
    http = platform._execd_http(handle.iid)
    body = command_body(_sh("true"), background=False, settings=platform._execd)
    # Use an owned regular file as the download positive control. System-file access is
    # measured separately; failure there must not obscure authentication on this route.
    file_path = "/tmp/flotilla-auth-probe"
    content_type, content = multipart_upload(file_path, b"probe", 0o600)
    routes: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", "/command", {"json": body}),
        ("POST", "/files/upload", {"content": content, "headers": {"Content-Type": content_type}}),
        ("GET", "/files/download", {"params": {"path": file_path}}),
    ]
    observed = []
    for method, path, kwargs in routes:
        for kind, token in (("valid", unit.token), ("missing", None), ("wrong", "flotilla-probe-invalid")):
            args = dict(kwargs)
            headers = {**http._headers, **args.pop("headers", {})}
            headers.pop(EXECD_TOKEN_HEADER, None)
            if token is not None:
                headers[EXECD_TOKEN_HEADER] = token
            # Preserve gateway credentials but remove only the sandbox's execd credential.
            response = await http.client.request(method, http._prefix + path, headers=headers, timeout=30, **args)
            code = response.status_code
            observed.append({"route": path, "credential": kind, "status": code})
            if kind == "valid" and not 200 <= code < 300:
                return Item(
                    status="error",
                    evidence="valid-token positive control failed",
                    measurements={"requests": observed},
                    scope=scope,
                )
            if kind != "valid" and code not in (401, 403):
                return Item(
                    status="failed" if 200 <= code < 300 else "error",
                    evidence="missing/wrong token was not explicitly rejected",
                    measurements={"requests": observed},
                    scope=scope,
                )
    lifecycle = platform._lifecycle
    leaked = lifecycle.probe_token_disclosure.get(handle.iid)
    if leaked is None:
        return Item(status="error", evidence="create response disclosure observation unavailable", scope=scope)
    disclosure_routes: tuple[tuple[str, dict[str, str | int] | None], ...] = (
        (sandbox_path(handle.iid), None),
        ("/sandboxes", {"metadata": metadata_filter({"flotilla/probe": run_id}), "pageSize": 200}),
    )
    for path, params in disclosure_routes:
        response = await lifecycle._http.request("GET", path, params=params, stage="run", idempotent=True)
        items = response.get("items", []) if path == "/sandboxes" else [response]
        if not any(item.get("id") == handle.iid for item in items):
            return Item(status="error", evidence="disclosure check did not observe the probed instance", scope=scope)
        leaked = leaked or unit.token in json.dumps(response)
    return Item(
        status="failed" if leaked else "passed",
        evidence="exec/file valid, missing and wrong token checks; create/get/list token disclosure",
        measurements={"requests": observed, "token_disclosed": leaked},
        scope=scope,
    )
