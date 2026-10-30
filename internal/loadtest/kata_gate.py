"""Loadtest-local shim: gate the first execd command behind an execd /ping readiness poll.

kata VMs reach Running before the platform injects execd; flotilla (8218d757) fires the
address-discovery command the moment an instance reads Running, and a 503 from a not-yet-ready
execd is never retried (stream_post treats commands as non-idempotent), so kata environments
fail at the address stage. The real fix belongs in flotilla (per-instance readiness gate before
the first command); this shim keeps P0 and the loadtest arms unblocked without touching the
pinned install. Import it before opening environments.
"""

from __future__ import annotations

import asyncio
import time

from flotilla.platform.base import FlotillaError
from flotilla.platform.opensandbox import platform as osp

_GATED: set[str] = set()
PING_TIMEOUT_S = 45.0
PING_INTERVAL_S = 2.0


async def _ping_until_ready(platform: object, iid: str) -> None:
    if iid in _GATED:
        return
    execd = await platform._execd_of(iid, stage="address")  # noqa: SLF001 - shim by design
    deadline = time.monotonic() + PING_TIMEOUT_S
    began = time.monotonic()
    while True:
        try:
            await execd._http.request("GET", "/ping", stage="address", idempotent=True)  # noqa: SLF001
            _GATED.add(iid)
            if (waited := time.monotonic() - began) > PING_INTERVAL_S:
                print(f"kata_gate: execd ready after {waited:.0f}s for {iid}", flush=True)
            return
        except FlotillaError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(PING_INTERVAL_S)


_original_internal_address = osp.OpenSandboxPlatform.internal_address


async def _gated_internal_address(self, handle):  # noqa: ANN001 - matches protocol signature
    await _ping_until_ready(self, handle.iid)
    return await _original_internal_address(self, handle)


def install() -> None:
    osp.OpenSandboxPlatform.internal_address = _gated_internal_address


install()
