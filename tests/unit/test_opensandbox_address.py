"""OpenSandbox internal address: default-route device parsing + exec-channel read."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from flotilla.platform.base import FlotillaError
from flotilla.platform.opensandbox.address import parse_default_dev, parse_dev_ipv4, read_internal_address
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings
from flotilla.platform.opensandbox.http import Http, RetryPolicy
from tests.support.platform import ManualClock

Handler = Callable[[httpx.Request], httpx.Response]

ROUTE = "default via 10.0.1.1 dev eth0 src 10.0.1.7 metric 100\n"
ADDR = "2: eth0    inet 10.0.1.7/24 brd 10.0.1.255 scope global eth0\n"


# ───────────────────────────── Pure parsing ─────────────────────────────


def test_parse_default_dev() -> None:
    assert parse_default_dev(ROUTE) == "eth0"


def test_parse_default_dev_picks_default_line() -> None:
    out = "10.0.1.0/24 dev eth1 scope link\ndefault via 10.0.1.1 dev eth0\n"
    assert parse_default_dev(out) == "eth0"


def test_parse_default_dev_none_when_absent() -> None:
    assert parse_default_dev("10.0.1.0/24 dev eth1 scope link\n") is None


def test_parse_dev_ipv4() -> None:
    assert parse_dev_ipv4(ADDR, "eth0") == "10.0.1.7"


def test_parse_dev_ipv4_ignores_other_dev() -> None:
    out = "3: eth1    inet 192.168.0.5/24 scope global eth1\n2: eth0    inet 10.0.1.7/24 scope global eth0\n"
    assert parse_dev_ipv4(out, "eth0") == "10.0.1.7"


def test_parse_dev_ipv4_none_when_no_inet() -> None:
    assert parse_dev_ipv4("2: eth0    mtu 1500 state UP\n", "eth0") is None


# ───────────────────────────── Reading via the exec channel ─────────────────────────────


def _sse_exec(stdout: str, *, exit_code: int = 0) -> bytes:
    import json

    # Same as execd: one stdout event per line, line endings stripped.
    events = [json.dumps({"type": "stdout", "text": line}) for line in stdout.splitlines()]
    if exit_code == 0:
        events.append(json.dumps({"type": "execution_complete"}))
    else:
        events.append(json.dumps({"type": "error", "evalue": str(exit_code)}))
    return "".join(f"{e}\n\n" for e in events).encode()


def _execd(handler: Handler, clock: ManualClock, settings: ExecdSettings | None = None) -> Execd:
    client = httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler))
    return Execd(Http(client, clock, RetryPolicy(attempts=2, initial_s=1.0)), settings or ExecdSettings())


@pytest.mark.asyncio
async def test_read_internal_address(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        argv = json.loads(request.content)["argv"]
        kind = "route" if "route" in argv else "addr"
        return httpx.Response(200, content=_sse_exec(ROUTE if kind == "route" else ADDR))

    assert await clock.run(read_internal_address(_execd(handler, clock), "sb-1")) == "10.0.1.7"


@pytest.mark.asyncio
async def test_read_internal_address_no_default_route(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_exec("10.0.1.0/24 dev eth1 scope link\n"))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(read_internal_address(_execd(handler, clock), "sb-1"))
    assert exc.value.stage == "address" and "no default route" in str(exc.value)


@pytest.mark.asyncio
async def test_read_internal_address_exec_nonzero(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_exec("", exit_code=1))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(read_internal_address(_execd(handler, clock), "sb-1"))
    assert exc.value.stage == "address"


@pytest.mark.asyncio
async def test_read_internal_address_uses_configured_busybox(clock: ManualClock) -> None:
    import json

    argvs: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        argv = json.loads(request.content)["argv"]
        argvs.append(argv)
        return httpx.Response(200, content=_sse_exec(ROUTE if "route" in argv else ADDR))

    execd = _execd(handler, clock, ExecdSettings(wrapper="/opt/bb"))
    assert await clock.run(read_internal_address(execd, "sb-1")) == "10.0.1.7"
    # The original argv after wrapping (after "flotilla") starts with the configured busybox instead of a
    # hard-coded share path.
    inner = [a[a.index("flotilla") + 1 :] for a in argvs]
    assert inner == [
        ["/opt/bb", "ip", "-4", "-o", "route", "show", "default"],
        ["/opt/bb", "ip", "-4", "-o", "addr", "show", "eth0"],
    ]
