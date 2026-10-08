"""execd 执行通道：前台执行、后台进程、文件（backends/opensandbox.md §3.1、§3.6）。

execd（44772）的事实决定本模块的做法（源码 `c7dc78a4`，2026-10-03 核对）：

- 每次执行都包一层 `env -i`：`sh -c 'exec busybox env -i K1="$K1" … "$@"'` 清空 execd 继承的环境、只留
  `proc.env` 的键，进程环境恰好是 `proc.env`（§3.6，`EXECD_ACCESS_TOKEN`、`OPENSANDBOX_ID` 等一并清掉）。环境**值**
  放进请求的 `envs`，命令里只出现变量**名**。execd 的命令 `content`（`GET /command/status/{id}`、`command: received`
  日志）只含 argv / 命令串、不含 `envs`（`commandContent()`），值不经状态与日志外泄。
  包装用的 busybox 由 `ExecdSettings.wrapper` 给出：单元从 share 挂载得到它，锚点镜像须在同一路径自带（§3.3）。
- 两种协议（`ExecdSettings.protocol`，部署配置显式选择，不在运行时探测——命令不幂等，回退时机无法保证正确）：
  - `current`：`POST /command` 用 **argv**（不经 shell，上游 2026-09-08 起）；上传走 `/v1/filesystem/{uid}/{gid}/`
    按数字身份写入（上游 2026-09-29 起）。
  - `legacy`：`c7dc78a4` 之前的 execd。不认 `argv`，命令以 `command` 字符串经 execd 的 `bash -c`（没有 bash
    时 `sh -c`）执行：把包装后的 argv 以 `shlex.join` 引用成一行，shell 只做一次分词即还原原 argv，故镜像须有
    `/bin/sh`。上传走 `/files/upload`，文件由 execd 自身（须为 root）写入，
    目标身份不是 root 时随后经一次执行 `chown`。
- execd 的 SSE 实为 **NDJSON**：每个事件是一行裸 JSON、以空行分隔（`sse.go`），`init` 事件的 `text` 是命令 id，
  前台成功以 `execution_complete` 收尾、失败发 `error`（`error.evalue` 为退出码串，超时被 SIGKILL 时为 `-1`）。
  stdout / stderr **按行**成事件且去掉行尾（`commandOutputTail.read`：`\n`、`\r` 都算行尾，空行单独发一个
  `"\n"`），`fold_exec` 据此还原换行。
- 命令执行与文件覆盖**不幂等、不重试**（§3.5、PRD N5）：失败原样报给调用方。`GET /command/status/{id}`、
  `GET /files/download` 幂等，经 `Http` 重试。
- 上传**避开 multipart 的分块形态**：在内存里拼完整 multipart body、带显式 `Content-Length`
  （httpx 的流式 multipart 经网关代理会失败，§3.6、§8.1）。写入是 `O_TRUNC` 原地覆盖。

`Execd` 持有一个指向某实例 execd 的 `Http`（经 server 代理，由 `platform` 按 iid 解析，§3.1）；
纯函数（事件解析、argv 包装、multipart 编码）拆出便于单测。
"""

from __future__ import annotations

import json
import math
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, ProcessSpec, ProcessStatus, Stage
from flotilla.platform.opensandbox.http import Http

BUSYBOX = "/.flotilla/bin/busybox"  # share 中的静态 busybox（§3.2、§3.3）
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # 能在 sh 里以 "$K" 引用的变量名
_UPLOAD_BOUNDARY = "flotilla5f8e2a1c"  # 固定 boundary：请求定长、可断言（内容不含它）
_CHOWN_TIMEOUT_S = 30.0  # legacy 上传后的 chown
#: 包装器路径会原样写进 `sh -c` 脚本：只允许不需要引用的字符，路径里的空格、`$`、引号不会被 shell 另行解释。
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
#: legacy 下 `envs` 先进入 execd 的外层 bash（非交互）：这些变量会改变外层 shell 本身——`BASH_ENV` 被 source，
#: `SHELLOPTS=noexec` 让一切命令静默返回 0（健康检查假通过），`BASHOPTS` / `ENV` 同理。Docker 下不存在这一层，拒绝。
LEGACY_UNSAFE_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"})

ExecdProtocol = Literal["current", "legacy"]


@dataclass(frozen=True)
class ExecdSettings:
    """部署配置 `[opensandbox.execd]`（§3.6）。"""

    protocol: ExecdProtocol = "current"
    #: 执行包装用的 busybox，所有实例同一路径；须带 sh、env（包装）、ip（读地址）、chown、chmod（legacy 上传）applet。
    wrapper: str = BUSYBOX

    def __post_init__(self) -> None:
        if self.protocol not in ("current", "legacy"):
            raise ValueError(f"execd.protocol 须为 current 或 legacy：{self.protocol!r}")
        if not _SAFE_PATH.fullmatch(self.wrapper) or posixpath.normpath(self.wrapper) != self.wrapper:
            raise ValueError(f"execd.wrapper 须为规范的绝对路径，只含 [A-Za-z0-9_./-]：{self.wrapper!r}")


# ─────────────────────────────────────────────────────────────────
# 纯函数：SSE(NDJSON) 解析、退出码归一、argv 包装、multipart 编码
# ─────────────────────────────────────────────────────────────────


def parse_events(text: str) -> list[dict[str, Any]]:
    """把 execd 的 NDJSON 流切成事件对象。每个事件是一行裸 JSON，事件间以空行分隔（`sse.go`）。

    非 JSON 行按一条 stdout 处理（与 Go SDK 的回退一致）；`:` 开头的 SSE 注释、`data:` 前缀都容忍。
    """
    events: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
            if not line:
                continue
        try:
            obj = json.loads(line)
        except ValueError:
            events.append({"type": "stdout", "text": raw})
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def fold_exec(events: Sequence[Mapping[str, Any]]) -> ExecResult:
    """累积 stdout/stderr，归一退出码与超时。

    输出按行还原：execd 每行一个事件且去掉了行尾，空行发 `"\n"`，这里给其余事件补回 `\n`。还原有损——
    `\r` 变成 `\n`，没有行尾的最后一行也补上 `\n`；按行消费的调用方（列目录、日志尾、地址解析）不受影响，
    要逐字节的输出须写到文件再 `read_file`。

    退出码：`error` 事件的 `error.evalue`（或旧式扁平 `evalue`）能转成整数时取它，否则 `execution_complete`
    视为 0；两者都没有（流被截断）时按失败退出码 1。`timed_out` 当退出码为负——execd 超时 kill 整个进程组，
    `ExitCode()` 返回 -1（§3.6；信号致死没有正常退出码，调用方不应依赖具体值）。
    """
    stdout: list[str] = []
    stderr: list[str] = []
    exit_code: int | None = None
    completed = False
    for ev in events:
        kind = ev.get("type")
        if kind == "stdout":
            stdout.append(_line(ev))
        elif kind == "stderr":
            stderr.append(_line(ev))
        elif kind == "execution_complete":
            completed = True
        elif kind == "error":
            exit_code = _error_exit_code(ev)
    if exit_code is None:
        exit_code = 0 if completed else 1
    return ExecResult(
        exit_code=exit_code,
        stdout="".join(stdout).encode(),
        stderr="".join(stderr).encode(),
        timed_out=exit_code < 0,
    )


def _line(event: Mapping[str, Any]) -> str:
    text = str(event.get("text") or "")
    return text if text == "\n" else text + "\n"


def _error_exit_code(event: Mapping[str, Any]) -> int:
    """从 `error` 事件取退出码：优先嵌套 `error.evalue`，回退扁平 `evalue`；非整数按 1。"""
    err = event.get("error")
    value = err.get("evalue") if isinstance(err, dict) else event.get("evalue")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 1


def check_env_keys(env: Mapping[str, str]) -> None:
    """变量名须能在 sh 里以 `"$K"` 引用（`[A-Za-z_][A-Za-z0-9_]*`）：包装里只写名字，名字不合法就传不进去。"""
    bad = sorted(key for key in env if not _ENV_KEY.fullmatch(key))
    if bad:
        raise FlotillaError(
            f"环境变量名不合法（须为 [A-Za-z_][A-Za-z0-9_]*）：{bad!r}",
            stage="run",
            category=ErrorCategory.INVALID,
            retryable=False,
        )


def wrap_argv(argv: Sequence[str], env_keys: Sequence[str], *, wrapper: str) -> list[str]:
    """包成 `env -i` 清空继承环境、只按名字带回 `env_keys` 后再 exec 原 argv（§3.6）。

    值经请求 `envs` 进入 sh 的环境，`K="$K"` 由 sh 展开后交给 `env -i`；`$0` 是 `flotilla`，`"$@"` 是原 argv。
    脚本里只有已校验的变量名（`check_env_keys`），没有任何值。键按名字排序（确定性）。`wrapper` 是 busybox 的路径。
    """
    assigns = "".join(f'{key}="${key}" ' for key in sorted(env_keys))
    return [wrapper, "sh", "-c", f'exec {wrapper} env -i {assigns}"$@"', "flotilla", *argv]


def multipart_upload(path: str, data: bytes, mode: int) -> tuple[str, bytes]:
    """按 execd 的两段式上传拼出完整 multipart body，返回 (content_type, body)。

    `mode` 送八进制数字的十进制形式（`0o644` → `644`，与 execd 的 `strconv.ParseUint(itoa(mode), 8, 32)`
    一致）。不传 owner/group：属主由 identity 路由的 uid/gid 决定（§3.6）。body 在内存里一次拼好，调用方据此
    设 `Content-Length`——避开 httpx 流式 multipart 的 chunked 形态（经网关代理会失败，§8.1）。
    """
    meta = json.dumps({"path": path, "mode": _octal_mode(mode)}).encode()
    dash, crlf = b"--", b"\r\n"
    boundary = _UPLOAD_BOUNDARY.encode()
    body = b"".join(
        [
            dash,
            boundary,
            crlf,
            b'Content-Disposition: form-data; name="metadata"; filename="metadata"',
            crlf,
            b"Content-Type: application/json",
            crlf,
            crlf,
            meta,
            crlf,
            dash,
            boundary,
            crlf,
            b'Content-Disposition: form-data; name="file"; filename="file"',
            crlf,
            b"Content-Type: application/octet-stream",
            crlf,
            crlf,
            data,
            crlf,
            dash,
            boundary,
            dash,
            crlf,
        ]
    )
    return f"multipart/form-data; boundary={_UPLOAD_BOUNDARY}", body


def _octal_mode(mode: int) -> int:
    """`0o644`（十进制 420）→ `644`：取权限位的八进制数字当十进制整数（同 Go `OctalMode`）。"""
    return int(format(mode & 0o7777, "o"))


def identity_prefix(uid: int, gid: int) -> str:
    return f"/v1/filesystem/{uid}/{gid}"


# ─────────────────────────────────────────────────────────────────
# Execd：一个实例的执行通道
# ─────────────────────────────────────────────────────────────────


class Execd:
    """某实例 execd 上的执行、后台进程与文件操作。`http` 指向该实例（经 server 代理，§3.1）。"""

    #: 命令请求的总时限余量（秒）：前台为 `ProcessSpec.timeout_s` + 它（execd kill 进程组、收尾 SSE 需时间），
    #: 后台启动为它本身。读超时管不住命令请求——execd 每 3 秒发一次 `ping`，间隔永远不超时。
    HTTP_SLACK_S = 30.0

    def __init__(self, http: Http, settings: ExecdSettings) -> None:
        self._http = http
        self.settings = settings

    async def exec(self, proc: ProcessSpec) -> ExecResult:
        """前台执行：流式消费 SSE、归一退出码与超时。不重试（命令执行不幂等，§3.5）。"""
        if proc.timeout_s is None:
            raise ValueError("exec 需要 ProcessSpec.timeout_s（前台命令只有 timeout 能结束，§3.6）")
        body = command_body(proc, background=False, settings=self.settings)
        text = await self._stream_command(body, stage="run", deadline_s=proc.timeout_s + self.HTTP_SLACK_S)
        return fold_exec(parse_events(text))

    async def start_process(self, proc: ProcessSpec) -> str:
        """后台启动，返回命令 id（取自 `init` 事件）。不重试（§3.5）。"""
        body = command_body(proc, background=True, settings=self.settings)
        text = await self._stream_command(body, stage="start", deadline_s=self.HTTP_SLACK_S)
        for ev in parse_events(text):
            if ev.get("type") == "init" and ev.get("text"):
                return str(ev["text"])
        raise FlotillaError(
            "后台命令未返回 init 事件，无法得到进程 id",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
        )

    async def process_status(self, pid: str) -> ProcessStatus:
        """`GET /command/status/{id}`；404（记录丢失）→ `found=False`，编排核心据此判 `lost`（§3.6）。"""
        try:
            result = await self._http.request(
                "GET", f"/command/status/{quote(pid, safe='')}", stage="run", idempotent=True
            )
        except FlotillaError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                return ProcessStatus(found=False, running=False, exit_code=None)
            raise
        if not isinstance(result, dict):
            raise FlotillaError(
                f"命令状态响应异常：{result!r}", stage="run", category=ErrorCategory.TRANSIENT, retryable=True
            )
        exit_code = result.get("exit_code")
        return ProcessStatus(
            found=True,
            running=bool(result.get("running")),
            exit_code=int(exit_code) if exit_code is not None else None,
        )

    async def write_file(self, path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
        """定长 multipart 上传，`O_TRUNC` 覆盖。不重试（§3.5）。

        `current`：`/v1/filesystem/{uid}/{gid}/files/upload`，以目标身份写入。
        `legacy`：`/files/upload` 由 execd（root）写入；目标身份不是 root 时再经一次执行 `chown` 并重设 mode
        （root 改属主时内核会清掉 setuid / setgid 位）。与 `current` 的差别：以 root 写入，不检查目标身份对目录的
        写权限；上传成功而 `chown` 失败时文件已写入、属主仍为 root，按 `invalid` 报告。
        """
        content_type, body = multipart_upload(path, data, mode)
        if self.settings.protocol == "legacy":
            # 旧版上传会按 execd 的环境展开路径里的 `$VAR`（`pathutil.ExpandPath`），随后的 chown 拿的是字面路径。
            if not posixpath.isabs(path) or "$" in path:
                raise ValueError(f"legacy execd 的 write_file 需要不含 `$` 的绝对路径：{path!r}")
            route = "/files/upload"
        else:
            route = f"{identity_prefix(uid, gid)}/files/upload"
        await self._http.request(
            "POST",
            route,
            stage="run",
            idempotent=False,
            content=body,
            headers={"Content-Type": content_type},
        )
        if self.settings.protocol == "legacy" and (uid, gid) != (0, 0):
            await self._chown(path, mode=mode, uid=uid, gid=gid)

    async def read_file(self, path: str) -> bytes:
        """`GET /files/download?path=...` 取原始字节；404 → `not_found`。幂等，可重试。"""
        return await self._http.download("/files/download", stage="run", params={"path": path})

    async def _chown(self, path: str, *, mode: int, uid: int, gid: int) -> None:
        """legacy 上传后改属主与 mode。applet 经包装器的绝对路径调用，不依赖镜像的 PATH 与工具。"""
        w = self.settings.wrapper
        script = '"$0" chown "$1" "$3" && "$0" chmod "$2" "$3"'
        argv = (w, "sh", "-c", script, w, f"{uid}:{gid}", format(mode & 0o7777, "o"), path)
        proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env={}, timeout_s=_CHOWN_TIMEOUT_S)
        result = await self.exec(proc)
        if result.timed_out:
            raise FlotillaError(
                f"{path} 已写入，但改属主与 mode 超时（{_CHOWN_TIMEOUT_S}s）",
                stage="run",
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )
        if result.exit_code != 0:
            raise FlotillaError(
                f"{path} 已写入，但 chown {uid}:{gid} / chmod {mode:o} 失败（execd 须以 root 运行，包装器须带这两个"
                f" applet）：{result.stderr.decode(errors='replace').strip()[-300:]}",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )

    async def _stream_command(self, body: Mapping[str, Any], *, stage: Stage, deadline_s: float) -> str:
        """`POST /command`，读完 NDJSON 流返回原文。命令不幂等，连错误也不重试（§3.5）。"""
        return await self._http.stream_post("/command", json=body, stage=stage, deadline_s=deadline_s)


def command_body(proc: ProcessSpec, *, background: bool, settings: ExecdSettings) -> dict[str, Any]:
    """`RunCommandRequest`：命令里只含变量名，值放 `envs`（§3.6）；timeout 为毫秒。

    `current` 以 `argv` 发出包装后的命令；`legacy` 发 `command`：`shlex.join` 后由 execd 的 shell 分词一次，还原出
    同一个 argv。差别：`legacy` 下 `envs` 先进入 execd 的外层 shell 再被包装清空，改变外层 shell 行为的变量
    （`LEGACY_UNSAFE_ENV`）因此以 `invalid` 拒绝。
    """
    check_env_keys(proc.env)
    if settings.protocol == "legacy" and (unsafe := sorted(LEGACY_UNSAFE_ENV & set(proc.env))):
        raise FlotillaError(
            f"legacy execd 下这些变量会改变 execd 的外层 shell，不能传入：{unsafe}",
            stage="run",
            category=ErrorCategory.INVALID,
            retryable=False,
        )
    argv = wrap_argv(proc.argv, list(proc.env), wrapper=settings.wrapper)
    body: dict[str, Any] = {"cwd": proc.cwd, "background": background, "uid": proc.uid, "gid": proc.gid}
    if settings.protocol == "legacy":
        body["command"] = shlex.join(argv)
    else:
        body["argv"] = argv
    if proc.env:
        body["envs"] = dict(proc.env)
    if proc.timeout_s is not None:
        body["timeout"] = math.ceil(proc.timeout_s * 1000)
    return body
