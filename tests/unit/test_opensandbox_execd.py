"""OpenSandbox 执行通道：exec / start_process / process_status / write_file / read_file（§3.6）。

对着 `httpx.MockTransport` 跑。重点：NDJSON 事件解析、按行还原输出、退出码/超时归一、
`env -i` 只按名字带回环境（值走 `envs`）、命令不重试与总时限、定长 multipart 上传（避开流式分块）、
下载取字节；`legacy` 协议的 `command` 引用与上传后 `chown`。见 backends/opensandbox.md §3.6。
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from flotilla.platform.base import ErrorCategory, FlotillaError, ProcessSpec
from flotilla.platform.fake import ManualClock
from flotilla.platform.opensandbox.execd import (
    BUSYBOX,
    Execd,
    ExecdSettings,
    check_env_keys,
    command_body,
    fold_exec,
    identity_prefix,
    multipart_upload,
    parse_events,
    wrap_argv,
)
from flotilla.platform.opensandbox.http import Http, RetryPolicy

Handler = Callable[[httpx.Request], httpx.Response]


CURRENT = ExecdSettings()
LEGACY = ExecdSettings(protocol="legacy")


def _execd(handler: Handler, clock: ManualClock, settings: ExecdSettings = CURRENT) -> Execd:
    client = httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler))
    return Execd(Http(client, clock, RetryPolicy(attempts=3, initial_s=1.0, cap_s=4.0)), settings)


def _sse(*events: str) -> str:
    """把若干 JSON 事件拼成 execd 的 NDJSON 流（裸 JSON 行、空行分隔）。"""
    return "".join(f"{e}\n\n" for e in events)


def _proc(argv: tuple[str, ...], *, env: dict[str, str] | None = None, timeout_s: float | None = 30.0) -> ProcessSpec:
    return ProcessSpec(argv=argv, uid=1000, gid=1000, cwd="/work", env=env or {}, timeout_s=timeout_s)


# ───────────────────────────── 纯函数：事件解析与退出码归一 ─────────────────────────────


def test_parse_events_ndjson() -> None:
    text = _sse('{"type":"init","text":"cmd-1"}', '{"type":"stdout","text":"hello\\n"}')
    assert parse_events(text) == [{"type": "init", "text": "cmd-1"}, {"type": "stdout", "text": "hello\n"}]


def test_parse_events_tolerates_sse_prefix_and_comment() -> None:
    text = ': keep-alive\ndata: {"type":"ping"}\n\n{"type":"stdout","text":"x"}\n\n'
    assert parse_events(text) == [{"type": "ping"}, {"type": "stdout", "text": "x"}]


def test_parse_events_non_json_line_is_stdout() -> None:
    assert parse_events("plain text line\n\n") == [{"type": "stdout", "text": "plain text line"}]


def test_fold_exec_accumulates_and_completes() -> None:
    events = parse_events(
        _sse(
            '{"type":"init","text":"c"}',
            '{"type":"stdout","text":"out1"}',
            '{"type":"stderr","text":"err"}',
            '{"type":"stdout","text":"out2"}',
            '{"type":"execution_complete","execution_time":5}',
        )
    )
    result = fold_exec(events)
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == b"out1\nout2\n" and result.stderr == b"err\n"


def test_fold_exec_restores_lines() -> None:
    # NDJSON 输出形状：`printf 'a\n\nb\rc\nd'` 的事件为 a、"\n"、b、c、d（每行一个、去掉行尾，空行发 "\n"）。
    texts = ["a", "\n", "b", "c", "d"]
    events = [{"type": "stdout", "text": t} for t in texts] + [{"type": "execution_complete"}]
    # 有损还原：\r 变 \n，末行补 \n；按行切分与原输出一致。
    assert fold_exec(events).stdout == b"a\n\nb\nc\nd\n"
    assert fold_exec(events).stdout.decode().splitlines() == ["a", "", "b", "c", "d"]


def test_fold_exec_error_exit_code() -> None:
    events = parse_events(_sse('{"type":"error","error":{"ename":"CommandExecError","evalue":"7"}}'))
    result = fold_exec(events)
    assert result.exit_code == 7 and not result.timed_out


def test_fold_exec_flat_error_value() -> None:
    # 旧式扁平 evalue 也认（execution.go 的向后兼容）。
    assert fold_exec(parse_events(_sse('{"type":"error","evalue":"3"}'))).exit_code == 3


def test_fold_exec_timeout_is_negative_exit() -> None:
    # execd 超时 kill 进程组，ExitCode()=-1（§3.6）；归一为 timed_out。
    result = fold_exec(parse_events(_sse('{"type":"error","error":{"evalue":"-1"}}')))
    assert result.exit_code == -1 and result.timed_out


def test_fold_exec_truncated_stream_is_failure() -> None:
    # 既无 error 也无 execution_complete：流被截断，按失败退出码 1。
    assert fold_exec(parse_events(_sse('{"type":"stdout","text":"partial"}'))).exit_code == 1


def test_fold_exec_noninteger_error_value() -> None:
    assert fold_exec(parse_events(_sse('{"type":"error","error":{"evalue":"boom"}}'))).exit_code == 1


# ───────────────────────────── 纯函数：argv 包装（只含变量名）─────────────────────────────


def test_wrap_argv_env_i_names_only() -> None:
    assert wrap_argv(("echo", "hi"), ["B", "A"], wrapper=BUSYBOX) == [
        BUSYBOX,
        "sh",
        "-c",
        f'exec {BUSYBOX} env -i A="$A" B="$B" "$@"',  # 键排序；脚本里只有名字
        "flotilla",
        "echo",
        "hi",
    ]


def test_wrap_argv_empty_env() -> None:
    assert wrap_argv(("true",), [], wrapper=BUSYBOX)[3] == f'exec {BUSYBOX} env -i "$@"'


def test_wrap_argv_uses_configured_wrapper() -> None:
    argv = wrap_argv(("ls",), [], wrapper="/opt/bb")
    assert argv[0] == "/opt/bb" and argv[3] == 'exec /opt/bb env -i "$@"'


@pytest.mark.parametrize(
    "bad",
    [
        {"protocol": "v2"},
        {"wrapper": "busybox"},  # 相对路径
        {"wrapper": "/opt/my bin/busybox"},  # 空格会被包装脚本里的 shell 分词
        {"wrapper": "/opt/$X/busybox"},
        {"wrapper": "/opt/bb;id"},
        {"wrapper": "/opt/../bb"},  # 不规范
    ],
)
def test_execd_settings_rejects_bad_values(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ExecdSettings(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["1A", "A-B", "A B", "A=B", "", "A$B", 'A"'])
def test_check_env_keys_rejects_unquotable(key: str) -> None:
    with pytest.raises(FlotillaError) as exc:
        check_env_keys({key: "v"})
    assert exc.value.category is ErrorCategory.INVALID


def test_check_env_keys_accepts_identifiers() -> None:
    check_env_keys({"PATH": "/bin", "_x1": "", "LC_ALL": "C"})


# ───────────────────────────── 纯函数：multipart 编码 ─────────────────────────────


def test_multipart_upload_fixed_length_two_parts() -> None:
    content_type, body = multipart_upload("/etc/hosts", b"127.0.0.1 x\n", 0o644)
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="metadata"' in body and b"application/json" in body
    assert b'{"path": "/etc/hosts", "mode": 644}' in body  # mode 为八进制数字的十进制
    assert b'name="file"' in body and b"application/octet-stream" in body
    assert b"127.0.0.1 x\n" in body
    assert body.endswith(b"--\r\n")  # 结束分隔符


# ───────────────────────────── exec（SSE 流 + 不重试）─────────────────────────────


@pytest.mark.asyncio
async def test_exec_streams_and_folds(clock: ManualClock) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse(
                '{"type":"init","text":"cmd-9"}',
                '{"type":"stdout","text":"ok"}',
                '{"type":"execution_complete"}',
            ),
        )

    secret = "s3cr'et value"
    result = await clock.run(_execd(handler, clock).exec(_proc(("echo", "ok"), env={"K": secret}, timeout_s=5.0)))
    assert result.exit_code == 0 and result.stdout == b"ok\n"
    # 一个请求；值只在 envs，argv 里只有名字；timeout 毫秒；带 uid/gid。
    assert len(posts) == 1
    assert posts[0]["envs"] == {"K": secret}
    assert posts[0]["argv"] == wrap_argv(("echo", "ok"), ["K"], wrapper=BUSYBOX)
    assert secret not in json.dumps(posts[0]["argv"]) and "command" not in posts[0]
    assert posts[0]["timeout"] == 5000 and posts[0]["uid"] == 1000 and posts[0]["background"] is False


@pytest.mark.asyncio
async def test_exec_without_env_omits_envs(clock: ManualClock) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    await clock.run(_execd(handler, clock).exec(_proc(("true",))))
    assert "envs" not in posts[0]


@pytest.mark.asyncio
async def test_exec_bad_env_key_sends_nothing(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("true",), env={"BAD-KEY": "v"})))
    assert calls == 0 and exc.value.category is ErrorCategory.INVALID


@pytest.mark.asyncio
async def test_exec_stream_bounded_by_deadline(clock: ManualClock) -> None:
    # execd 一直发 ping、不收尾：读超时等不到，靠 timeout_s + HTTP_SLACK_S 的总时限结束（经注入时钟）。
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"type":"ping"}\n\n'
            await clock.sleep(10_000)
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stalled())

    started = clock.monotonic()
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("sleep", "999"), timeout_s=5.0)))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.stage == "run"
    assert clock.monotonic() - started == pytest.approx(5.0 + Execd.HTTP_SLACK_S)


@pytest.mark.asyncio
async def test_exec_requires_timeout(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    with pytest.raises(ValueError, match="timeout"):
        await clock.run(_execd(handler, clock).exec(_proc(("x",), timeout_s=None)))


@pytest.mark.asyncio
async def test_exec_command_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("x",))))
    assert calls == 1 and exc.value.category is ErrorCategory.TRANSIENT  # 命令执行不幂等（§3.5）


# ───────────────────────────── start_process / process_status ─────────────────────────────


@pytest.mark.asyncio
async def test_start_process_returns_init_id(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["background"] is True
        return httpx.Response(200, content=_sse('{"type":"init","text":"bg-42"}', '{"type":"execution_complete"}'))

    pid = await clock.run(_execd(handler, clock).start_process(_proc(("server",), timeout_s=None)))
    assert pid == "bg-42"


@pytest.mark.asyncio
async def test_process_status_running(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/command/status/bg-42"
        return httpx.Response(200, json={"id": "bg-42", "running": True, "exit_code": None})

    status = await clock.run(_execd(handler, clock).process_status("bg-42"))
    assert status.found and status.running and status.exit_code is None


@pytest.mark.asyncio
async def test_process_status_finished(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "bg-42", "running": False, "exit_code": 0})

    status = await clock.run(_execd(handler, clock).process_status("bg-42"))
    assert status.found and not status.running and status.exit_code == 0


@pytest.mark.asyncio
async def test_process_status_404_is_not_found(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "gone"})

    status = await clock.run(_execd(handler, clock).process_status("bg-x"))
    assert status == type(status)(found=False, running=False, exit_code=None)


# ───────────────────────────── write_file / read_file ─────────────────────────────


@pytest.mark.asyncio
async def test_write_file_fixed_length_multipart_to_identity_route(clock: ManualClock) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["ctype"] = request.headers["Content-Type"]
        captured["clen"] = request.headers.get("Content-Length")
        captured["body"] = bytes(request.content)
        return httpx.Response(200)

    data = b"hello world"
    await clock.run(_execd(handler, clock).write_file("/srv/f.txt", data, mode=0o644, uid=1000, gid=1000))
    assert captured["path"] == f"{identity_prefix(1000, 1000)}/files/upload"
    assert str(captured["ctype"]).startswith("multipart/form-data; boundary=")
    # 定长：显式 Content-Length，且等于内存里拼好的 body（不是流式/分块）。
    assert captured["clen"] == str(len(captured["body"]))  # type: ignore[arg-type]
    assert data in captured["body"]  # type: ignore[operator]
    assert b'{"path": "/srv/f.txt", "mode": 644}' in captured["body"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_write_file_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError):
        await clock.run(_execd(handler, clock).write_file("/f", b"x", mode=0o644, uid=0, gid=0))
    assert calls == 1  # 覆盖不幂等（§3.5）


@pytest.mark.asyncio
async def test_read_file_returns_bytes(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/files/download"
        assert request.url.params["path"] == "/etc/hosts"
        return httpx.Response(200, content=b"127.0.0.1 localhost\n")

    data = await clock.run(_execd(handler, clock).read_file("/etc/hosts"))
    assert data == b"127.0.0.1 localhost\n"


@pytest.mark.asyncio
async def test_read_file_404_is_not_found(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "no file"})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).read_file("/missing"))
    assert exc.value.category is ErrorCategory.NOT_FOUND


# ───────────────────────────── legacy 协议（§3.6）─────────────────────────────


HOSTILE_ARGS = (
    "",  # 空参数
    "a b",
    "tab\there",
    "nl\nx",
    "crlf\r\n",
    "back\\\nslash",
    "-n",
    "--",
    "$HOME",
    "${HOME}",
    "$(id)",
    "`id`",
    "!!",
    "~",
    "a=~",
    "*",
    "{a,b}",
    "#c",
    "'\"",
    "非ASCII\u2028😀",
)


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash"])
def test_legacy_command_reproduces_argv_and_env_in_a_real_shell(shell: str, tmp_path: Path) -> None:
    """execd 的 legacy 模式以 `<shell> -c <command>` 执行，请求的 envs 叠加在 execd 的环境上：对着真实 shell
    验证分词一次后得到的就是原 argv，且进程环境恰好是 `proc.env`。

    包装器用一个把 sh / env 转给系统工具的脚本代替 busybox。
    """
    if not Path(shell).exists():
        pytest.skip(f"没有 {shell}")
    bb = tmp_path / "bb"
    bb.write_text(
        '#!/bin/sh\napp=$1; shift\ncase $app in sh) exec /bin/sh "$@";; env) exec /usr/bin/env "$@";; esac\nexit 99\n'
    )
    bb.chmod(0o755)
    settings = ExecdSettings(protocol="legacy", wrapper=str(bb))
    env = {"K": "s3cr'et $(id) `id` value", "PATH": "/usr/bin:/bin"}

    def run(argv: tuple[str, ...]) -> list[str]:
        body = command_body(_proc(argv, env=env), background=False, settings=settings)
        assert "canary" not in body["command"] and "s3cr" not in body["command"]  # 值只在 envs
        noisy = {"PATH": "/usr/bin:/bin", "IFS": "x", "NOISE": "1", **body["envs"]}
        out = subprocess.run([shell, "-c", body["command"]], env=noisy, capture_output=True, check=True).stdout
        return out.decode().split("\0")[:-1]

    assert run(("/usr/bin/printf", "%s\\0", *HOSTILE_ARGS)) == list(HOSTILE_ARGS)
    assert sorted(run(("/usr/bin/env", "-0"))) == sorted(f"{k}={v}" for k, v in env.items())


def test_legacy_and_current_bodies_differ_only_in_command_field() -> None:
    proc = _proc(("true",), env={"A": "1"}, timeout_s=2.0)
    current = command_body(proc, background=True, settings=CURRENT)
    legacy = command_body(proc, background=True, settings=LEGACY)
    assert current.pop("argv") == shlex.split(legacy.pop("command"))
    assert (
        current
        == legacy
        == {"cwd": "/work", "background": True, "uid": 1000, "gid": 1000, "envs": {"A": "1"}, "timeout": 2000}
    )


@pytest.mark.asyncio
async def test_legacy_write_file_as_root_uses_plain_route_without_chown(clock: ManualClock) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200)

    await clock.run(_execd(handler, clock, LEGACY).write_file("/etc/hosts", b"x", mode=0o644, uid=0, gid=0))
    assert paths == ["/files/upload"]


@pytest.mark.asyncio
async def test_legacy_write_file_non_root_chowns_then_restores_mode(clock: ManualClock) -> None:
    calls: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            calls.append(("upload", bytes(request.content)))
            return httpx.Response(200)
        calls.append(("command", json.loads(request.content)))
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    await clock.run(_execd(handler, clock, LEGACY).write_file("/srv/a b.txt", b"data", mode=0o4750, uid=1000, gid=1001))
    assert [kind for kind, _ in calls] == ["upload", "command"]
    assert b'{"path": "/srv/a b.txt", "mode": 4750}' in calls[0][1]  # type: ignore[operator]
    cmd = calls[1][1]
    assert isinstance(cmd, dict) and cmd["uid"] == 0 and cmd["gid"] == 0 and "envs" not in cmd
    # 包装之后的原 argv：经 busybox 的 chown 与 chmod（root 改属主会清掉 setuid 位，须重设 mode）；路径含空格也安全。
    inner = shlex.split(cmd["command"])
    inner = inner[inner.index("flotilla") + 1 :]
    assert inner == [
        BUSYBOX,
        "sh",
        "-c",
        '"$0" chown "$1" "$3" && "$0" chmod "$2" "$3"',
        BUSYBOX,
        "1000:1001",
        "4750",
        "/srv/a b.txt",
    ]


@pytest.mark.asyncio
async def test_legacy_write_file_chown_failure_is_invalid(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            return httpx.Response(200)
        return httpx.Response(
            200,
            content=_sse(
                '{"type":"stderr","text":"chown: Operation not permitted"}', '{"type":"error","error":{"evalue":"1"}}'
            ),
        )

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert exc.value.category is ErrorCategory.INVALID and not exc.value.retryable
    assert "Operation not permitted" in str(exc.value)


@pytest.mark.asyncio
async def test_legacy_write_file_upload_failure_skips_chown(clock: ManualClock) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500)

    with pytest.raises(FlotillaError):
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert paths == ["/files/upload"]  # 不重试、也不在失败后去 chown


@pytest.mark.parametrize("path", ["rel", "/srv/$HOME/f", "/srv/${X}"])
@pytest.mark.asyncio
async def test_legacy_write_file_rejects_relative_or_expandable_path(clock: ManualClock, path: str) -> None:
    # 旧版上传会按 execd 的环境展开 `$VAR`，随后的 chown 拿字面路径：两步会落到不同文件。
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(ValueError):
        await clock.run(_execd(handler, clock, LEGACY).write_file(path, b"", mode=0o600, uid=1000, gid=1000))
    assert calls == 0


@pytest.mark.parametrize("key", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"])
def test_legacy_rejects_env_that_alters_outer_shell(key: str) -> None:
    # 例如 SHELLOPTS=noexec 会让外层 bash 静默返回 0，健康检查假通过；current 协议没有外层 shell，不受影响。
    with pytest.raises(FlotillaError) as exc:
        command_body(_proc(("true",), env={key: "x"}), background=False, settings=LEGACY)
    assert exc.value.category is ErrorCategory.INVALID
    command_body(_proc(("true",), env={key: "x"}), background=False, settings=CURRENT)


@pytest.mark.asyncio
async def test_legacy_write_file_chown_timeout_is_transient(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            return httpx.Response(200)
        return httpx.Response(200, content=_sse('{"type":"error","error":{"evalue":"-1"}}'))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.retryable
