"""内部地址：经执行通道读默认路由网卡的 IPv4（backends/opensandbox.md §3.2、C7）。

标准 `SandboxStatus` 没有 IP 字段，`get_endpoint()` 返回的是网关/代理地址、不能用于组内流量（§3.2）；
本后端经 execd 跑 busybox（`ExecdSettings.wrapper`）：先看默认路由在哪张网卡，
再取该网卡的 IPv4 地址（`internal_address = "exec"`）。

解析是纯函数，单独可测；读取经两次 `exec`。
"""

from __future__ import annotations

from flotilla.platform.base import ErrorCategory, FlotillaError, ProcessSpec
from flotilla.platform.opensandbox.execd import Execd

# busybox 不展开 shell；两条都用 argv。`-o` 每条记录单行，便于解析。
_ADDR_ENV: dict[str, str] = {}  # 读地址无需业务环境（busybox 走绝对路径，不依赖 PATH）
_EXEC_TIMEOUT_S = 30.0


def parse_default_dev(route_out: str) -> str | None:
    """从 `ip -o route show default` 的输出取默认路由网卡：`default via <gw> dev <dev> ...`。

    只认以 `default` 开头的路由行（命令已 `show default`，这里再防其他行混入）。
    """
    for line in route_out.splitlines():
        fields = line.split()
        if fields and fields[0] == "default" and "dev" in fields:
            idx = fields.index("dev")
            if idx + 1 < len(fields):
                return fields[idx + 1]
    return None


def parse_dev_ipv4(addr_out: str, dev: str) -> str | None:
    """从 `ip -o -4 addr show <dev>` 取该网卡的 IPv4（去掉 `/prefix`）。

    行形如 `2: eth0    inet 10.0.1.7/24 brd ... scope global eth0`；取 `inet` 后的地址。
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
    """经 execd 读实例默认路由网卡的 IPv4（§3.2）。失败/读不到按 `address` 阶段的 transient。

    `iid` 只用于诊断消息；`execd` 已指向该实例。
    """
    busybox = execd.settings.wrapper
    dev = parse_default_dev((await _run(execd, route_argv(busybox))).decode(errors="replace"))
    if dev is None:
        raise _fail(f"实例 {iid} 找不到默认路由网卡（ip route show default 无 dev）")
    addr = parse_dev_ipv4((await _run(execd, addr_argv(busybox, dev))).decode(errors="replace"), dev)
    if addr is None:
        raise _fail(f"实例 {iid} 网卡 {dev} 没有 IPv4 地址")
    return addr


async def _run(execd: Execd, argv: tuple[str, ...]) -> bytes:
    proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env=_ADDR_ENV, timeout_s=_EXEC_TIMEOUT_S)
    result = await execd.exec(proc)
    if result.exit_code != 0:
        raise _fail(f"{' '.join(argv)} 退出码 {result.exit_code}：{result.stderr.decode(errors='replace')[:200]}")
    return result.stdout


def _fail(message: str) -> FlotillaError:
    return FlotillaError(message, stage="address", category=ErrorCategory.TRANSIENT, retryable=True)
