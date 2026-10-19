"""Discover internal addresses without guessing unrelated routes."""

from __future__ import annotations

from flotilla.platform.base import ErrorCategory, FlotillaError, ProcessSpec
from flotilla.platform.opensandbox.execd import Execd

# busybox runs no shell expansion; both commands use argv. `-o` prints one record per line for parsing.
_ADDR_ENV: dict[str, str] = {}  # no workload env needed (busybox uses an absolute path, not PATH)
_EXEC_TIMEOUT_S = 30.0


def parse_default_dev(route_out: str) -> str | None:
    """Extract the default-route device from `ip -o route show default`: `default via <gw> dev <dev> ...`.

    Only lines starting with `default` count (the command already filters; this guards against stray lines).
    """
    for line in route_out.splitlines():
        fields = line.split()
        if fields and fields[0] == "default" and "dev" in fields:
            idx = fields.index("dev")
            if idx + 1 < len(fields):
                return fields[idx + 1]
    return None


def parse_dev_ipv4(addr_out: str, dev: str) -> str | None:
    """Extract the device's IPv4 (without `/prefix`) from `ip -o -4 addr show <dev>`.

    Lines look like `2: eth0    inet 10.0.1.7/24 brd ... scope global eth0`; take the address after `inet`.
    """
    for line in addr_out.splitlines():
        fields = line.split()
        if dev in fields and "inet" in fields:
            idx = fields.index("inet")
            if idx + 1 < len(fields):
                return fields[idx + 1].split("/", 1)[0]
    return None


def route_argv(busybox: str) -> tuple[str, ...]:
    return (busybox, "ip", "-4", "-o", "route", "show", "default")


def addr_argv(busybox: str, dev: str) -> tuple[str, ...]:
    return (busybox, "ip", "-4", "-o", "addr", "show", dev)


async def read_internal_address(execd: Execd, iid: str) -> str:
    """Read the IPv4 of the instance's default-route device via execd. Failures are transient at `address`.

    `iid` is only used in diagnostics; `execd` already targets the instance.
    """
    busybox = execd.settings.wrapper
    dev = parse_default_dev((await _run(execd, route_argv(busybox))).decode(errors="replace"))
    if dev is None:
        raise _fail(f"instance {iid} has no default route device (ip route show default has no dev)")
    addr = parse_dev_ipv4((await _run(execd, addr_argv(busybox, dev))).decode(errors="replace"), dev)
    if addr is None:
        raise _fail(f"instance {iid} device {dev} has no IPv4 address")
    return addr


async def _run(execd: Execd, argv: tuple[str, ...]) -> bytes:
    proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env=_ADDR_ENV, timeout_s=_EXEC_TIMEOUT_S)
    result = await execd.exec(proc)
    if result.exit_code != 0:
        raise _fail(f"{' '.join(argv)} exit code {result.exit_code}: {result.stderr.decode(errors='replace')[:200]}")
    return result.stdout


def _fail(message: str) -> FlotillaError:
    return FlotillaError(message, stage="address", category=ErrorCategory.TRANSIENT, retryable=True)
